#!/usr/bin/env python3
"""The Docker image's entrypoint: on the first start, write the control
panel's config into /data from the environment, and its admin password;
then run the panel, which runs the server.

    TMX_ADMIN_PASSWORD   the first user's password (first start only; without
                         it the panel asks for the first user when it is
                         opened, within 15 minutes of starting)
    TMX_ADMIN_USER       that user's name (default "admin")
    TMX_PORT             the modem-call port (default 2323)
    TMX_TCP_PORTS        direct TournaMAXX ports (default "15000,17751"; "" for none)
    TMX_SWITCH_PORT      the Mega-Link switch's UDP port (default 8086; 0 for none;
                         also set in a config made before the switch existed)
    TMX_SECURE_COOKIES   "true" when the panel is behind HTTPS
    TMX_TRUSTED_PROXIES  the proxy's address or network, e.g. "172.16.0.0/12"
                         (comma-separated), whose X-Real-IP header the login
                         throttle believes; read at every start when set

The ports are only read on the first start; later they are changed in the
panel (Settings), which keeps them in /data/panel.json.
"""
import json
import os
import subprocess
import sys

DATA = os.environ.get("TMX_DATA", "/data")
CONFIG = os.path.join(DATA, "panel.json")
PANEL = "/app/panel/panel.py"


def main():
    if not os.path.exists(CONFIG):
        tcp = [int(p) for p in os.environ.get("TMX_TCP_PORTS", "15000,17751").replace(",", " ").split()]
        cfg = {
            "listen": "0.0.0.0", "port": 8080, "data_dir": DATA,
            "server_script": "/app/modem-server.py", "mkupdate_script": "/app/mkupdate.py",
            "service": {"mode": "process"},
            "server": {"port": int(os.environ.get("TMX_PORT", "2323")), "tcp_ports": tcp, "admin_port": 2324},
            "secure_cookies": os.environ.get("TMX_SECURE_COOKIES", "false").lower() in ("1", "true", "yes"),
            "users": {},
        }
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        os.chmod(CONFIG, 0o600)
        print("made %s" % CONFIG, flush=True)
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    changed = False
    if "switch_port" not in cfg.setdefault("server", {}):
        cfg["server"]["switch_port"] = int(os.environ.get("TMX_SWITCH_PORT", "8086") or 0)
        changed = True
    if "TMX_TRUSTED_PROXIES" in os.environ:
        proxies = os.environ["TMX_TRUSTED_PROXIES"].replace(",", " ").split()
        changed = changed or cfg.get("trusted_proxies") != proxies
        cfg["trusted_proxies"] = proxies
    if changed:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    users = cfg.get("users")
    if not users and os.environ.get("TMX_ADMIN_PASSWORD"):
        subprocess.run([sys.executable, PANEL, "--config", CONFIG, "--set-password",
                        os.environ.get("TMX_ADMIN_USER", "admin")],
                       env=dict(os.environ, TMX_PASSWORD=os.environ["TMX_ADMIN_PASSWORD"]), check=True)
    os.execv(sys.executable, [sys.executable, PANEL, "--config", CONFIG])


if __name__ == "__main__":
    main()
