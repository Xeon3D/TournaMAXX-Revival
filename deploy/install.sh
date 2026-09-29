#!/bin/sh
# Install (or update) TournaMAXX-Revival and its control panel on a Debian or
# Ubuntu server.  Run as root from a copy of the repository:
#
#   sudo sh deploy/install.sh --domain us.accessmerit.com --email you@example.com
#
#   --domain NAME   the panel's address; a Let's Encrypt certificate is made
#                   for it (needs its DNS pointing here and port 80 open)
#   --email ADDR    for Let's Encrypt's expiry notices
#   --no-tls        skip the certificate (the panel is then plain HTTP:
#                   only for trying it out)
#
# Running it again updates the code and keeps the data, config and password.
#
# What it sets up:
#   /opt/tournamaxx            the code
#   /var/lib/tournamaxx        state.json, modem-server.log, panel.json,
#                              server.env, files/, packages/, outgoing/, backups/
#   tournamaxx.service         the server, as user tournamaxx
#   tournamaxx-panel.service   the panel on 127.0.0.1:8080, behind nginx
#   /etc/sudoers.d/tournamaxx  lets the panel start/stop/restart the server
#   /etc/logrotate.d/tournamaxx
set -eu

DOMAIN=""
EMAIL=""
TLS=1
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --email) EMAIL="$2"; shift 2 ;;
        --no-tls) TLS=0; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "run this as root (sudo sh deploy/install.sh ...)" >&2; exit 1; }
[ -n "$DOMAIN" ] || { echo "--domain is needed" >&2; exit 2; }
if [ "$TLS" = 1 ] && [ -z "$EMAIL" ]; then
    echo "--email is needed for the certificate (or --no-tls)" >&2; exit 2
fi

SRC=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/tournamaxx
DATA=/var/lib/tournamaxx
say() { printf '\n== %s\n' "$*"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 nginx sudo logrotate
[ "$TLS" = 1 ] && apt-get install -y -q certbot python3-certbot-nginx

say "user and folders"
id tournamaxx >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin tournamaxx
install -d -o tournamaxx -g tournamaxx -m 750 "$DATA"
install -d -m 755 "$APP"

say "code -> $APP"
rm -rf "$APP/panel"
cp "$SRC/modem-server.py" "$SRC/megalink_switch.py" "$SRC/mkupdate.py" "$SRC/LICENSE" "$SRC/README.md" "$APP/"
cp -r "$SRC/panel" "$SRC/docs" "$SRC/deploy" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
rm -f "$APP/panel/panel.json"
chown -R root:root "$APP"
chmod -R u=rwX,go=rX "$APP"

say "configuration"
if [ ! -f "$DATA/panel.json" ]; then
    SECURE=true; [ "$TLS" = 1 ] || SECURE=false
    cat > "$DATA/panel.json" <<EOF
{
 "listen": "127.0.0.1",
 "port": 8080,
 "data_dir": "$DATA",
 "server_script": "$APP/modem-server.py",
 "mkupdate_script": "$APP/mkupdate.py",
 "service": {"mode": "systemd", "unit": "tournamaxx"},
 "server": {"port": 2323, "tcp_ports": [15000, 17751], "admin_port": 2324, "switch_port": 8086},
 "secure_cookies": $SECURE,
 "users": {}
}
EOF
    chown tournamaxx:tournamaxx "$DATA/panel.json"
    chmod 600 "$DATA/panel.json"
fi
if [ ! -f "$DATA/server.env" ]; then
    echo "TMX_ARGS=--port 2323 --tcp-ports 15000,17751 --admin-port 2324 --switch-port 8086" > "$DATA/server.env"
    chown tournamaxx:tournamaxx "$DATA/server.env"
fi

say "services"
install -m 644 "$APP/deploy/tournamaxx.service" /etc/systemd/system/tournamaxx.service
install -m 644 "$APP/deploy/tournamaxx-panel.service" /etc/systemd/system/tournamaxx-panel.service
SYSTEMCTL=$(command -v systemctl)
cat > /tmp/tournamaxx.sudoers <<EOF
# The control panel (user tournamaxx) may start, stop and restart the server.
tournamaxx ALL=(root) NOPASSWD: $SYSTEMCTL start tournamaxx, $SYSTEMCTL stop tournamaxx, $SYSTEMCTL restart tournamaxx
EOF
visudo -cf /tmp/tournamaxx.sudoers
install -m 440 /tmp/tournamaxx.sudoers /etc/sudoers.d/tournamaxx
rm -f /tmp/tournamaxx.sudoers
install -m 644 "$APP/deploy/logrotate-tournamaxx" /etc/logrotate.d/tournamaxx
systemctl daemon-reload

say "panel login"
if ! grep -q '"pbkdf2_sha256' "$DATA/panel.json"; then
    echo "Choose the panel's admin password (10 characters or more)."
    if [ -n "${TMX_PASSWORD:-}" ]; then
        runuser -u tournamaxx -- env TMX_PASSWORD="$TMX_PASSWORD" python3 "$APP/panel/panel.py" --config "$DATA/panel.json" --set-password admin
    else
        runuser -u tournamaxx -- python3 "$APP/panel/panel.py" --config "$DATA/panel.json" --set-password admin
    fi
else
    echo "kept (change it in the panel, under Settings)"
fi

systemctl enable -q tournamaxx tournamaxx-panel
systemctl restart tournamaxx tournamaxx-panel

say "nginx for $DOMAIN"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP/deploy/nginx-tournamaxx.conf" > /etc/nginx/sites-available/tournamaxx
ln -sf /etc/nginx/sites-available/tournamaxx /etc/nginx/sites-enabled/tournamaxx
nginx -t
systemctl reload nginx
if [ "$TLS" = 1 ]; then
    if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
        certbot --nginx -d "$DOMAIN" --non-interactive --reinstall --redirect
    else
        certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
    fi
fi

say "firewall"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    for p in 80 443 2323 15000 17751; do ufw allow "$p/tcp" >/dev/null; done
    ufw allow 8086/udp >/dev/null
    echo "ufw: opened TCP 80, 443, 2323, 15000, 17751 and UDP 8086"
else
    echo "no active ufw: make sure TCP 80, 443, 2323, 15000, 17751 and UDP 8086 reach this machine"
fi

say "done"
echo "server: $(systemctl is-active tournamaxx), panel: $(systemctl is-active tournamaxx-panel)"
PROTO=https; [ "$TLS" = 1 ] || PROTO=http
echo "Control panel: $PROTO://$DOMAIN/  (user admin)"
echo "Emulators dial $DOMAIN, port 2323; Mega-Link: Remote Switch $DOMAIN:8086."
