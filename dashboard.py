#!/usr/bin/env python3
"""Lightweight Linux dashboard + service manager. Python stdlib only.

Works on any systemd-based Linux (Debian/Ubuntu, Raspberry Pi OS, Armbian,
Fedora, Arch, ...). Install with install.sh, or run standalone:

    python3 dashboard.py --config ./config.json --port 7979
"""
import argparse, http.server, json, subprocess, os, sys, time, threading, re, socket, tempfile
import pty, select, fcntl, struct, termios, signal, codecs, uuid, binascii, urllib.parse

APP_VERSION = "v1.1"
PORT = 7979
BIND = "0.0.0.0"
CONFIG = os.environ.get("DASH_CONFIG", "/etc/dashboard/config.json")


def _run_dir():
    d = os.environ.get("DASH_RUN_DIR")
    if d:
        return d
    d = os.path.dirname(os.path.abspath(CONFIG)) or "."
    if os.access(d, os.W_OK):
        return d
    return tempfile.gettempdir()


# ---------- helpers ----------

def load_config():
    with open(CONFIG) as f:
        cfg = json.load(f)
    cfg.setdefault("services", [])
    return cfg

def save_config(cfg):
    tmp = CONFIG + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except Exception:
        pass

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)

def get_unit_states(names):
    """One systemctl call for all units -> {name: (ActiveState, UnitFileState)}."""
    states = {}
    if not names:
        return states
    rc, out, err = run("systemctl show -p Id -p ActiveState -p UnitFileState " + " ".join(names), timeout=15)
    cur = {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            unit = cur.get("Id", "")
            if unit:
                if unit.endswith(".service"):
                    unit = unit[:-8]
                states[unit] = (cur.get("ActiveState", "unknown"), cur.get("UnitFileState", ""))
            cur = {}
            continue
        key, _, val = line.partition("=")
        cur[key] = val
    return states

def get_services(with_creds=False):
    cfg = load_config()
    protected = set(cfg.get("protected") or [])
    states = get_unit_states([s["name"] for s in cfg["services"]])
    result = []
    for s in cfg["services"]:
        st, en = states.get(s["name"], ("unknown", ""))
        status = st if st in ("active", "inactive", "failed", "activating") else "unknown"
        result.append({
            "name": s["name"], "label": s["label"], "port": s.get("port"),
            "url": s.get("url"), "icon": s.get("icon", "PKG"), "desc": s.get("desc", ""),
            "safe": bool(s.get("safe", True)) and s["name"] not in protected,
            "status": status,
            "enabled": en.startswith("enabled"),
            "creds": s.get("creds") if with_creds else None,
            "has_creds": bool(s.get("creds")),
        })
    return result

def _meminfo():
    info = {}
    with open('/proc/meminfo') as f:
        for line in f:
            key, _, val = line.partition(':')
            info[key] = int(val.split()[0])
    return info

def _mem_str():
    info = _meminfo()
    total = info.get('MemTotal', 0)
    avail = info.get('MemAvailable', info.get('MemFree', 0))
    return "%d/%d MB" % ((total - avail) // 1024, total // 1024)

def _human_bytes(n):
    g = n / (1024.0 ** 3)
    if g >= 1:
        return "%.1fG" % g
    return "%.0fM" % (n / (1024.0 ** 2))

def _disk_str(path):
    try:
        st = os.statvfs(path)
    except Exception:
        return "N/A"
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    return "%s/%s" % (_human_bytes(total - free), _human_bytes(total))

def _uptime_str():
    try:
        with open('/proc/uptime') as f:
            secs = int(float(f.read().split()[0]))
    except Exception:
        return "N/A"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "up %d days, %d hours" % (days, hours)
    if hours:
        return "up %d hours, %d min" % (hours, mins)
    return "up %d min" % mins

def _load_str():
    try:
        with open('/proc/loadavg') as f:
            return " ".join(f.read().split()[:3])
    except Exception:
        return "N/A"

def get_cpu_percent(window=0.3):
    def read_cpu():
        with open('/proc/stat') as f:
            line = f.readline()
        parts = line.split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    idle1, total1 = read_cpu()
    time.sleep(window)
    idle2, total2 = read_cpu()
    d_total = total2 - total1
    if d_total == 0:
        return 0.0
    return round(100.0 * (1 - (idle2 - idle1) / d_total), 1)

def current_cpu():
    """Latest 15 s sampler average, i.e. the value on the CPU chart."""
    with HISTORY_LOCK:
        if HISTORY and time.time() - HISTORY[-1]['t'] <= HIST_INTERVAL * 2:
            return HISTORY[-1]['cpu']
    return get_cpu_percent(1.0)

def get_sysinfo():
    cfg = load_config()
    disk_path = cfg.get("disk_path") or "/"
    rc, ip, _ = run("hostname -I")
    ips = [i for i in ip.split() if ':' not in i and not i.startswith("127.")]
    lan_ip = next((i for i in ips if i.startswith("192.168.")), None)
    if not lan_ip:
        lan_ip = next((i for i in ips if not i.startswith("100.")), None)
    if not lan_ip:
        lan_ip = _primary_ip()
    ts_ip = next((i for i in ips if i.startswith("100.")), "N/A")
    return {"hostname": socket.gethostname(), "ip": lan_ip, "tailscale_ip": ts_ip,
            "uptime": _uptime_str(), "mem": _mem_str(), "disk": _disk_str(disk_path),
            "disk_mount": disk_path,
            "load": _load_str(),
            "cpu": current_cpu(), "cpu_now": get_cpu_percent(1.0)}


def scan_services():
    """List listening TCP ports and the process owning each one."""
    cfg = load_config()
    known = {s.get("port") for s in cfg["services"] if s.get("port")}
    rc, out, err = run("ss -H -ltnp 2>/dev/null")
    found = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        m = re.match(r'^(.*):(\d+)$', fields[3])
        if not m:
            continue
        port = int(m.group(2))
        if port in found or port == PORT:
            continue
        pm = re.search(r'users:\(\("([^"]+)"', line)
        found[port] = pm.group(1) if pm else "unknown"
    return [{"port": p, "process": found[p], "already_added": p in known}
            for p in sorted(found)]


# ---------- first-run detection ----------

PROTECTED_DEFAULT = [
    "ssh", "sshd", "systemd-networkd", "systemd-resolved", "systemd-logind",
    "systemd-journald", "systemd-udevd", "dbus", "dbus-broker", "networking",
    "NetworkManager", "iwd", "wpa_supplicant", "dashboard",
]

UNIT_LABELS = {
    "ssh": "SSH", "sshd": "SSH", "nginx": "nginx", "smbd": "Samba", "nmbd": "NetBIOS",
    "cups": "CUPS", "avahi-daemon": "mDNS (Avahi)", "tailscaled": "Tailscale",
    "docker": "Docker", "jellyfin": "Jellyfin", "filebrowser": "FileBrowser",
    "qbittorrent": "qBittorrent", "qbittorrent-nox": "qBittorrent",
    "transmission-daemon": "Transmission", "deluge-daemon": "Deluge",
    "syncthing": "Syncthing", "vaultwarden": "Vaultwarden", "adguardhome": "AdGuard Home",
    "pihole-FTL": "Pi-hole", "prometheus": "Prometheus", "grafana-server": "Grafana",
    "node-exporter": "node_exporter", "netdata": "Netdata", "glances": "Glances",
    "postgresql": "PostgreSQL", "mariadb": "MariaDB",
    "mysql": "MySQL", "redis-server": "Redis", "cockpit": "Cockpit",
    "plexmediaserver": "Plex", "samba": "Samba", "code-server": "code-server",
    "fail2ban": "fail2ban", "privoxy": "privoxy", "tor": "Tor",
    "apache2": "Apache", "httpd": "HTTP server", "lighttpd": "lighttpd",
    "minidlna": "MiniDLNA", "navidrome": "Navidrome", "uptime-kuma": "Uptime Kuma",
    "sonarr": "Sonarr", "radarr": "Radarr", "lidarr": "Lidarr", "prowlarr": "Prowlarr",
    "bazarr": "Bazarr", "jackett": "Jackett", "nzbget": "NZBGet", "sabnzbd": "SABnzbd",
    "home-assistant": "Home Assistant", "octoprint": "OctoPrint",
    "zigbee2mqtt": "Zigbee2MQTT", "mosquitto": "MQTT (Mosquitto)",
    "photoprism": "PhotoPrism", "immich": "Immich", "gitea": "Gitea",
    "gitlab-runner": "GitLab Runner", "portainer": "Portainer",
    "openmediavault": "OpenMediaVault", "nextcloud": "Nextcloud",
    "wireguard": "WireGuard", "openvpn": "OpenVPN", "ftp": "FTP",
    "vsftpd": "FTP", "proftpd": "FTP", "ntp": "NTP", "chrony": "NTP",
}

HTTP_UNITS = {
    "nginx", "apache2", "httpd", "lighttpd", "jellyfin", "filebrowser",
    "qbittorrent", "qbittorrent-nox", "transmission-daemon", "deluge-daemon",
    "vaultwarden", "adguardhome", "pihole-FTL", "cockpit", "grafana-server",
    "prometheus", "code-server", "syncthing", "plexmediaserver", "minidlna",
    "navidrome", "uptime-kuma", "netdata", "glances", "sonarr", "radarr",
    "lidarr", "prowlarr", "bazarr", "jackett", "nzbget", "sabnzbd",
    "home-assistant", "octoprint", "zigbee2mqtt", "photoprism", "immich",
    "gitea", "portainer", "openmediavault", "nextcloud",
}

HTTPS_PORTS = {443, 8443, 9443, 4443}


def _unit_for_pid(pid):
    try:
        with open("/proc/%d/cgroup" % int(pid)) as f:
            for line in f:
                tail = line.rsplit("/", 1)[-1].strip()
                if tail.endswith(".service"):
                    return tail[:-8]
    except Exception:
        pass
    return None


def _primary_ip():
    """Address other machines on the LAN can reach (not Tailscale, not loopback)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    rc, out, _ = run("hostname -I")
    for ip in (out or "").split():
        if ":" not in ip and not ip.startswith("127.") and not ip.startswith("100."):
            return ip
    return socket.gethostname()


def _parse_ss(out, found):
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        m = re.match(r'^(.*):(\d+)$', fields[3])
        if not m:
            continue
        host, port = m.group(1), int(m.group(2))
        pm = re.search(r'pid=(\d+)', line)
        unit = _unit_for_pid(pm.group(1)) if pm else None
        if port not in found or (found[port][0] is None and unit):
            found[port] = (unit, host)


def _listening():
    """port -> (systemd unit, bind address) for every listening TCP socket."""
    found = {}
    rc, out, _ = run("ss -H -ltnp 2>/dev/null")
    _parse_ss(out, found)
    # A normal user only sees PIDs they own; retry with passwordless sudo.
    if any(u is None for u, h in found.values()) and hasattr(os, "geteuid") and os.geteuid() != 0:
        rc, out, _ = run("sudo -n ss -H -ltnp 2>/dev/null")
        if rc == 0:
            _parse_ss(out, found)
    return found


def _reachable_bind(host):
    """True when the socket is reachable from the LAN, not loopback-only."""
    h = host.split("%")[0].strip("[]")
    return not (h.startswith("127.") or h in ("::1", "localhost", ""))


def detect_services(port):
    by_unit = {}
    for p, (unit, host) in _listening().items():
        if not unit or unit in ("dashboard",) or p == int(port):
            continue
        if not _reachable_bind(host):
            continue
        by_unit.setdefault(unit, []).append(p)
    host_ip = _primary_ip()
    services = []
    for unit, ports in sorted(by_unit.items(), key=lambda kv: min(kv[1])):
        ports = sorted(set(ports))
        # Prefer registered ports; torrent/peer-relay ports are ephemeral noise.
        low = [p for p in ports if p < 32768]
        ports = low or [ports[0]]
        label = UNIT_LABELS.get(unit) or unit.replace("-", " ").replace("_", " ").title()
        icon = re.sub(r"[^A-Za-z]", "", label)[:3].upper() or "APP"
        url = ""
        desc = "listening on " + ", ".join(str(p) for p in ports)
        shown_port = ports[0]
        if shown_port >= 32768:      # ephemeral: torrent/peer socket, not a service port
            shown_port = None
            desc = "system service"
        if unit in HTTP_UNITS and shown_port:
            https = shown_port in HTTPS_PORTS
            scheme = "https" if https else "http"
            default = (https and shown_port == 443) or (not https and shown_port == 80)
            url = ("%s://%s" % (scheme, host_ip) if default
                   else "%s://%s:%d" % (scheme, host_ip, shown_port))
        services.append({
            "name": unit, "label": label, "port": shown_port, "url": url, "icon": icon,
            "desc": desc,
            "safe": unit not in PROTECTED_DEFAULT,
        })
    return services


def detect_config(port=7979, title=None, pin=""):
    return {
        "title": title or socket.gethostname(),
        "subtitle": "System dashboard",
        "pin": pin,
        "port": int(port),
        "bind": "0.0.0.0",
        "disk_path": "/",
        "net_iface": "",
        "history_minutes": 30,
        "protected": list(PROTECTED_DEFAULT),
        "services": detect_services(port),
    }


# ---------- sampled history (default: 30 minutes) ----------

HISTORY = []
HISTORY_LOCK = threading.Lock()
HIST_MINUTES = 30           # graph window, user configurable (10 .. 1440)
HIST_MAX = 120              # points kept per graph
HIST_INTERVAL = 15          # seconds between samples (derived from HIST_MINUTES)
HIST_WAKE = threading.Event()
HIST_FILE = os.path.join(_run_dir(), "dashboard_history.json")
HIST_MIN_OPTIONS = (10, 30, 60, 180, 360, 720, 1440)


def _disk_path():
    try:
        return load_config().get("disk_path") or "/"
    except Exception:
        return "/"


def _hist_interval(minutes):
    return max(2, int(round(minutes * 60.0 / HIST_MAX)))


def _read_cpu_times():
    with open('/proc/stat') as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def _read_mem_percent():
    try:
        info = _meminfo()
    except Exception:
        return 0.0
    total = info.get('MemTotal', 0)
    avail = info.get('MemAvailable', info.get('MemFree', 0))
    if not total:
        return 0.0
    return round(100.0 * (total - avail) / total, 1)


def _read_disk_percent(path='/'):
    try:
        st = os.statvfs(path)
    except Exception:
        return 0.0
    if not st.f_blocks:
        return 0.0
    return round(100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks, 1)


NET_CLASS = '/sys/class/net'


def _iface_names():
    try:
        return sorted(n for n in os.listdir(NET_CLASS)
                      if os.path.isdir(os.path.join(NET_CLASS, n)))
    except Exception:
        return []


def _default_iface():
    """Interface carrying the default route, else the first non-loopback one."""
    try:
        rc, out, _ = run("ip route show default 2>/dev/null")
        m = re.search(r'\bdev\s+(\S+)', out or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    names = [n for n in _iface_names() if n != 'lo']
    return names[0] if names else 'eth0'


def _net_iface():
    """Interface to graph: config override ('all', a name) or auto (default route)."""
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    want = str(cfg.get("net_iface") or "").strip()
    if want == "all":
        return "all"
    if want and want in _iface_names():
        return want
    return _default_iface()


def _iface_is_wireless(name):
    return os.path.isdir(os.path.join(NET_CLASS, name, 'wireless'))


def _iface_bytes(name):
    try:
        base = os.path.join(NET_CLASS, name, 'statistics')
        with open(os.path.join(base, 'rx_bytes')) as f:
            rx = int(f.read())
        with open(os.path.join(base, 'tx_bytes')) as f:
            tx = int(f.read())
        return rx, tx
    except Exception:
        return None


def _read_net_bytes(iface=None):
    """rx/tx byte totals for one interface or for all physical ones."""
    iface = iface or _net_iface()
    if iface != 'all':
        return _iface_bytes(iface)
    rx = tx = 0
    seen = False
    for name in _iface_names():
        if name == 'lo' or name.startswith(('docker', 'veth', 'br-', 'virbr', 'vnet')):
            continue
        pair = _iface_bytes(name)
        if pair:
            rx += pair[0]
            tx += pair[1]
            seen = True
    return (rx, tx) if seen else None


def _iface_ip(name):
    try:
        rc, out, _ = run("ip -4 -o addr show dev " + name + " 2>/dev/null")
        m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', out or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def _iface_info():
    default = _default_iface()
    interfaces = []
    for name in _iface_names():
        if name == 'lo':
            continue
        state = ""
        mac = ""
        try:
            with open(os.path.join(NET_CLASS, name, 'operstate')) as f:
                state = f.read().strip()
        except Exception:
            pass
        try:
            with open(os.path.join(NET_CLASS, name, 'address')) as f:
                mac = f.read().strip()
        except Exception:
            pass
        interfaces.append({
            "name": name,
            "state": state or "unknown",
            "ip": _iface_ip(name),
            "mac": mac,
            "wireless": _iface_is_wireless(name),
            "default": name == default,
        })
    try:
        cfg_want = str(load_config().get("net_iface") or "")
    except Exception:
        cfg_want = ""
    return {"interfaces": interfaces, "default": default,
            "selected": cfg_want, "resolved": _net_iface(),
            "options": list(HIST_MIN_OPTIONS), "minutes": HIST_MINUTES}


def _save_history():
    try:
        tmp = HIST_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(HISTORY, f)
        os.replace(tmp, HIST_FILE)
    except Exception:
        pass


def _load_history():
    cutoff = int(time.time()) - HIST_INTERVAL * HIST_MAX
    try:
        with open(HIST_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            fresh = [p for p in data
                     if isinstance(p, dict) and isinstance(p.get('t'), int) and p['t'] >= cutoff]
            return fresh[-HIST_MAX:]
    except Exception:
        pass
    return []


def reset_history():
    """Drop samples (window/interface changed) and let the sampler run again."""
    global HISTORY
    with HISTORY_LOCK:
        HISTORY = []
        _save_history()
    HIST_WAKE.set()


def apply_history_minutes(minutes):
    """Change the graph window; returns the effective value."""
    global HIST_MINUTES, HIST_INTERVAL
    minutes = max(HIST_MIN_OPTIONS[0], min(HIST_MIN_OPTIONS[-1], int(minutes)))
    interval = _hist_interval(minutes)
    if minutes == HIST_MINUTES and interval == HIST_INTERVAL:
        return HIST_MINUTES
    HIST_MINUTES = minutes
    HIST_INTERVAL = interval
    reset_history()
    return HIST_MINUTES


def seed_history():
    global HISTORY
    if HISTORY:
        return
    point = {"t": int(time.time()), "cpu": get_cpu_percent(),
             "mem": _read_mem_percent(), "disk": _read_disk_percent(_disk_path()),
             "rx": 0.0, "tx": 0.0}
    with HISTORY_LOCK:
        HISTORY = [point]
        _save_history()


def history_loop():
    global HISTORY
    prev_idle, prev_total = _read_cpu_times()
    prev_iface = _net_iface()
    prev_net = _read_net_bytes(prev_iface)
    prev_t = time.time()
    while True:
        HIST_WAKE.wait(max(1, HIST_INTERVAL))
        HIST_WAKE.clear()
        now = time.time()
        dt = now - prev_t
        idle, total = _read_cpu_times()
        d_total = total - prev_total
        d_idle = idle - prev_idle
        cpu = round(100.0 * (1 - d_idle / d_total), 1) if d_total > 0 else 0.0
        prev_idle, prev_total = idle, total
        iface = _net_iface()
        net = _read_net_bytes(iface)
        rx = tx = 0.0
        if iface != prev_iface:
            prev_net, prev_iface = net, iface
        elif net and prev_net and dt >= 2:
            rx = round(max(0, net[0] - prev_net[0]) / dt / 1024.0, 1)
            tx = round(max(0, net[1] - prev_net[1]) / dt / 1024.0, 1)
        if net:
            prev_net = net
        prev_t = now
        point = {"t": int(now), "cpu": cpu, "mem": _read_mem_percent(),
                 "disk": _read_disk_percent(_disk_path()), "rx": rx, "tx": tx}
        with HISTORY_LOCK:
            HISTORY = (HISTORY + [point])[-HIST_MAX:]
            _save_history()


def get_history():
    with HISTORY_LOCK:
        return {"interval": HIST_INTERVAL, "minutes": HIST_MINUTES,
                "span": HIST_INTERVAL * HIST_MAX, "net_iface": _net_iface(),
                "points": list(HISTORY)}


# ---------- PIN gate (server side) ----------

TOKENS = {}
TOKENS_LOCK = threading.Lock()
TOKEN_TTL = 12 * 3600
PIN_FAILS = {}
PIN_FAIL_LOCK = threading.Lock()
PIN_MAX_FAILS = 8
PIN_FAIL_WINDOW = 300


def new_token():
    tok = binascii.hexlify(os.urandom(24)).decode()
    now = time.time()
    with TOKENS_LOCK:
        for k in [k for k, v in TOKENS.items() if v < now]:
            TOKENS.pop(k, None)
        TOKENS[tok] = now + TOKEN_TTL
    return tok


def token_ok(tok):
    if not tok:
        return False
    with TOKENS_LOCK:
        exp = TOKENS.get(tok)
        if exp and exp > time.time():
            return True
        TOKENS.pop(tok, None)
    return False


def pin_throttled(ip):
    now = time.time()
    with PIN_FAIL_LOCK:
        fails = [t for t in PIN_FAILS.get(ip, []) if now - t < PIN_FAIL_WINDOW]
        PIN_FAILS[ip] = fails
        return len(fails) >= PIN_MAX_FAILS


def pin_fail(ip):
    with PIN_FAIL_LOCK:
        PIN_FAILS.setdefault(ip, []).append(time.time())


def pin_ok(given):
    try:
        stored = load_config().get('pin')
    except Exception:
        stored = None
    if not stored:
        return False  # no PIN configured: keep the console locked
    return str(given) == str(stored)


def sanitize_cfg(cfg, with_creds):
    out = {k: v for k, v in cfg.items() if k != 'pin'}
    out['services'] = []
    for s in cfg.get('services', []):
        c = dict(s)
        if not with_creds:
            c.pop('creds', None)
        out['services'].append(c)
    return out


# ---------- web console ----------

TERMS = {}
TERMS_LOCK = threading.Lock()
TERM_MAX = 4
TERM_IDLE = 900
TERM_BUF_MAX = 160000
TERM_LOG = os.path.join(_run_dir(), 'dashboard_term.log')
TERM_PS1 = '\\u@\\h:\\w\\$ '

ANSI_RE = re.compile(
    r'\x1b\[[0-?]*[ -/]*[@-~]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[@-Z\\-_]'
)
CTRL_RE = re.compile(r'[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]')


def clean_output(text):
    return CTRL_RE.sub('', ANSI_RE.sub('', text))


class TermSession:
    def __init__(self, cols=100, rows=30):
        self.id = uuid.uuid4().hex[:12]
        self.cols = max(20, min(300, int(cols)))
        self.rows = max(10, min(120, int(rows)))
        self.lock = threading.RLock()
        self.wlock = threading.Lock()
        self.dec = codecs.getincrementaldecoder('utf-8')('replace')
        self.chunks = []
        self.base = 0
        self.total = 0
        self.dead = False
        self.last = time.time()
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir('/root')
                env = dict(os.environ)
                env.update({'TERM': 'xterm-256color', 'LANG': 'C.UTF-8',
                            'PS1': TERM_PS1, 'HISTFILE': '/root/.bash_history'})
                os.execve('/bin/bash', ['/bin/bash', '--noprofile', '--norc', '-i'], env)
            except Exception:
                pass
            os._exit(1)
        self.pid = pid
        self.fd = fd
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack('HHHH', self.rows, self.cols, 0, 0))
        except Exception:
            pass
        fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)

    def _append(self, text):
        if not text:
            return
        self.chunks.append(text)
        self.total += len(text)
        while self.total - self.base > TERM_BUF_MAX and len(self.chunks) > 1:
            self.base += len(self.chunks.pop(0))

    def note_death(self, why):
        self.dead = True
        self.reason = why
        try:
            try:
                if os.path.getsize(TERM_LOG) >= 20000:
                    return
            except OSError:
                pass
            with open(TERM_LOG, 'a') as f:
                f.write('%d %s %s\n' % (int(time.time()), self.id, why))
        except Exception:
            pass

    def pump(self):
        with self.lock:
            while True:
                try:
                    data = os.read(self.fd, 32768)
                except (BlockingIOError, OSError) as e:
                    if not isinstance(e, BlockingIOError):
                        self.note_death('read-error:%r' % (e,))
                    break
                if not data:
                    self.note_death('eof')
                    break
                self._append(clean_output(self.dec.decode(data)))
            if not self.dead:
                try:
                    wpid, status = os.waitpid(self.pid, os.WNOHANG)
                    if wpid == self.pid:
                        self.note_death('exited:%d' % status)
                except ChildProcessError:
                    self.note_death('already-reaped')

    def read(self, since):
        self.pump()
        self.last = time.time()
        with self.lock:
            if since < self.base:
                since = self.base
            if since > self.total:
                since = self.total
            return ''.join(self.chunks)[since - self.base:], self.total, not self.dead

    def write(self, data):
        self.last = time.time()
        buf = data.encode('utf-8')
        deadline = time.time() + 5.0
        with self.wlock:
            while buf:
                try:
                    n = os.write(self.fd, buf)
                    buf = buf[n:]
                    continue
                except BlockingIOError:
                    if time.time() > deadline:
                        self.note_death('write-timeout data=%r' % (data[:40],))
                        return
                    try:
                        select.select([], [self.fd], [], 0.05)
                    except OSError as e:
                        self.note_death('write-select:%r' % (e,))
                        return
                except OSError as e:
                    self.note_death('write-error:%r data=%r' % (e, data[:40]))
                    return

    def resize(self, cols, rows):
        self.cols = max(20, min(300, int(cols)))
        self.rows = max(10, min(120, int(rows)))
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack('HHHH', self.rows, self.cols, 0, 0))
        except Exception:
            pass

    def close(self):
        self.note_death('closed-by-server')
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            os.close(self.fd)
        except Exception:
            pass
        self.dead = True


def term_start(cols, rows):
    s = TermSession(cols, rows)
    with TERMS_LOCK:
        for sid in [sid for sid, t in TERMS.items() if t.dead]:
            old = TERMS.pop(sid, None)
            if old:
                old.close()
        if len(TERMS) >= TERM_MAX:
            old = sorted(TERMS.values(), key=lambda x: x.last)[0]
            TERMS.pop(old.id, None)
            old.close()
        TERMS[s.id] = s
    return s


def term_get(sid):
    with TERMS_LOCK:
        return TERMS.get(sid)


def term_drop(sid):
    with TERMS_LOCK:
        s = TERMS.pop(sid, None)
    if s:
        s.close()


def term_reap_loop():
    while True:
        time.sleep(30)
        now = time.time()
        with TERMS_LOCK:
            stale = [sid for sid, s in TERMS.items() if now - s.last > TERM_IDLE]
        for sid in stale:
            term_drop(sid)


# ---------- HTML ----------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Linux Dashboard</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--accent:#38bdf8;--text:#e2e8f0;--muted:#94a3b8;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;--border:#334155}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);padding:20px}
  h1{font-size:1.5rem;margin-bottom:2px}
  .sub{color:var(--muted);font-size:.85rem;margin-bottom:20px}
  .sysinfo{background:var(--card);border-radius:12px;padding:16px;margin-bottom:20px;border:1px solid var(--border);display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}
  .sysinfo .si{font-size:.8rem}.sysinfo .si span:first-child{color:var(--muted);display:block;font-size:.7rem}.sysinfo .si span:last-child{font-weight:600}
  .sysinfo .si em{color:var(--muted);font-style:normal;font-weight:400}
  .top-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
  .top-bar button{padding:8px 16px;font-size:.85rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
  .card{background:var(--card);border-radius:12px;padding:18px;border:1px solid var(--border);transition:transform .15s,border-color .15s}
  .card:hover{transform:translateY(-2px);border-color:var(--accent)}
  .card-head{display:flex;align-items:center;gap:10px;margin-bottom:8px}
  .card-head .icon{font-size:1.4rem}
  .card-head h2{font-size:1.05rem;flex:1}
  .port{font-family:monospace;color:var(--accent);font-size:.85rem}
  .desc{color:var(--muted);font-size:.8rem;margin:6px 0 10px}
  .status{display:flex;align-items:center;gap:6px;font-size:.8rem;margin-bottom:8px}
  .dot{width:8px;height:8px;border-radius:50%}
  .dot.on{background:var(--green)}.dot.off{background:var(--red)}.dot.warn{background:var(--amber)}
  .actions{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
  .btn{padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:#0f172a;color:var(--text);cursor:pointer;font-size:.75rem;font-weight:600;transition:background .15s}
  .btn:hover{background:#334155}.btn.start{border-color:var(--green);color:var(--green)}.btn.stop{border-color:var(--red);color:var(--red)}.btn.restart{border-color:var(--amber);color:var(--amber)}.btn:disabled{opacity:.4;cursor:not-allowed}.btn.cred{border-color:var(--accent);color:var(--accent)}.btn.scan{border-color:var(--accent);color:var(--accent);background:var(--accent);color:#0f172a}.btn.reboot{border-color:var(--red);color:var(--red);background:var(--red);color:#fff}
  .btn.danger{border-color:var(--red);color:var(--red)}
  .card a{color:var(--accent);text-decoration:none;font-size:.8rem;font-weight:500}
  .card a:hover{text-decoration:underline}
  .meta{font-size:.75rem;color:var(--muted);margin-top:4px}
  .meta b{color:var(--text)}
  .cred-box{background:#0f172a;border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin-top:8px;font-size:.8rem;display:none}
  .cred-box.show{display:block}
  .cred-box .row{display:flex;justify-content:space-between;padding:2px 0}
  .cred-box .row span:first-child{color:var(--muted)}
  .cred-box .row span:last-child{font-family:monospace;color:var(--accent)}
  .cred-box .row input{width:100%;padding:4px 6px;margin:2px 0;background:var(--card);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:.8rem}
  .cred-box .row button{margin-top:4px}
  .footer{text-align:center;color:var(--muted);font-size:.75rem;margin-top:24px}
  .toast{position:fixed;top:16px;right:16px;padding:10px 16px;border-radius:8px;font-size:.85rem;display:none;z-index:99}
  .toast.ok{background:#166534;color:#bbf7d0;display:block}
  .toast.err{background:#7f1d1d;color:#fecaca;display:block}
  .pin-modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:200}
  .pin-modal.show{display:flex}
  .pin-box{background:var(--card);border-radius:12px;padding:24px;border:1px solid var(--border);text-align:center;min-width:280px}
  .pin-box h3{margin-bottom:4px}
  .pin-box p{color:var(--muted);font-size:.85rem;margin-bottom:16px}
  .pin-input{display:flex;gap:10px;justify-content:center;margin-bottom:12px}
  .pin-input input{width:44px;height:52px;text-align:center;font-size:1.3rem;font-family:monospace;background:#0f172a;border:2px solid var(--border);border-radius:8px;color:var(--text);outline:none}
  .pin-input input:focus{border-color:var(--accent)}
  .pin-error{color:var(--red);font-size:.8rem;display:none;margin-bottom:8px}
  .scan-results{margin-bottom:16px}
  .scan-item{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:#0f172a;border-radius:6px;font-size:.8rem;margin-bottom:4px}
  .scan-item .si-left{display:flex;align-items:center;gap:8px}
  .scan-item .si-port{font-family:monospace;color:var(--accent)}
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner{display:inline-block;width:12px;height:12px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite;margin-left:6px;vertical-align:middle}
  .charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-bottom:20px}
  .chart-card{background:var(--card);border-radius:12px;padding:12px 14px;border:1px solid var(--border)}
  .chart-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:8px}
  .chart-head h2{font-size:.9rem;font-weight:600}
  .chart-head .chart-now{font-size:.78rem;color:var(--accent);font-family:monospace;white-space:nowrap}
  .chart-head .mini{margin-left:auto;max-width:170px}
  .ctl{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:.75rem}
  .ctl select,.mini{background:#0f172a;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 7px;font-size:.75rem;font-family:inherit;outline:none}
  .ctl select:focus,.mini:focus{border-color:var(--accent)}
  .chart-card canvas{width:100%;height:90px;display:block;border-radius:6px}
  .chart-legend{display:flex;gap:12px;font-size:.7rem;color:var(--muted);margin-top:6px;flex-wrap:wrap}
  .chart-legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}
  .tabs{display:flex;gap:4px;margin:0 0 16px;border-bottom:1px solid var(--border)}
  .tabs .tab{background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);padding:8px 14px;font-size:.9rem;font-weight:600;cursor:pointer}
  .tabs .tab.active{color:var(--text);border-bottom-color:var(--accent)}
  .term-out{background:#000;color:#dbe6f2;border:1px solid var(--border);border-radius:8px;padding:10px;height:62vh;overflow:auto;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word;margin:0}
  .term-in{width:100%;margin-top:8px;padding:10px 12px;background:#0f172a;border:1px solid var(--accent);border-radius:8px;color:var(--text);font:13px ui-monospace,Menlo,Consolas,monospace;outline:none}
  .term-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
  .term-keys{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
  .term-note{color:var(--muted);font-size:.72rem}
  .badge{font-size:.72rem;padding:2px 8px;border-radius:10px;background:#334155;color:var(--muted)}
  .badge.on{background:#14532d;color:#bbf7d0}
</style>
</head>
<body>
<h1 id="title">Dashboard</h1>
<p class="sub" id="subtitle"></p>

<div class="tabs">
  <button class="tab active" id="tabDash" onclick="showTab('dash')">Dashboard</button>
  <button class="tab" id="tabTerm" onclick="showTab('term')">Terminal</button>
</div>

<div id="view-dash">

<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center;">
  <button class="btn" id="refreshBtn" style="padding:8px 16px;font-size:.85rem;" onclick="refreshAll(true)">Refresh</button>
  <span id="lastUpdated" style="color:var(--muted);font-size:.75rem;"></span>
  <label class="ctl">Graph
    <select id="rangeSel" onchange="setRange(this.value)"></select>
  </label>
  <button class="btn scan" style="padding:8px 16px;font-size:.85rem;" onclick="scanServices()">Scan for Services</button>
  <button class="btn reboot" style="padding:8px 16px;font-size:.85rem;margin-left:auto;" onclick="rebootBoard()">Reboot</button>
</div>

<div class="scan-results" id="scanResults" style="margin-bottom:16px;"></div>

<div class="sysinfo" id="sysinfo"><div style="color:var(--muted);padding:10px;">Loading...</div></div>

<div class="charts" id="charts">
  <div class="chart-card">
    <div class="chart-head"><h2>CPU</h2><span class="chart-now" id="now-cpu">&ndash;</span></div>
    <canvas id="chart-cpu"></canvas>
    <div class="chart-legend"><span class="span-label">last 30 min</span></div>
  </div>
  <div class="chart-card">
    <div class="chart-head"><h2>RAM</h2><span class="chart-now" id="now-mem">&ndash;</span></div>
    <canvas id="chart-mem"></canvas>
    <div class="chart-legend"><span class="span-label">last 30 min</span></div>
  </div>
  <div class="chart-card">
    <div class="chart-head"><h2 id="disk-title">Disk</h2><span class="chart-now" id="now-disk">&ndash;</span></div>
    <canvas id="chart-disk"></canvas>
    <div class="chart-legend"><span class="span-label">last 30 min</span></div>
  </div>
  <div class="chart-card">
    <div class="chart-head"><h2>Network RX/TX</h2>
      <select id="netSel" class="mini" onchange="setIface(this.value)" title="Interface to graph"></select>
      <span class="chart-now" id="now-net">&ndash;</span></div>
    <canvas id="chart-net"></canvas>
    <div class="chart-legend"><span><i style="background:#38bdf8"></i>RX</span><span><i style="background:#f59e0b"></i>TX</span><span>KB/s &middot; <span class="span-label">last 30 min</span></span></div>
  </div>
</div>

<div class="grid" id="grid"><div style="color:var(--muted);padding:20px;">Loading services...</div></div>

</div><!-- /view-dash -->

<div id="view-term" style="display:none">
  <div class="term-bar">
    <span class="badge" id="termState">disconnected</span>
    <button class="btn" onclick="termConnect(true)">Connect</button>
    <button class="btn danger" onclick="termClose()">Close</button>
    <span class="term-note">root shell &middot; full-screen apps (htop, vim, nano) will not render here</span>
  </div>
  <pre id="termOut" class="term-out"></pre>
  <div class="term-keys">
    <button class="btn" onclick="termSend('\t')">Tab</button>
    <button class="btn" onclick="termSend('\x1b[A')">&uarr;</button>
    <button class="btn" onclick="termSend('\x1b[B')">&darr;</button>
    <button class="btn" onclick="termSend('\x1b[D')">&larr;</button>
    <button class="btn" onclick="termSend('\x1b[C')">&rarr;</button>
    <button class="btn" onclick="termSend('\x03')">Ctrl+C</button>
    <button class="btn" onclick="termSend('\x04')">Ctrl+D</button>
    <button class="btn" onclick="termClearScreen()">Clear</button>
  </div>
  <input id="termIn" class="term-in" placeholder="type a command and press Enter" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
</div>


<div class="pin-modal" id="pinModal">
  <div class="pin-box">
    <h3>Enter PIN</h3>
    <p id="pinPurpose">Enter 4-digit PIN</p>
    <div class="pin-input">
      <input type="password" maxlength="1" inputmode="numeric" id="pp1">
      <input type="password" maxlength="1" inputmode="numeric" id="pp2">
      <input type="password" maxlength="1" inputmode="numeric" id="pp3">
      <input type="password" maxlength="1" inputmode="numeric" id="pp4">
    </div>
    <div class="pin-error" id="pinModalError">Incorrect PIN</div>
    <button class="btn" style="margin-top:8px;" onclick="cancelPin()">Cancel</button>
  </div>
</div>

<div class="pin-modal" id="credEditModal">
  <div class="pin-box" style="min-width:320px">
    <h3>Edit Credentials</h3>
    <p id="credEditLabel"></p>
    <div style="text-align:left">
      <input type="text" id="credUser" placeholder="Username" style="width:100%;padding:6px 8px;margin:4px 0;background:#0f172a;border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:.85rem">
      <input type="text" id="credPass" placeholder="Password" style="width:100%;padding:6px 8px;margin:4px 0;background:#0f172a;border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:.85rem">
      <div style="display:flex;gap:8px;margin-top:8px;">
        <button class="btn start" onclick="saveCreds()">Save</button>
        <button class="btn" onclick="closeCredEdit()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let cfg = {};
let services = [];
let editingService = null;
let history = {points: [], span: 1800};
let netInfo = {interfaces: []};
let netSig = '';
let authToken = sessionStorage.getItem('dash_token') || '';
let pinVerified = !!authToken;
let currentTab = 'dash';

function authHeaders() {
  const h = {'Content-Type': 'application/json'};
  if (authToken) h['X-Dash-Token'] = authToken;
  return h;
}

async function api(path, method='GET', body=null) {
  const opts = {method, headers: authHeaders()};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (r.status === 401 && path.startsWith('/api/term')) return {ok:false, error:'locked'};
  return r.json();
}

function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (ok?'ok':'err');
  setTimeout(()=>t.className='toast', 3000);
}

async function loadConfig() {
  const r = await fetch('/api/config');
  cfg = await r.json();
  document.getElementById('title').textContent = (cfg.title || 'Dashboard') + ' __APP_VERSION__';
  document.getElementById('subtitle').textContent = cfg.subtitle || '';
}

async function loadSysinfo() {
  const d = await api('/api/sysinfo');
  const mount = d.disk_mount || '/';
  const diskTitle = document.getElementById('disk-title');
  if (diskTitle) diskTitle.textContent = 'Disk (' + mount + ')';
  document.getElementById('sysinfo').innerHTML = `
    <div class="si"><span>Hostname</span><span>${d.hostname}</span></div>
    <div class="si"><span>LAN IP</span><span>${d.ip}</span></div>
    <div class="si"><span>Tailscale IP</span><span>${d.tailscale_ip}</span></div>
    <div class="si"><span>CPU</span><span>${d.cpu_now}% now <em>&middot; ${d.cpu}% avg</em></span></div>
    <div class="si"><span>Uptime</span><span>${d.uptime}</span></div>
    <div class="si"><span>RAM</span><span>${d.mem}</span></div>
    <div class="si"><span>Disk (${mount})</span><span>${d.disk}</span></div>
    <div class="si"><span>Load</span><span>${d.load}</span></div>
  `;
}

async function loadHistory() {
  const d = await api('/api/history');
  if (d && Array.isArray(d.points)) history = d;
  const span = fmtSpan(d.span || 1800);
  document.querySelectorAll('.span-label').forEach(el => { el.textContent = span; });
  drawAllCharts();
}

function fmtSpan(sec) {
  sec = Number(sec) || 0;
  if (sec < 3600) return 'last ' + Math.max(1, Math.round(sec / 60)) + ' min';
  const h = sec / 3600;
  return 'last ' + (h % 1 ? h.toFixed(1) : h) + ' h';
}

async function loadNet() {
  let d;
  try { d = await api('/api/net'); } catch (e) { return; }
  netInfo = d;
  const sel = document.getElementById('netSel');
  if (!sel) return;
  const sig = JSON.stringify([d.selected, d.minutes,
    (d.interfaces || []).map(i => [i.name, i.ip, i.state, i.wireless])]);
  if (sig === netSig) return;
  netSig = sig;
  const cur = d.selected || '';
  sel.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = '';
  const def = (d.interfaces || []).find(i => i.name === d.default);
  auto.textContent = 'Auto' + (def ? ' · ' + def.name : '');
  sel.appendChild(auto);
  (d.interfaces || []).forEach(i => {
    const o = document.createElement('option');
    o.value = i.name;
    o.textContent = i.name + (i.wireless ? ' · Wi-Fi' : '') + (i.ip ? ' · ' + i.ip : '');
    sel.appendChild(o);
  });
  const all = document.createElement('option');
  all.value = 'all';
  all.textContent = 'All interfaces';
  sel.appendChild(all);
  sel.value = cur;
  const range = document.getElementById('rangeSel');
  if (range && !range.options.length) {
    const opts = d.options || [10, 30, 60, 180, 360, 720, 1440];
    opts.forEach(m => {
      const o = document.createElement('option');
      o.value = String(m);
      o.textContent = m < 60 ? m + ' min' : (m / 60) + ' hour' + (m >= 120 ? 's' : '');
      range.appendChild(o);
    });
    range.value = String(d.minutes || 30);
  }
}

async function setIface(v) {
  try {
    const r = await api('/api/settings', 'POST', {net_iface: v});
    if (!r.ok) throw new Error(r.error || 'failed');
    toast(v === 'all' ? 'Graphing all interfaces' : (v ? 'Graphing ' + v : 'Graphing the default interface'), true);
    await loadNet();
    await loadHistory();
  } catch (e) {
    toast('Could not change interface: ' + e.message, false);
  }
}

async function setRange(v) {
  try {
    const r = await api('/api/settings', 'POST', {history_minutes: Number(v)});
    if (!r.ok) throw new Error(r.error || 'failed');
    toast('Graph window: ' + fmtSpan(r.minutes * 60).replace('last ', ''), true);
    await loadHistory();
  } catch (e) {
    toast('Could not change graph window: ' + e.message, false);
  }
}

function fmtRate(v) {
  if (v >= 1024) return (v / 1024).toFixed(2) + ' MB/s';
  return v.toFixed(1) + ' KB/s';
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
}

function chartCtx(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(60, canvas.clientWidth || 300);
  const h = Math.max(40, canvas.clientHeight || 90);
  const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
  if (canvas.width !== pw || canvas.height !== ph) { canvas.width = pw; canvas.height = ph; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx, w, h};
}

function drawAllCharts() {
  const pts = history.points || [];
  const last = k => pts.length ? (Number(pts[pts.length - 1][k]) || 0) : 0;
  const setNow = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
  setNow('now-cpu', pts.length ? last('cpu').toFixed(1) + '%' : '\u2013');
  setNow('now-mem', pts.length ? last('mem').toFixed(1) + '%' : '\u2013');
  setNow('now-disk', pts.length ? last('disk').toFixed(1) + '%' : '\u2013');
  setNow('now-net', pts.length ? '\u2193 ' + fmtRate(last('rx')) + '  \u2191 ' + fmtRate(last('tx')) : '\u2013');
  drawChart('chart-cpu', [{k: 'cpu', c: '#38bdf8'}], {fixedMax: 100});
  drawChart('chart-mem', [{k: 'mem', c: '#22c55e'}], {fixedMax: 100});
  drawChart('chart-disk', [{k: 'disk', c: '#f59e0b'}], {fixedMax: 100});
  drawChart('chart-net', [{k: 'rx', c: '#38bdf8'}, {k: 'tx', c: '#f59e0b'}], {});
}

function drawChart(id, series, opts) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const {ctx, w, h} = chartCtx(canvas);
  const pts = (history.points || []).filter(p => typeof p.t === 'number');
  const span = history.span || 1800;
  const padL = 38, padR = 8, padT = 8, padB = 16;
  const iw = Math.max(10, w - padL - padR);
  const ih = Math.max(10, h - padT - padB);

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0f172a';
  ctx.fillRect(0, 0, w, h);

  let maxY = opts.fixedMax || 0;
  if (!maxY) {
    let peak = 0;
    pts.forEach(p => series.forEach(s => { peak = Math.max(peak, Number(p[s.k]) || 0); }));
    maxY = Math.max(10, peak * 1.15);
    const mag = Math.pow(10, Math.floor(Math.log10(maxY)));
    maxY = Math.ceil(maxY / mag) * mag;
  }

  ctx.strokeStyle = '#243244';
  ctx.lineWidth = 1;
  ctx.fillStyle = '#64748b';
  ctx.font = '9px monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const y = padT + ih * (i / steps);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
    const val = maxY * (1 - i / steps);
    ctx.fillText(opts.fixedMax ? val.toFixed(0) : Math.round(val), padL - 5, y);
  }

  const tEnd = pts.length ? pts[pts.length - 1].t : Math.floor(Date.now() / 1000);
  const tStart = tEnd - span;
  const xOf = t => padL + (Math.max(tStart, Math.min(tEnd, t)) - tStart) / span * iw;
  const yOf = v => padT + ih * (1 - Math.min(maxY, Math.max(0, Number(v) || 0)) / maxY);

  if (pts.length < 2) {
    ctx.fillStyle = '#94a3b8';
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('collecting data\u2026', padL + iw / 2, padT + ih / 2);
    return;
  }

  series.forEach(s => {
    ctx.beginPath();
    pts.forEach((p, i) => {
      const px = xOf(p.t), py = yOf(p[s.k]);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = s.c;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.stroke();

    const grad = ctx.createLinearGradient(0, padT, 0, padT + ih);
    grad.addColorStop(0, hexA(s.c, 0.25));
    grad.addColorStop(1, hexA(s.c, 0.02));
    ctx.lineTo(xOf(pts[pts.length - 1].t), padT + ih);
    ctx.lineTo(xOf(pts[0].t), padT + ih);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
  });

  ctx.fillStyle = '#64748b';
  ctx.font = '9px monospace';
  ctx.textBaseline = 'top';
  ctx.textAlign = 'left';
  ctx.fillText('-30m', padL, padT + ih + 3);
  ctx.textAlign = 'right';
  ctx.fillText('now', padL + iw, padT + ih + 3);
}

window.addEventListener('resize', drawAllCharts);

async function loadServices() {
  services = await api('/api/services');
  const grid = document.getElementById('grid');
  grid.innerHTML = services.map(s => {
    const credHtml = s.creds ? `
      <div class="cred-box ${pinVerified?'show':''}" id="cred-${s.name}">
        ${Object.entries(s.creds).map(([k,v])=>`<div class="row"><span>${k}</span><span>${v}</span></div>`).join('')}
        <button class="btn" style="margin-top:6px;font-size:.7rem" onclick="editCreds('${s.name}')">Edit</button>
      </div>
    ` : '';
    const credBtn = s.has_creds ? `<button class="btn cred" onclick="requestPin('${s.name}')">Credentials</button>` : `<button class="btn cred" onclick="addCreds('${s.name}')">+ Add Creds</button>`;
    return `
    <div class="card" id="card-${s.name}">
      <div class="card-head">
        <span class="icon">${s.icon}</span>
        <h2>${s.label}</h2>
        ${s.port ? `<span class="port">:${s.port}</span>` : ''}
      </div>
      <div class="desc">${s.desc}</div>
      <div class="status">
        <span class="dot ${s.status==='active'?'on':s.status==='inactive'?'off':'warn'}"></span>
        <span>${s.status} ${s.enabled?'(enabled)':'(disabled)'}</span>
      </div>
      ${s.url ? `<div class="meta"><a href="${s.url}" target="_blank">Open</a></div>` : ''}
      ${credHtml}
      <div class="actions">
        ${s.safe ? `
          <button class="btn start" onclick="doAction('${s.name}','start')" ${s.status==='active'?'disabled':''}>Start</button>
          <button class="btn stop" onclick="doAction('${s.name}','stop')" ${s.status!=='active'?'disabled':''}>Stop</button>
          <button class="btn restart" onclick="doAction('${s.name}','restart')">Restart</button>
          <button class="btn" onclick="doAction('${s.name}','enable')" ${s.enabled?'disabled':''} style="font-size:.7rem;">Enable</button>
          <button class="btn" onclick="doAction('${s.name}','disable')" ${!s.enabled?'disabled':''} style="font-size:.7rem;">Disable</button>
          <button class="btn" style="border-color:var(--red);color:var(--red);" onclick="removeService('${s.name}')">Remove</button>
        ` : `<span style="color:var(--muted);font-size:.75rem;">Protected</span>`}
        ${credBtn}
      </div>
    </div>
  `}).join('');
  if (pinVerified) {
    document.querySelectorAll('.cred-box').forEach(el=>el.classList.add('show'));
  }
}

let pinResolve = null;

function askPin(purpose) {
  return new Promise(resolve => {
    if (pinResolve) { const old = pinResolve; pinResolve = null; old(false); }
    pinResolve = resolve;
    const modal = document.getElementById('pinModal');
    const errEl = document.getElementById('pinModalError');
    document.getElementById('pinPurpose').textContent = purpose || 'Enter 4-digit PIN';
    errEl.style.display = 'none';
    const inputs = ['pp1','pp2','pp3','pp4'].map(id => document.getElementById(id));
    inputs.forEach(inp => { inp.value = ''; });
    modal.classList.add('show');
    modal.onclick = (e) => { if (e.target === modal) cancelPin(); };
    inputs.forEach((inp, i) => {
      inp.oninput = async () => {
        if (inp.value.length === 1 && i < 3) inputs[i+1].focus();
        const pin = inputs.map(x => x.value).join('');
        if (pin.length !== 4) return;
        let ok = false, msg = 'Incorrect PIN';
        try {
          const r = await fetch('/api/unlock', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pin})});
          const d = await r.json();
          if (d.ok && d.token) {
            authToken = d.token;
            sessionStorage.setItem('dash_token', d.token);
            pinVerified = true;
            ok = true;
          } else if (d.error === 'too many attempts') {
            msg = 'Too many attempts - wait a minute';
          }
        } catch (e) { msg = 'Cannot reach host'; }
        if (ok) { closePin(true); return; }
        errEl.textContent = msg;
        errEl.style.display = 'block';
        inputs.forEach(x => { x.value = ''; });
        inputs[0].focus();
        setTimeout(() => { errEl.style.display = 'none'; }, 2200);
      };
      inp.onkeydown = (e) => {
        if (e.key === 'Backspace' && !inp.value && i > 0) inputs[i-1].focus();
        if (e.key === 'Escape') cancelPin();
      };
    });
    setTimeout(() => inputs[0].focus(), 60);
  });
}

function closePin(ok) {
  const modal = document.getElementById('pinModal');
  modal.classList.remove('show');
  modal.onclick = null;
  const r = pinResolve;
  pinResolve = null;
  if (r) r(!!ok);
}

function cancelPin() { closePin(false); }

async function requestPin(name) {
  if (authToken) {
    try {
      const r = await fetch('/api/auth', {headers: authHeaders()});
      const d = await r.json();
      if (!d.ok) { authToken = ''; pinVerified = false; sessionStorage.removeItem('dash_token'); }
    } catch (e) { /* host not reachable; keep token */ }
  }
  if (!authToken) {
    const ok = await askPin('Enter 4-digit PIN to view credentials');
    if (!ok) { toast('Credentials locked', false); return; }
  }
  pinVerified = true;
  await loadServices();
  document.querySelectorAll('.cred-box').forEach(el => el.classList.add('show'));
  toast('Credentials unlocked');
}

function editCreds(name) {
  const svc = services.find(s=>s.name===name);
  editingService = name;
  document.getElementById('credEditLabel').textContent = 'Edit credentials for ' + svc.label;
  const creds = svc.creds || {};
  const user = creds.User || creds.user || '';
  const pass = creds.Pass || creds.pass || creds.Password || '';
  document.getElementById('credUser').value = user;
  document.getElementById('credPass').value = pass;
  document.getElementById('credEditModal').classList.add('show');
}

function addCreds(name) {
  editingService = name;
  const svc = services.find(s=>s.name===name);
  document.getElementById('credEditLabel').textContent = 'Add credentials for ' + svc.label;
  document.getElementById('credUser').value = '';
  document.getElementById('credPass').value = '';
  document.getElementById('credEditModal').classList.add('show');
}

function closeCredEdit() {
  document.getElementById('credEditModal').classList.remove('show');
  editingService = null;
}

async function saveCreds() {
  const user = document.getElementById('credUser').value.trim();
  const pass = document.getElementById('credPass').value;
  if (!user) { toast('Username required', false); return; }
  const r = await api('/api/creds/' + editingService, 'POST', {user, pass});
  if (r.ok) { toast('Credentials saved'); closeCredEdit(); await loadServices(); }
  else toast('Failed to save credentials', false);
}

async function doAction(name, action) {
  const card = document.getElementById('card-'+name);
  const btns = card.querySelectorAll('.btn');
  btns.forEach(b=>b.disabled=true);
  const spinner = document.createElement('span');
  spinner.className='spinner';
  card.querySelector('.actions').appendChild(spinner);
  if (action === 'enable' || action === 'disable') toast(name + ': ' + action + '... (can take ~30s)');
  try {
    const r = await api('/api/services/' + name + '/' + action, 'POST');
    if (r.ok) toast(name + ': ' + action + ' OK');
    else toast(name + ': ' + action + ' failed' + (r.error ? ' - ' + String(r.error).slice(0, 70) : ''), false);
  } catch(e) { toast(name + ': ' + action + ' error', false); }
  await loadServices();
}

async function removeService(name) {
  if (!confirm('Remove ' + name + ' from dashboard?')) return;
  const r = await api('/api/services/' + name + '/remove', 'POST');
  if (r.ok) { toast(name + ' removed'); await loadServices(); }
  else toast('Failed to remove: ' + (r.error||''), false);
}

async function scanServices() {
  const btn = document.querySelector('.btn.scan');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  const results = document.getElementById('scanResults');
  results.innerHTML = '<div style="color:var(--muted);font-size:.8rem;padding:8px;">Scanning listening ports and running services...</div>';
  try {
    const discovered = await api('/api/scan');
    if (!discovered.length) {
      results.innerHTML = '<div style="color:var(--muted);font-size:.8rem;padding:8px;">No new services found.</div>';
    } else {
      results.innerHTML = discovered.map(d => `
        <div class="scan-item">
          <div class="si-left">
            <span class="si-port">:${d.port}</span>
            <span>${d.process || 'unknown'}</span>
            ${d.already_added ? '<span style="color:var(--green);font-size:.7rem;">(already added)</span>' : ''}
          </div>
          ${!d.already_added ? `<button class="btn start" onclick="addService(${d.port},'${d.process||'service'}')">Add</button>` : ''}
        </div>
      `).join('');
    }
  } catch(e) {
    results.innerHTML = '<div style="color:var(--red);font-size:.8rem;padding:8px;">Scan failed</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Scan for Services';
}

async function addService(port, process) {
  const name = prompt('Service name (systemd unit name):', process);
  if (!name) return;
  const label = prompt('Display label:', name);
  if (!label) return;
  const url = prompt('URL (optional):', port ? location.protocol + '//' + location.hostname + ':' + port : '');
  const r = await api('/api/services/add', 'POST', {name, label, port: port||null, url: url||null, desc: 'Discovered on port ' + port, safe: true});
  if (r.ok) { toast(name + ' added'); await loadServices(); }
  else toast('Failed to add: ' + (r.error||''), false);
}

async function rebootBoard() {
  const ok = await askPin('Enter 4-digit PIN to reboot the machine');
  if (!ok) { toast('Reboot cancelled', false); return; }
  if (!confirm('Reboot the machine? All services stop for 2-3 minutes.')) return;
  toast('Sending reboot command...');
  try {
    const r = await api('/api/reboot', 'POST');
    toast('Rebooting... reconnecting in ~2 minutes');
    setTimeout(() => location.reload(), 150000);
  } catch (e) {
    toast('Rebooting... reconnecting in ~2 minutes');
    setTimeout(() => location.reload(), 150000);
  }
}

async function refreshAll(manual) {
  const btn = document.getElementById('refreshBtn');
  if (manual && btn) { btn.disabled = true; btn.textContent = 'Refreshing...'; }
  try { await loadAll(); } catch (e) { console.error(e); }
  const ts = document.getElementById('lastUpdated');
  if (ts) ts.textContent = 'Updated ' + new Date().toLocaleTimeString();
  if (manual && btn) { btn.disabled = false; btn.textContent = 'Refresh'; toast('Refreshed'); }
}

async function loadAll() {
  try {
    await loadConfig();
  } catch(e) { console.error('Config failed:', e); }
  try {
    await loadSysinfo();
  } catch(e) { console.error('Sysinfo failed:', e); }
  try {
    await loadHistory();
  } catch(e) { console.error('History failed:', e); }
  try {
    await loadNet();
  } catch(e) { console.error('Net failed:', e); }
  try {
    await loadServices();
  } catch(e) { console.error('Services failed:', e); }
}

// ---------- web console ----------
const term = {id: null, cursor: 0, timer: null, lines: [''], cr: false, out: '', flushT: null, sending: false};

function setTermState(txt, ok) {
  const el = document.getElementById('termState');
  if (!el) return;
  el.textContent = txt;
  el.className = 'badge' + (ok ? ' on' : '');
}

function termRender() {
  const out = document.getElementById('termOut');
  if (!out) return;
  out.textContent = term.lines.join('\n');
  out.scrollTop = out.scrollHeight;
}

function termWrite(txt) {
  if (!txt) return;
  let lines = term.lines;
  let cr = term.cr;
  for (const ch of txt) {
    if (cr) { cr = false; if (ch !== '\n') lines[lines.length - 1] = ''; }
    if (ch === '\r') { cr = true; }
    else if (ch === '\n') { lines.push(''); }
    else if (ch === '\b') { const i = lines.length - 1; lines[i] = lines[i].slice(0, -1); }
    else if (ch === '\t') { lines[lines.length - 1] += '    '; }
    else if (ch >= ' ') { lines[lines.length - 1] += ch; }
  }
  term.cr = cr;
  term.lines = lines.length > 800 ? lines.slice(-800) : lines;
  termRender();
}

function termClearScreen() {
  term.lines = [''];
  term.cr = false;
  termRender();
  termSend('\x0c');
}

async function termConnect(force) {
  if (term.id && !force) return;
  if (force && term.id) await termClose(true);
  if (!authToken) {
    const ok = await askPin('Enter 4-digit PIN for the web console');
    if (!ok) { setTermState('locked'); return; }
  }
  setTermState('connecting...');
  try {
    const r = await fetch('/api/term/start', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({cols: 100, rows: 32})
    });
    const d = await r.json();
    if (!d.ok) {
      if (r.status === 401) {
        authToken = ''; pinVerified = false; sessionStorage.removeItem('dash_token');
        setTermState('locked');
      } else {
        setTermState('failed: ' + (d.error || 'unknown'));
      }
      return;
    }
    term.id = d.id;
    term.cursor = d.cursor || 0;
    term.lines = [''];
    term.cr = false;
    setTermState('connected', true);
    termWrite(d.data || '');
    termPoll();
    const inp = document.getElementById('termIn');
    if (inp) inp.focus();
  } catch (e) {
    setTermState('cannot reach host');
  }
}

async function termPoll() {
  clearTimeout(term.timer);
  if (!term.id) return;
  try {
    const r = await fetch('/api/term/read?id=' + encodeURIComponent(term.id) + '&since=' + term.cursor,
                          {headers: authHeaders()});
    if (r.status === 404 || r.status === 401) {
      termWrite('\n[session closed]\n');
      setTermState(r.status === 401 ? 'locked' : 'closed');
      term.id = null;
      if (r.status === 401) { authToken = ''; pinVerified = false; sessionStorage.removeItem('dash_token'); }
      return;
    }
    if (r.ok) {
      const d = await r.json();
      if (d.data) termWrite(d.data);
      if (typeof d.cursor === 'number') term.cursor = d.cursor;
      if (!d.alive) {
        termWrite('\n[session ended]\n');
        setTermState('ended');
        term.id = null;
        return;
      }
    }
  } catch (e) { /* keep polling */ }
  if (term.id) term.timer = setTimeout(termPoll, 350);
}

async function termSend(data) {
  term.out += data;
  if (!term.flushT) term.flushT = setTimeout(termFlush, 120);
}

async function termFlush() {
  clearTimeout(term.flushT);
  term.flushT = null;
  if (term.sending) { term.flushT = setTimeout(termFlush, 60); return; }
  if (!term.id || !term.out) return;
  const id = term.id;
  const data = term.out;
  term.out = '';
  term.sending = true;
  try {
    await fetch('/api/term/write', {method: 'POST', headers: authHeaders(),
                                     body: JSON.stringify({id, data})});
  } catch (e) { /* ignore */ }
  term.sending = false;
  if (term.out && term.id) term.flushT = setTimeout(termFlush, 40);
}

async function termClose(silent) {
  clearTimeout(term.timer);
  clearTimeout(term.flushT);
  term.out = '';
  const id = term.id;
  term.id = null;
  if (id) {
    try {
      await fetch('/api/term/close', {method: 'POST', headers: authHeaders(),
                                       body: JSON.stringify({id})});
    } catch (e) { /* ignore */ }
  }
  setTermState('disconnected');
  if (!silent) toast('Console closed');
}

function showTab(name) {
  currentTab = name;
  document.getElementById('view-dash').style.display = name === 'dash' ? '' : 'none';
  document.getElementById('view-term').style.display = name === 'term' ? '' : 'none';
  document.getElementById('tabDash').classList.toggle('active', name === 'dash');
  document.getElementById('tabTerm').classList.toggle('active', name === 'term');
  if (name === 'term') {
    termConnect(false);
    const i = document.getElementById('termIn');
    if (i) i.focus();
  } else {
    refreshAll(false);
  }
}

(function bindTermInput() {
  const inp = document.getElementById('termIn');
  if (!inp) return;
  inp.addEventListener('input', () => {
    if (inp.value) { termSend(inp.value); inp.value = ''; }
  });
  inp.addEventListener('keydown', (e) => {
    if (e.isComposing) return;
    const k = e.key;
    if (k === 'Enter') { e.preventDefault(); termSend('\r'); }
    else if (k === 'Backspace') { e.preventDefault(); termSend('\x7f'); }
    else if (k === 'Tab') { e.preventDefault(); termSend('\t'); }
    else if (k === 'Escape') { e.preventDefault(); termSend('\x1b'); }
    else if (k === 'ArrowUp') { e.preventDefault(); termSend('\x1b[A'); }
    else if (k === 'ArrowDown') { e.preventDefault(); termSend('\x1b[B'); }
    else if (k === 'ArrowLeft') { e.preventDefault(); termSend('\x1b[D'); }
    else if (k === 'ArrowRight') { e.preventDefault(); termSend('\x1b[C'); }
    else if (k === 'Home') { e.preventDefault(); termSend('\x1b[H'); }
    else if (k === 'End') { e.preventDefault(); termSend('\x1b[F'); }
    else if (e.ctrlKey || e.metaKey) {
      const c = (k || '').toLowerCase();
      if (c === 'c') { e.preventDefault(); termSend('\x03'); }
      else if (c === 'd') { e.preventDefault(); termSend('\x04'); }
      else if (c === 'l') { e.preventDefault(); termClearScreen(); }
      else if (c === 'u') { e.preventDefault(); termSend('\x15'); }
      else if (c === 'r') { e.preventDefault(); termSend('\x12'); }
    }
  });
})();

setInterval(() => { if (currentTab === 'dash') refreshAll(false); }, 10000);
refreshAll(false);

(async function validateToken() {
  if (!authToken) return;
  try {
    const r = await fetch('/api/auth', {headers: authHeaders()});
    const d = await r.json();
    if (!d.ok) { authToken = ''; pinVerified = false; sessionStorage.removeItem('dash_token'); }
  } catch (e) { /* host unreachable, keep token */ }
})();
</script>
</body>
</html>
"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(HTML.replace('__APP_VERSION__', APP_VERSION).encode())
        elif self.path == '/api/config':
            tok = token_ok(self.headers.get('X-Dash-Token'))
            self.send_json(sanitize_cfg(load_config(), tok))
        elif self.path == '/api/services':
            tok = token_ok(self.headers.get('X-Dash-Token'))
            self.send_json(get_services(with_creds=tok))
        elif self.path == '/api/auth':
            self.send_json({"ok": token_ok(self.headers.get('X-Dash-Token'))})
        elif self.path == '/api/sysinfo':
            self.send_json(get_sysinfo())
        elif self.path == '/api/scan':
            self.send_json(scan_services())
        elif self.path == '/api/history':
            self.send_json(get_history())
        elif self.path == '/api/net':
            self.send_json(_iface_info())
        elif self.path.startswith('/api/term/read'):
            if not token_ok(self.headers.get('X-Dash-Token')):
                self.send_json({"ok": False, "error": "locked"}, 401)
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = (q.get('id') or [''])[0]
            try:
                since = int((q.get('since') or ['0'])[0] or 0)
            except ValueError:
                since = 0
            s = term_get(sid)
            if not s:
                self.send_json({"ok": False, "error": "no session"}, 404)
                return
            try:
                data, cursor, alive = s.read(since)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
                return
            self.send_json({"ok": True, "data": data, "cursor": cursor, "alive": alive})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parts = self.path.strip('/').split('/')

        # /api/unlock
        if len(parts) == 2 and parts[0] == 'api' and parts[1] == 'unlock':
            ip = self.client_address[0]
            if pin_throttled(ip):
                self.send_json({"ok": False, "error": "too many attempts"}, 429)
                return
            body = self.read_body()
            if pin_ok(body.get('pin', '')):
                self.send_json({"ok": True, "token": new_token()})
            else:
                pin_fail(ip)
                self.send_json({"ok": False, "error": "bad pin"}, 401)
            return

        # /api/settings (display preferences: network interface, graph window)
        if len(parts) == 2 and parts[0] == 'api' and parts[1] == 'settings':
            body = self.read_body()
            cfg = load_config()
            changed = []
            if 'net_iface' in body:
                want = str(body.get('net_iface') or '').strip()
                if want == 'auto':
                    want = ''
                if want and want != 'all' and want not in _iface_names():
                    self.send_json({"ok": False, "error": "unknown interface"}, 400)
                    return
                cfg['net_iface'] = want
                changed.append('net_iface')
            if 'history_minutes' in body:
                try:
                    minutes = int(body.get('history_minutes'))
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "history_minutes must be a number"}, 400)
                    return
                cfg['history_minutes'] = max(HIST_MIN_OPTIONS[0],
                                             min(HIST_MIN_OPTIONS[-1], minutes))
                changed.append('history_minutes')
            if not changed:
                self.send_json({"ok": False, "error": "nothing to change"}, 400)
                return
            save_config(cfg)
            if 'net_iface' in changed:
                reset_history()
            if 'history_minutes' in changed:
                apply_history_minutes(cfg['history_minutes'])
            self.send_json({"ok": True, "changed": changed,
                            "net_iface": _net_iface(),
                            "minutes": HIST_MINUTES,
                            "interval": HIST_INTERVAL})
            return

        # /api/term/{start|write|resize|close}
        if len(parts) == 3 and parts[0] == 'api' and parts[1] == 'term':
            if not token_ok(self.headers.get('X-Dash-Token')):
                self.send_json({"ok": False, "error": "locked"}, 401)
                return
            action = parts[2]
            body = self.read_body()
            if action == 'start':
                try:
                    s = term_start(int(body.get('cols') or 100), int(body.get('rows') or 30))
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)}, 500)
                    return
                data, cursor, alive = s.read(0)
                self.send_json({"ok": True, "id": s.id, "data": data,
                                "cursor": cursor, "alive": alive})
                return
            s = term_get(str(body.get('id') or ''))
            if action == 'close':
                term_drop(str(body.get('id') or ''))
                self.send_json({"ok": True})
                return
            if not s:
                self.send_json({"ok": False, "error": "no session"}, 404)
                return
            if action == 'write':
                s.write(str(body.get('data') or ''))
            elif action == 'resize':
                s.resize(int(body.get('cols') or s.cols), int(body.get('rows') or s.rows))
            else:
                self.send_json({"ok": False, "error": "bad action"}, 400)
                return
            self.send_json({"ok": True})
            return

        # /api/services/add (3 parts)
        if len(parts) == 3 and parts[0]=='api' and parts[1]=='services' and parts[2]=='add':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            name = body.get('name', '')
            if not name:
                self.send_response(400)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok":False,"error":"name required"}).encode())
                return
            cfg = load_config()
            if any(s['name'] == name for s in cfg['services']):
                self.send_response(409)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok":False,"error":"already exists"}).encode())
                return
            new_svc = {
                "name": name,
                "label": body.get('label', name),
                "port": body.get('port'),
                "url": body.get('url'),
                "icon": "PKG",
                "safe": body.get('safe', True),
                "desc": body.get('desc', ''),
            }
            cfg['services'].append(new_svc)
            save_config(cfg)
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok":True}).encode())
            return
        # /api/services/{name}/{action} (4 parts)
        if len(parts) == 4 and parts[0]=='api' and parts[1]=='services':
            name, action = parts[2], parts[3]
            if action == 'remove':
                cfg = load_config()
                cfg['services'] = [s for s in cfg['services'] if s['name'] != name]
                save_config(cfg)
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok":True}).encode())
                return
            if action in ('enable','disable'):
                cfg = load_config()
                svc = next((s for s in cfg['services'] if s['name']==name), None)
                if not svc:
                    self.send_response(404); self.end_headers(); return
                if not svc.get('safe', True):
                    self.send_response(403)
                    self.send_header('Content-Type','application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok":False,"error":"protected service"}).encode())
                    return
                rc, out, err = run("systemctl " + action + " " + name, timeout=90)
                ok = (rc == 0)
                st, en = get_unit_states([name]).get(name, ("", ""))
                if not ok:
                    if action == 'enable' and en.startswith('enabled'):
                        ok = True
                    elif action == 'disable' and en in ('disabled','masked','static','indirect','generated','not-found'):
                        ok = True
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok":ok,"output":out,"error":err,"unit_state":en}).encode())
                return
            if action not in ('start','stop','restart'):
                self.send_response(400); self.end_headers(); return
            cfg = load_config()
            svc = next((s for s in cfg['services'] if s['name']==name), None)
            if not svc:
                self.send_response(404); self.end_headers(); return
            if not svc.get('safe', True):
                self.send_response(403)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok":False,"error":"protected service"}).encode())
                return
            rc, out, err = run("systemctl " + action + " " + name,
                               timeout=60 if action == 'restart' else 30)
            ok = (rc == 0)
            st, en = get_unit_states([name]).get(name, ("", ""))
            if not ok:
                if action in ('start','restart') and st == 'active':
                    ok = True
                elif action == 'stop' and st in ('inactive','failed'):
                    ok = True
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok":ok,"output":out,"error":err,"unit_state":st}).encode())
            return
        # /api/creds/{name}
        if len(parts) == 3 and parts[0]=='api' and parts[1]=='creds':
            if not token_ok(self.headers.get('X-Dash-Token')):
                self.send_json({"ok": False, "error": "locked"}, 401)
                return
            name = parts[2]
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            cfg = load_config()
            svc = next((s for s in cfg['services'] if s['name']==name), None)
            if not svc:
                self.send_response(404); self.end_headers(); return
            svc['creds'] = {"User": body.get('user',''), "Pass": body.get('pass','')}
            save_config(cfg)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok":True}).encode())
            return
        # /api/reboot
        if len(parts) == 2 and parts[0]=='api' and parts[1]=='reboot':
            if not token_ok(self.headers.get('X-Dash-Token')):
                self.send_json({"ok": False, "error": "locked"}, 401)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok":True}).encode())
            try:
                self.wfile.flush()
            except Exception:
                pass
            subprocess.Popen(['/bin/sh','-c','sleep 1; /usr/bin/systemctl reboot'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type, X-Dash-Token')
        self.end_headers()

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description="Lightweight Linux dashboard + service manager")
    ap.add_argument("--config", default=CONFIG, help="config file (default: %(default)s)")
    ap.add_argument("--port", type=int, help="listen port (overrides config)")
    ap.add_argument("--bind", help="listen address (overrides config)")
    ap.add_argument("--detect", action="store_true",
                    help="print a detected config as JSON and exit")
    ap.add_argument("--title", help="dashboard title for --detect")
    ap.add_argument("--version", action="version", version="dashboard " + APP_VERSION)
    args = ap.parse_args()

    if args.detect:
        pin = os.environ.get("DASH_PIN", "")
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            print("note: run as root (sudo) to detect services owned by other users",
                  file=sys.stderr)
        print(json.dumps(detect_config(args.port or PORT, args.title, pin), indent=2))
        sys.exit(0)

    CONFIG = os.path.abspath(args.config)
    try:
        cfg = load_config()
    except Exception as e:
        print("cannot read config %s: %s" % (CONFIG, e), file=sys.stderr)
        print("create one with: %s --detect > %s" % (os.path.basename(sys.argv[0]), CONFIG),
              file=sys.stderr)
        sys.exit(1)
    PORT = int(args.port or cfg.get("port") or PORT)
    BIND = args.bind or cfg.get("bind") or BIND
    HIST_FILE = os.path.join(_run_dir(), "dashboard_history.json")
    TERM_LOG = os.path.join(_run_dir(), "dashboard_term.log")

    try:
        HIST_MINUTES = max(HIST_MIN_OPTIONS[0],
                           min(HIST_MIN_OPTIONS[-1], int(cfg.get("history_minutes") or HIST_MINUTES)))
    except (TypeError, ValueError):
        HIST_MINUTES = 30
    HIST_INTERVAL = _hist_interval(HIST_MINUTES)

    HISTORY = _load_history()
    seed_history()
    threading.Thread(target=history_loop, daemon=True).start()
    threading.Thread(target=term_reap_loop, daemon=True).start()
    httpd = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    print("Dashboard running on http://%s:%d (config: %s)" % (BIND, PORT, CONFIG))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

