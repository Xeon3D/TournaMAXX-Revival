#!/usr/bin/env python3
"""The Docker image's entrypoint: on the first start, write the control
panel's config into /data from the environment, and its admin password;
then run the panel, which runs the server.

    TMX_ADMIN_PASSWORD   the panel's admin password (first start only; without
                         it one is made up and printed in the container's log)
    TMX_PORT             the modem-call port (default 2323)
    TMX_TCP_PORTS        direct TournaMAXX ports (default "15000,17751"; "" for none)
    TMX_SECURE_COOKIES   "true" when the panel is behind HTTPS

The ports are only read on the first start; later they are changed in the
panel (Settings), which keeps them in /data/panel.json.
"""
import json
import os
import secrets
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
        users = json.load(f).get("users")
    if not users:
        pw = os.environ.get("TMX_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        subprocess.run([sys.executable, PANEL, "--config", CONFIG, "--set-password", "admin"],
                       env=dict(os.environ, TMX_PASSWORD=pw), check=True)
        if not os.environ.get("TMX_ADMIN_PASSWORD"):
            print("=" * 60)
            print("The control panel's login: admin / %s" % pw)
            print("(change it in the panel, under Settings)")
            print("=" * 60, flush=True)
    os.execv(sys.executable, [sys.executable, PANEL, "--config", CONFIG])


if __name__ == "__main__":
    main()
