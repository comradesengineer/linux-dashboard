#!/usr/bin/env bash
#
# Linux Dashboard installer / uninstaller
#
#   curl -fsSL https://raw.githubusercontent.com/comradesengineer/linux-dashboard/main/install.sh | sudo bash
#   sudo ./install.sh [--port 7979] [--title "My Server"] [--pin 1234]
#                     [--nginx] [--prefix DIR] [--no-systemd] [--yes]
#   sudo ./install.sh --uninstall [--purge]
#
set -euo pipefail

APP_VERSION="1.1"
REPO="${DASH_REPO:-https://github.com/comradesengineer/linux-dashboard}"
TARBALL_URL="${DASH_TARBALL_URL:-$REPO/archive/refs/heads/main.tar.gz}"

SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
    SRC_DIR="$(cd "$(dirname "$SELF")" && pwd)"
else
    SRC_DIR="$(pwd)"
fi
BUILD_DIR=""
cleanup() {
    if [ -n "$BUILD_DIR" ]; then
        rm -rf "$BUILD_DIR"
    fi
}
trap cleanup EXIT

BIN="/usr/local/bin/dashboard.py"
CONF_DIR="/etc/dashboard"
CONF="$CONF_DIR/config.json"
UNIT="/etc/systemd/system/dashboard.service"
NGINX_SITE="/etc/nginx/sites-available/dashboard"

PORT=7979
BIND="0.0.0.0"
TITLE=""
PIN=""
WITH_NGINX=0
UNINSTALL=0
PURGE=0
PREFIX=""
NO_SYSTEMD=0
YES=0

usage() {
    cat <<'EOF'
Linux Dashboard installer / uninstaller

Usage:
  curl -fsSL https://raw.githubusercontent.com/comradesengineer/linux-dashboard/main/install.sh | sudo bash
  sudo ./install.sh [options]
  sudo ./install.sh --uninstall [--purge]

Options:
  --port N        listen port (default 7979)
  --bind ADDR     listen address (default 0.0.0.0)
  --title TEXT    dashboard title (default: hostname)
  --pin XXXX      PIN for console/credentials/reboot (default: generated)
  --nginx         also proxy port 80 to the dashboard via nginx
  --prefix DIR    install files into DIR only, do not touch systemd
  --no-systemd    install files, skip the systemd unit
  --yes, -y       assume yes for prompts
  --uninstall     remove the service and binary (keeps config)
  --purge         with --uninstall: also delete /etc/dashboard
  -h, --help      show this help
  -V, --version   show the version
EOF
}

log()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port)       PORT="${2:?--port needs a value}"; shift 2 ;;
        --bind)       BIND="${2:?--bind needs a value}"; shift 2 ;;
        --title)      TITLE="${2:?--title needs a value}"; shift 2 ;;
        --pin)        PIN="${2:?--pin needs a value}"; shift 2 ;;
        --nginx)      WITH_NGINX=1; shift ;;
        --prefix)     PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
        --no-systemd) NO_SYSTEMD=1; shift ;;
        --yes|-y)     YES=1; shift ;;
        --uninstall)  UNINSTALL=1; shift ;;
        --purge)      PURGE=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        -V|--version) printf 'linux-dashboard installer %s\n' "$APP_VERSION"; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
done

as_root() { [ "$(id -u)" -eq 0 ]; }

if [ -n "$PREFIX" ]; then
    case "$PREFIX" in
        /|.|..|"$HOME") die "--prefix needs a dedicated directory (got '$PREFIX')" ;;
    esac
    mkdir -p "$PREFIX"
    PREFIX="$(cd "$PREFIX" && pwd)"
fi

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
    as_root || die "run the uninstaller with sudo"
    if [ -n "$PREFIX" ]; then
        if [ "$PURGE" = 1 ]; then
            [ -f "$PREFIX/dashboard.py" ] || die "$PREFIX does not look like a dashboard install"
            rm -rf "$PREFIX"
            log "removed $PREFIX"
        else
            rm -f "$PREFIX/dashboard.py"
            log "kept config in $PREFIX (use --purge to delete it)"
        fi
        log "uninstalled."
        exit 0
    fi
    if [ -f "$UNIT" ]; then
        systemctl disable --now dashboard.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl daemon-reload
        log "removed dashboard.service"
    fi
    rm -f "$BIN"
    if [ -e "$NGINX_SITE" ] || [ -e "/etc/nginx/sites-enabled/dashboard" ]; then
        rm -f "$NGINX_SITE" /etc/nginx/sites-enabled/dashboard
        command -v nginx >/dev/null && nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
        log "removed nginx site"
    fi
    if [ "$PURGE" = 1 ]; then
        rm -rf "$CONF_DIR"
        log "removed $CONF_DIR"
    else
        log "kept config in $CONF_DIR (use --purge to delete it)"
    fi
    log "uninstalled."
    exit 0
fi

# ----------------------------------------------------------------- install
if [ ! -f "$SRC_DIR/dashboard.py" ]; then
    # Started without the payload (e.g. curl ... | sudo bash): fetch it.
    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
        || die "curl or wget is required for a remote install"
    BUILD_DIR="$(mktemp -d)"
    log "downloading $TARBALL_URL"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 "$TARBALL_URL" -o "$BUILD_DIR/pkg.tar.gz" \
            || die "download failed: $TARBALL_URL"
    else
        wget -qO "$BUILD_DIR/pkg.tar.gz" "$TARBALL_URL" \
            || die "download failed: $TARBALL_URL"
    fi
    tar -xzf "$BUILD_DIR/pkg.tar.gz" -C "$BUILD_DIR" || die "cannot extract the download"
    SRC_DIR="$(dirname "$(find "$BUILD_DIR" -maxdepth 3 -type f -name dashboard.py | head -n1)")"
    [ -f "$SRC_DIR/dashboard.py" ] || die "download did not contain dashboard.py"
fi
command -v python3 >/dev/null 2>&1 || die "python3 is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' \
    || die "python3 >= 3.7 is required"

if [ -z "$PREFIX" ] && ! as_root; then
    die "run with sudo (or use --prefix DIR for a user-local install)"
fi

case "$PORT" in
    ''|*[!0-9]*) die "--port must be a number" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port out of range"

if [ -z "$PREFIX" ] && command -v ss >/dev/null 2>&1; then
    if ss -H -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"; then
        [ "$YES" = 1 ] && warn "port $PORT already in use, continuing anyway" \
                       || die "port $PORT is already in use (pick another with --port)"
    fi
fi

PY="$(command -v python3)"
HAS_SYSTEMD=0
if [ "$NO_SYSTEMD" = 0 ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    HAS_SYSTEMD=1
fi

# --- where files go
if [ -n "$PREFIX" ]; then
    install -d -m 755 "$PREFIX"
    install -m 755 "$SRC_DIR/dashboard.py" "$PREFIX/dashboard.py"
    BIN="$PREFIX/dashboard.py"
    CONF="$PREFIX/config.json"
    HAS_SYSTEMD=0
else
    install -d -m 755 "$(dirname "$BIN")"
    install -m 755 "$SRC_DIR/dashboard.py" "$BIN"
    install -d -m 755 "$CONF_DIR"
fi

# --- config: detect services on first install, patch on upgrades
FRESH=0
if [ ! -f "$CONF" ]; then
    FRESH=1
    DASH_PIN="$PIN" "$PY" "$SRC_DIR/dashboard.py" --detect --port "$PORT" \
        ${TITLE:+--title "$TITLE"} > "$CONF"
    log "generated $CONF from currently listening services"
    [ "$(id -u)" -eq 0 ] || warn "not running as root: services owned by other users were not detected (run with sudo for a full scan)"
fi

"$PY" - "$CONF" "$PORT" "$BIND" "$TITLE" "$PIN" <<'PY'
import json, secrets, sys

path, port, bind, title, pin = sys.argv[1:6]
with open(path) as f:
    cfg = json.load(f)
cfg["port"] = int(port)
cfg["bind"] = bind
if title:
    cfg["title"] = title
if pin:
    cfg["pin"] = pin
cfg.setdefault("services", [])
cfg.setdefault("disk_path", "/")
cfg.setdefault("net_iface", "")
cfg.setdefault("history_minutes", 30)
if not cfg.get("pin"):
    cfg["pin"] = "%04d" % secrets.randbelow(10000)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
chmod 600 "$CONF" 2>/dev/null || true

CONFIG_PIN="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["pin"])' "$CONF")"
SERVICE_COUNT="$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["services"]))' "$CONF")"

# --- systemd unit
if [ "$HAS_SYSTEMD" = 1 ]; then
    cat > "$UNIT" <<EOF
[Unit]
Description=Linux Dashboard + Service Manager
After=network-online.target

[Service]
Type=simple
ExecStart=$PY $BIN --config $CONF
Restart=on-failure
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable dashboard.service >/dev/null
    systemctl restart dashboard.service
    sleep 1
    systemctl is-active --quiet dashboard.service \
        || die "dashboard failed to start: journalctl -u dashboard -n 30"
fi

# --- optional nginx proxy on :80
if [ "$WITH_NGINX" = 1 ]; then
    command -v nginx >/dev/null 2>&1 || die "--nginx requested, but nginx is not installed"
    install -d -m 755 /etc/nginx/sites-available /etc/nginx/sites-enabled
    cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
EOF
    ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/dashboard
    if ! nginx -t >/dev/null 2>&1; then
        rm -f /etc/nginx/sites-enabled/dashboard
        die "nginx config test failed; site not enabled (port 80 in use?)"
    fi
    systemctl reload nginx 2>/dev/null || systemctl restart nginx
    "$PY" - "$CONF" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
pr = c.setdefault("protected", [])
if "nginx" not in pr:
    pr.append("nginx")
json.dump(c, open(p, "w"), indent=2)
PY
    log "nginx: port 80 now proxies the dashboard (nginx is protected from Stop)"
fi

# --- summary
IP="$("$PY" - <<'PY'
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
except Exception:
    print("127.0.0.1")
PY
)"

printf '\n'
printf '  Dashboard : http://%s:%s\n' "$IP" "$PORT"
printf '  PIN       : %s\n' "$CONFIG_PIN"
printf '  Config    : %s\n' "$CONF"
printf '  Services  : %s detected\n' "$SERVICE_COUNT"
if [ "$HAS_SYSTEMD" = 1 ]; then
    printf '  Service   : systemctl status dashboard\n'
elif [ -n "$PREFIX" ]; then
    printf '\n  Run it with:\n    %s %s --config %s --port %s\n' "$PY" "$BIN" "$CONF" "$PORT"
elif [ "$FRESH" = 1 ]; then
    printf '\n  No systemd detected, start it manually:\n    %s %s --config %s\n' "$PY" "$BIN" "$CONF"
fi
printf '\n'
