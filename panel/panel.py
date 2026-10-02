#!/usr/bin/env python3
"""
TournaMAXX-Revival control panel: a web page to run modem-server.py.

Python 3, standard library only.  It starts, stops and restarts the server,
shows its log, and edits everything in its state file -- tournaments,
cabinets and their locations, the operator's outbox (messages, files,
settings, prices, dial-up), players and update packages -- through the
server's admin port while it runs (so that edits and calls take turns), or
the file itself while it is stopped.

    python panel.py --config panel.json                    run it
    python panel.py --config panel.json --set-password admin

The config (made with defaults if missing):

    {"listen": "127.0.0.1", "port": 8080,  one address, or a list; "0.0.0.0": all IPv4,
                                           "::": all IPv6 (Settings > Panel access sets it)
     "data_dir": ".",                      state, log, files, packages, backups
     "server_script": "../modem-server.py",
     "service": {"mode": "process"}        the panel runs the server itself
              | {"mode": "systemd", "unit": "tournamaxx"},
     "server": {"port": 2323, "tcp_ports": [], "admin_port": 2324,
                "switch_port": 0},         the Mega-Link switch's UDP port (0: off)
     "secure_cookies": false,              true: always Secure (also when a proxy
                                           sends X-Forwarded-Proto: https)
     "trusted_proxies": [],                more proxies whose X-Real-IP is believed,
                                           e.g. ["172.16.0.0/12"] (optional)
     "users": {"admin": "pbkdf2_sha256$..."}}

In systemd mode the server's options go to <data_dir>/server.env, which
the unit reads, and the panel runs "sudo -n systemctl start|stop|restart
<unit>" (deploy/ sets up the sudo rule).  Put it behind a TLS proxy
(deploy/nginx-tournamaxx.conf) rather than on the open internet.

Failed logins are throttled per address: 5 for one user name, 20 in all,
then five minutes' wait.  Behind a proxy every connection comes from the
proxy, so the panel takes the browser's address from the proxy's X-Real-IP
header -- but only when the connection comes from a trusted proxy: loopback
(nginx on the same host), and the addresses or networks (CIDR) listed in
"trusted_proxies", such as the address Nginx Proxy Manager connects from
when the panel runs in Docker (an X-Real-IP from anywhere else is ignored,
and the panel prints the sender's address once, to find it by).
"""

import argparse
import datetime
import getpass
import hashlib
import hmac
import http.server
import importlib.util
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
MAX_UPLOAD = 64 * 1024 * 1024
SESSION_HOURS = 12
SETUP_MINUTES = 15        # a fresh install takes its first user this long after starting
USER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

CFG = None
CFG_PATH = None
CFG_LOCK = threading.RLock()


def now():
    return int(time.time())


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def default_config(path):
    base = os.path.dirname(os.path.abspath(path))
    return {
        "listen": "127.0.0.1", "port": 8080,
        "data_dir": base,
        "server_script": os.path.normpath(os.path.join(HERE, "..", "modem-server.py")),
        "mkupdate_script": os.path.normpath(os.path.join(HERE, "..", "mkupdate.py")),
        "service": {"mode": "process"},
        "server": {"port": 2323, "tcp_ports": [], "admin_port": 2324, "switch_port": 0},
        "secure_cookies": False,
        "users": {},
    }


def load_config(path):
    global CFG, CFG_PATH
    CFG_PATH = os.path.abspath(path)
    cfg = default_config(path)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    CFG = cfg
    save_config()


def save_config():
    with CFG_LOCK:
        write_atomic(CFG_PATH, json.dumps(CFG, indent=1).encode("utf-8"))
        try:
            os.chmod(CFG_PATH, 0o600)
        except OSError:
            pass


def data(*parts):
    return os.path.join(CFG["data_dir"], *parts)


def state_path():
    return data("state.json")


def log_path():
    return data("modem-server.log")


# ------------------------------------------------------------------ passwords

def hash_password(pw, iterations=240000):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt, h.hex())


def check_password(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, AttributeError):
        return False


SESSIONS = {}          # token -> [user, expiry]
FAILS = {}             # address -> [count, until]
AUTH_LOCK = threading.Lock()


def new_session(user):
    tok = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        SESSIONS[tok] = [user, now() + SESSION_HOURS * 3600]
    return tok


def session_user(tok):
    with AUTH_LOCK:
        s = SESSIONS.get(tok or "")
        if not s or s[1] < now() or s[0] not in CFG["users"]:
            SESSIONS.pop(tok or "", None)
            return None
        s[1] = now() + SESSION_HOURS * 3600
        return s[0]


def end_sessions(user, keep=None):
    """Log a user out everywhere (but the session KEEP)."""
    with AUTH_LOCK:
        for tok in [t for t, s in SESSIONS.items() if s[0] == user and t != keep]:
            del SESSIONS[tok]


STARTED = [0]


def setup_state():
    """First-run setup: open while there are no users, for SETUP_MINUTES after
    the panel starts (restart it to open it again)."""
    if CFG["users"]:
        return {"needed": False, "open": False}
    return {"needed": True, "open": now() < STARTED[0] + SETUP_MINUTES * 60,
            "minutes": SETUP_MINUTES}


def check_new_user(name, password):
    if not USER_NAME.match(name or ""):
        raise ValueError("a user name is 1-32 letters, digits, dots, dashes or underscores")
    if len(password or "") < 10:
        raise ValueError("use at least 10 characters")


# Failed logins, counted for five minutes after the last one: 5 for one user
# name from one address, so that people behind one address (a shared proxy,
# a NAT) do not lock each other out; and 20 from one address over all names.
FAIL_WINDOW = 300
FAIL_LIMITS = (("user", 5), ("addr", 20))


def fail_keys(addr, user):
    return {"user": ("user", addr, str(user)[:64]), "addr": ("addr", addr)}


def throttled(addr, user):
    keys = fail_keys(addr, user)
    with AUTH_LOCK:
        for kind, limit in FAIL_LIMITS:
            c = FAILS.get(keys[kind])
            if c is not None and c[0] >= limit and c[1] > now():
                return True
    return False


def failed(addr, user):
    with AUTH_LOCK:
        t = now()
        for k in [k for k, c in FAILS.items() if c[1] <= t]:
            del FAILS[k]
        for k in fail_keys(addr, user).values():
            c = FAILS.setdefault(k, [0, 0])
            c[0] += 1
            c[1] = t + FAIL_WINDOW


def logged_in(addr, user):
    with AUTH_LOCK:
        FAILS.pop(fail_keys(addr, user)["user"], None)


# ------------------------------------------------------------------ proxies

LOOPBACK = ("127.0.0.0/8", "::1")
PROXY_WARNED = set()


def trusted_proxies():
    """Loopback (nginx on the same host, deploy/nginx-tournamaxx.conf) and the
    config's "trusted_proxies": addresses or networks, a list or a string."""
    v = CFG.get("trusted_proxies") or []
    if isinstance(v, str):
        v = v.replace(",", " ").split()
    nets = []
    for a in list(LOOPBACK) + list(v):
        try:
            nets.append(ipaddress.ip_network(str(a).strip().strip("[]"), strict=False))
        except ValueError:
            raise ValueError("not an address or network: %s" % a)
    return nets


def ip(addr):
    """addr as an ip_address (an IPv4-mapped IPv6 one as IPv4), or None."""
    try:
        a = ipaddress.ip_address(str(addr).strip().strip("[]").split("%")[0])
    except ValueError:
        return None
    return (a.ipv4_mapped or a) if a.version == 6 else a


def real_address(peer, header):
    """The browser's address: the proxy's X-Real-IP header when the connection
    comes from a trusted proxy, otherwise the connection's own address (anyone
    else could send any X-Real-IP, a new one at each login attempt)."""
    p = ip(peer)
    if not header or p is None:
        return str(p or peer)
    if not any(p.version == n.version and p in n for n in trusted_proxies()):
        if peer not in PROXY_WARNED and len(PROXY_WARNED) < 100:
            PROXY_WARNED.add(peer)
            print("X-Real-IP from %s ignored: not a trusted proxy (see trusted_proxies "
                  "in panel.py)" % peer, flush=True)
        return str(p)
    r = ip(header)
    return str(r) if r is not None else str(p)


# ------------------------------------------------------------------ the server

_SERVER_MOD = None


def server_module():
    """modem-server.py itself, for apply_admin and default_state."""
    global _SERVER_MOD
    if _SERVER_MOD is None:
        spec = importlib.util.spec_from_file_location("modem_server", CFG["server_script"])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SERVER_MOD = mod
    return _SERVER_MOD


def server_args():
    s = CFG["server"]
    args = ["--port", str(int(s.get("port", 2323)))]
    ports = [int(p) for p in s.get("tcp_ports", [])]
    if ports:
        args += ["--tcp-ports", ",".join(str(p) for p in ports)]
    if s.get("admin_port"):
        args += ["--admin-port", str(int(s["admin_port"]))]
    if s.get("switch_port"):
        args += ["--switch-port", str(int(s["switch_port"]))]
    if s.get("dns_port"):
        args += ["--dns-port", str(int(s["dns_port"]))]
        if s.get("dns_answer"):
            args += ["--dns-answer", str(s["dns_answer"])]
    return args


CRASH_SECONDS = 60       # a server that stops sooner than this after starting has crashed
MAX_CRASHES = 3          # crashes in a row before the panel stops starting it again


def last_error():
    """The last line of server-stderr.txt (a traceback's "OSError: ..."), without colours."""
    lines, _ = tail(data("server-stderr.txt"), 8192)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", l).strip() for l in lines]
    lines = [l for l in lines if l]
    return lines[-1] if lines else ""


class ProcessService:
    """The panel runs the server as its own child (trying it out, and in the
    Docker image).  A server that stops without being told to is started
    again, as systemd would -- but not after MAX_CRASHES quick crashes in a
    row (a port another program holds, say): then it waits for a start by
    hand."""

    def __init__(self):
        self.proc = None
        self.started = None
        self.wanted = False
        self.crashes = 0
        self.failure = None
        self.lock = threading.RLock()
        threading.Thread(target=self.watch, daemon=True).start()

    def status(self):
        running = self.proc is not None and self.proc.poll() is None
        state = "running" if running else "failed" if self.failure else "stopped"
        return {"mode": "process", "active": running, "state": state, "failure": self.failure,
                "pid": self.proc.pid if running else None, "since": self.started if running else None}

    def start(self):
        """A start by hand (or the panel's own at its start): the crashes count again from 0."""
        with self.lock:
            self.crashes, self.failure = 0, None
            self._start()

    def _start(self):
        self.wanted = True
        if self.status()["active"]:
            return
        cmd = [sys.executable, CFG["server_script"], "--log", log_path(), "--state", state_path()] + server_args()
        with open(data("server-stderr.txt"), "ab") as err:
            self.proc = subprocess.Popen(cmd, cwd=CFG["data_dir"], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=err)
        self.started = now()
        time.sleep(0.5)
        if self.proc.poll() is not None:
            raise RuntimeError("the server stopped at once: %s" % (last_error() or "see server-stderr.txt"))

    def stop(self):
        with self.lock:
            self.wanted = False
            self.failure = None
            if self.status()["active"]:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def watch(self):
        while True:
            time.sleep(5)
            with self.lock:
                if not (self.wanted and self.proc is not None and self.proc.poll() is not None):
                    continue
                code = self.proc.returncode
                self.proc = None
                self.crashes = self.crashes + 1 if now() - self.started < CRASH_SECONDS else 1
                if self.crashes >= MAX_CRASHES:
                    self.wanted = False
                    self.failure = "the server stopped %d times in a row within %d seconds of starting (exit %s): %s" % (
                        self.crashes, CRASH_SECONDS, code, last_error() or "see server-stderr.txt")
                    print("%s; not starting it again until it is started by hand" % self.failure, flush=True)
                    continue
                print("the server stopped (exit %s): starting it again" % code, flush=True)
                try:
                    self._start()
                except (OSError, RuntimeError) as e:
                    print("could not start the server: %s" % e, flush=True)

    def restart(self):
        self.stop()
        self.start()

    def apply_settings(self):
        pass


class SystemdService:
    def __init__(self, unit):
        self.unit = unit

    def status(self):
        try:
            out = subprocess.run(["systemctl", "show", self.unit, "--property=ActiveState,SubState,MainPID,"
                                  "ActiveEnterTimestamp,UnitFileState"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"mode": "systemd", "active": False, "state": "unknown (%s)" % e}
        p = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        since = None
        ts = p.get("ActiveEnterTimestamp", "")
        if ts:
            try:
                since = int(datetime.datetime.strptime(" ".join(ts.split()[1:3]), "%Y-%m-%d %H:%M:%S").timestamp())
            except ValueError:
                pass
        active = p.get("ActiveState") == "active"
        return {"mode": "systemd", "unit": self.unit, "active": active,
                "state": "%s (%s)" % (p.get("ActiveState", "?"), p.get("SubState", "?")),
                "pid": int(p.get("MainPID", 0) or 0) or None, "since": since if active else None,
                "enabled": p.get("UnitFileState")}

    def _run(self, action):
        r = subprocess.run(["sudo", "-n", "systemctl", action, self.unit], capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise RuntimeError("systemctl %s failed: %s" % (action, (r.stderr or r.stdout).strip()))

    def start(self):
        self._run("start")

    def stop(self):
        self._run("stop")

    def restart(self):
        self._run("restart")

    def apply_settings(self):
        write_atomic(data("server.env"), ("TMX_ARGS=%s\n" % " ".join(server_args())).encode("utf-8"))


SERVICE = None


# ------------------------------------------------------------------ the state

STORE_LOCK = threading.Lock()


class Offline(Exception):
    pass


def admin_request(req):
    port = CFG["server"].get("admin_port")
    if not port:
        raise Offline()
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=10)
    except OSError:
        raise Offline()
    with s:
        s.sendall(json.dumps(req).encode("utf-8") + b"\n")
        f = s.makefile("rb")
        line = f.readline()
    if not line:
        raise RuntimeError("the server closed the admin connection")
    rep = json.loads(line.decode("utf-8"))
    if not rep.get("ok"):
        raise RuntimeError(rep.get("error", "the server refused the request"))
    return rep["result"]


def read_state_file():
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return server_module().default_state()


def state_request(req):
    """A request to the running server, or to the file while it is stopped."""
    mutating = req.get("op") != "get"
    with STORE_LOCK:
        if mutating:
            backup_state()
        try:
            return admin_request(req)
        except Offline:
            if SERVICE.status()["active"]:
                raise RuntimeError("the server is running but its admin port (%s) does not answer; "
                                   "restart it from the panel" % CFG["server"].get("admin_port"))
        st = read_state_file()
        result = server_module().apply_admin(st, req)
        if mutating:
            write_atomic(state_path(), json.dumps(st, indent=1).encode("utf-8"))
        return result


def get_state():
    return state_request({"op": "get"})


def switch_status():
    """The running Mega-Link switch's rooms and cabinets, or None."""
    try:
        return admin_request({"op": "switch"})
    except (Offline, RuntimeError, ValueError, OSError):
        return None


def public_megalink(host):
    """What the public Mega-Link page shows: the public rooms, how full they
    are, and how to connect.  No addresses, nothing private."""
    st = read_state_file()
    ml = st.get("megalink", {})
    status = switch_status()
    live = {r["name"]: r for r in (status or {}).get("rooms", [])}
    port = int(CFG["server"].get("switch_port") or 0)
    rooms = []
    for r in ml.get("rooms", []):
        if not r.get("public"):
            continue
        cabs = live.get(r.get("name"), {}).get("cabinets", [])
        rooms.append({"name": r.get("name", ""), "description": r.get("description", ""),
                      "secret": r.get("secret", ""), "max": int(r.get("max") or 8),
                      "cabinets": [{"since": c["since"]} for c in cabs]})
    return {"enabled": bool(ml.get("public_page")), "online": port > 0 and status is not None,
            "host": ml.get("host") or host, "port": port, "intro": ml.get("intro", ""),
            "rooms": rooms, "now": now()}


# ------------------------------------------------------------------ backups

LAST_BACKUP = [0]


def backup_state(force=False):
    """A copy of the state file before changes, at most every 15 minutes
    (always before a whole-file replace); the newest 60 are kept."""
    if not os.path.exists(state_path()):
        return None
    if not force and now() - LAST_BACKUP[0] < 900:
        return None
    os.makedirs(data("backups"), exist_ok=True)
    name = "state-%s.json" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(state_path(), data("backups", name))
    LAST_BACKUP[0] = now()
    for old in sorted(os.listdir(data("backups")))[:-60]:
        os.remove(data("backups", old))
    return name


def backups():
    if not os.path.isdir(data("backups")):
        return []
    out = []
    for n in sorted(os.listdir(data("backups")), reverse=True):
        p = data("backups", n)
        out.append({"name": n, "size": os.path.getsize(p), "at": int(os.path.getmtime(p))})
    return out


# ------------------------------------------------------------------ reports
#
# What the cabinets answer at the end of an update call, as docs/tournamaxx.md
# describes it.  Offsets below are into the body (the message offset less 4).

RELEASES = {9: "Emerald 2 (V9.0x)", 7: "Emerald (V8.04)", 6: "Double Diamond (V7.01)", 3: "Diamond (V6.03)"}


def ctext(b):
    return b.split(b"\0")[0].decode("latin1")


# The decoders take the report's bytes (without the 4 of the framing) and
# whether the cabinet runs a Linux release (login protocol 13 or more: Ruby
# on), whose reports are larger (docs/tournamaxx.md, "Linux releases").

# A Linux release's option table (0x0202 +0xA6F): index -> what it sets.
# Plain names: the report's own fields are read from these (Jade 2,
# Process_Setup_Options_Request).  "used by": what reads the byte in the
# program (a hint, not a confirmed meaning).
LINUX_OPTIONS = {
    0: "used by SetDefaultCurrencyValues", 1: "used by the link code (Link_InitLinkStruc)",
    2: "used by CheckCoinlessOp, Idle", 4: "used by REPLAY_CheckForReplay",
    5: "used by GetGamePriceString, CreatePrizeBucksTable", 7: "used by the link code",
    8: "adult games", 9: "used by the link code", 11: "used by ShowAdverts, Idle",
    12: "used by Tournament_Start, ApplyPriceInfo", 13: "6 Star", 15: "used by ResetCCOData",
    19: "used by IsMeritMoneyActive", 20: "used by NTournSupported (TournaMAXX)",
    21: "(also at +0xA69)", 22: "used by ChangeLanguage", 23: "adult content",
    26: "6 Star: high scores", 27: "6 Star: video billboard", 28: "6 Star: volume",
    30: "nudity", 31: "full nude", 32: "used by PrizeZoneOptions", 33: "used by the link code",
    35: "used by ChangeLanguage", 36: "used by TournSupported", 37: "6 Star: calibration",
    40: "used by CheckSexDependencies", 41: "used by Continue, ContPayTable", 42: "used by ShowMyMeritData",
    47: "used by SetGamePriceAndStatusFromKey", 48: "used by AdjustTime", 49: "used by IsLeaseModeOn",
    50: "6 Star: update from server", 51: "used by IsPromoCreditActive",
    52: "used by CheckMegaLink, UpdateNumUnitsLinked", 53: "used by CreateHighScores",
    55: "classic mode (SwitchVideoMode; a 0x0201 also sets /var/config/classic)",
    57: "used by PrizeZoneIsEnabled", 58: "used by CheckSexDependencies, FindGameInCategory",
    61: "used by BuildCoinlessDialog", 62: "used by DDCGameActive", 67: "used by SetGamePriceAndStatusFromKey",
    68: "used by IsShowTendersActive", 69: "used by CDM_Supported (Fantasy Sports)",
    70: "used by Process_Ad_Impressions_Request", 71: "used by the rankings screens",
}


def decode_0202_linux(b):
    """A Linux release's operator settings (Jade 2, 3181 bytes;
    Process_Setup_Options_Request).  The fields are taken from the NVRAM
    option table (80 bytes at NVRAM +0x8E), which the report also carries
    whole, raw at +0x113 and as (index, value) pairs at +0xA6F -- the form a
    0x0201 sets them in; index 0xFE is the volume in percent."""
    f = lambda a, n: ctext(b[a:a + n])
    pairs = {b[i]: b[i + 1] for i in range(0xA6F, len(b) - 1, 2) if b[i] != 0xFF}
    accounts = []
    for k in range(6):
        o = 0x21C + 0x162 * k
        if b[o + 1]:
            accounts.append({"type": b[o + 1], "flags": b[o + 2], "phone": f(o + 3, 120),
                             "login": f(o + 0x7B, 100), "password": f(o + 0xDF, 100),
                             "dns1": f(o + 0x143, 16), "dns2": f(o + 0x153, 15)})
    return {
        "adult_mode": b[0], "nudity": b[1], "fullnude": b[2], "adult_from": b[3], "adult_to": b[4],
        "adult_attract": b[5], "volume_level": b[6], "volume": pairs.get(0xFE, b[6] * 100 // 127),
        "six_star": b[7], "six_star_scores": b[8], "six_star_billboard": b[9], "six_star_volume": b[10],
        "six_star_calibration": b[11], "six_star_update": b[12],
        "six_star_pin": struct.unpack("<I", b[13:17])[0], "adult_content": b[17], "ac_level": b[18],
        "coin_value": f(0x173, 15), "currency": f(0x186, 24).replace("&#36;", "$"),
        "options": [pairs.get(i) for i in range(80)],
        "option_names": {str(k): v for k, v in LINUX_OPTIONS.items()},
        "accounts": accounts, "linux": True,
    }


def decode_0202(b, linux=False):
    """The operator settings (DOS: 285 bytes)."""
    if linux and len(b) >= 0xA6F:
        return decode_0202_linux(b)
    if len(b) < 0x11:
        return None
    return {
        "adult_mode": b[0], "nudity": b[1], "fullnude": b[2], "adult_from": b[3], "adult_to": b[4],
        "adult_attract": b[5], "volume_level": b[6], "volume": b[6] * 100 // 127,
        "six_star": b[7], "six_star_scores": b[8], "six_star_billboard": b[9], "six_star_volume": b[10],
        "six_star_calibration": b[11], "six_star_update": b[12],
        "six_star_pin": struct.unpack("<H", b[13:15])[0], "adult_content": b[15], "ac_level": b[16],
    }


def decode_0212(b, linux=False):
    prices = {}
    for i in range(0, len(b) - 1, 2):
        if b[i] or b[i + 1]:
            prices[str(b[i])] = b[i + 1] & 0x0F
    return {"prices": prices}


def decode_0222(b, linux=False):
    """The Dial-Up Network settings.  A Linux release adds, after the hour and
    three option bytes, a u32 (seconds), and for a MANUAL broadband account
    its IP address and gateway; its "phone" is then the account type
    (AUTOMATIC: DHCP, MANUAL: fixed address, else a dial-up number)."""
    if len(b) < 0x134:
        return None
    f = lambda a, n: ctext(b[a:a + n])
    out = {"init": f(0x000, 100), "prefix": f(0x064, 11), "phone": f(0x06F, 41), "login": f(0x098, 41),
           "password": f(0x0C1, 41), "server": f(0x0EA, 41), "dns1": f(0x113, 16), "dns2": f(0x123, 16),
           "update_hour": b[0x133]}
    if len(b) >= 0x15B:
        out.update(options=list(b[0x134:0x137]), seconds=struct.unpack("<I", b[0x137:0x13B])[0],
                   ip=f(0x13B, 16), gateway=f(0x14B, 16),
                   account={"AUTOMATIC": "broadband (DHCP)", "MANUAL": "broadband (fixed address)"}.get(
                       out["phone"], "dial-up"))
    return out


def decode_00E2(b, linux=False):
    """The last 14 calls.  A Linux release's records are 19 bytes: u32 start,
    u32 (an intermediate time), u32 end, u8 status, u8 error, u8, u32."""
    calls = []
    if linux:
        for i in range(0, len(b) - 18, 19):
            start, mid, end, status, err = struct.unpack("<IIIBB", b[i:i + 14])
            if start:
                calls.append({"start": start, "end": end or mid, "status": status, "error": err})
        return {"calls": calls}
    for i in range(0, len(b) - 13, 14):
        start, end, status, err = struct.unpack("<IIBB", b[i:i + 10])
        if start:
            calls.append({"start": start, "end": end, "status": status, "error": err})
    return {"calls": calls}


def decode_00CA(b, linux=False):
    names = {3: "coins", 0x21: "bills"}
    rows = []
    for i in range(0, len(b) - 13, 14):
        code, idx, a, bb, c = struct.unpack("<BBIII", b[i:i + 14])
        rows.append({"code": code, "what": names.get(code, "game counter %d" % code),
                     "index": idx, "current": a, "lifetime": bb, "since_report": c})
    return {"counters": rows}


LINUX_STATS = 3010      # a Linux release's 0x00C2 / 0x00C3 (Jade 2), without the framing


def decode_stats_linux(b):
    """A Linux release's 0x00C2 / 0x00C3 (Jade 2, 0x00C1 handler
    Process_Books_Request): a 58-byte header, then 128 slots of 23 bytes for
    the games (the first u16-count of them used, the rest 0xFF), then two u32
    credit counts.  Credit types: 0 total (money in), 1 free (the cabinet's
    audit labels these two); 2, 3 and 4 are added only by older paths
    (OldAddCreditsToBooks / StartGame, OldUseMeritMoney, OldUseCredits) --
    a normal play adds to neither (Jade 2)."""
    games_n, total, free, started, unk_a, unk_b = struct.unpack("<HIIIII", b[:22])
    meters = list(struct.unpack("<6H", b[22:34]))
    t_plays, t_credits = struct.unpack("<II", b[34:42])
    y1, m1, c1, y2, m2, c2 = struct.unpack("<HHIHHI", b[42:58])
    used, merit = struct.unpack("<II", b[3002:3010])
    games = []
    for k in range(min(games_n, 128)):
        i = 58 + 23 * k
        g, price, share, plays, credits, short, long_, avg, *by = struct.unpack("<BBBHHHHH5H", b[i:i + 23])
        games.append({"game": g, "price": price, "share": share, "plays": plays, "credits": credits,
                      "shortest": short, "longest": long_, "average": avg,
                      "linked": by[0], "by_players": by[1:]})
    return {"total_credits": total, "free_credits": free, "games_started": started,
            "credits_used": used, "merit_money": merit, "meter_pulses": meters,
            "tournament_plays": t_plays, "tournament_credits": t_credits,
            "months": [{"year": y, "month": m, "credits": c} for y, m, c in ((y1, m1, c1), (y2, m2, c2)) if m],
            "unknown": [unk_a, unk_b], "games": games}


def decode_stats(b, linux=False):
    if linux and len(b) == LINUX_STATS:
        return decode_stats_linux(b)
    """0x00C2 / 0x00C3: the message's first 54 bytes are its header (here
    without the 4 of the framing, like every report kept): u16 games, u32
    total, free and played credits, u16 x6 meter pulses, u32 TournaMAXX
    games and credits, and (0x00C3) this and last month's tournament
    credits with their year and month.  Diamond V6.03's header is 24 bytes
    (MEGACDLL 0x930a4): u16 games, u16 the games' credits, u16 free credits,
    u16 credits played, u16 x6 meter pulses.  Then 23 bytes per
    game played (MEGACDLL 0x9fab8): u8 game, u8 price, u8 share of plays %,
    u16 plays (every player of every game), u16 credits, u16 shortest,
    longest and average play in seconds, u16 x5 games: linked, 1-4 players."""
    if len(b) % 23 == 20:  # Diamond V6.03
        _, games_credits, free, played, *meters = struct.unpack("<4H6H", b[:20])
        out = {"games_credits": games_credits, "free_credits": free, "credits_played": played,
               "meter_pulses": meters}
        start = 20
    elif len(b) >= 50:
        total, free, played = struct.unpack("<3I", b[2:14])
        meters = list(struct.unpack("<6H", b[14:26]))
        t_plays, t_credits = struct.unpack("<2I", b[26:34])
        y1, m1, c1, y2, m2, c2 = struct.unpack("<HHIHHI", b[34:50])
        out = {"total_credits": total, "free_credits": free, "credits_played": played,
               "meter_pulses": meters, "tournament_plays": t_plays, "tournament_credits": t_credits,
               "months": [{"year": y, "month": m, "credits": c}
                          for y, m, c in ((y1, m1, c1), (y2, m2, c2)) if m]}
        start = 50
    else:
        return None
    games = []
    for i in range(start, len(b) - 22, 23):
        g, price, share, plays, credits, short, long_, avg, *by = struct.unpack("<BBBHHHHH5H", b[i:i + 23])
        games.append({"game": g, "price": price, "share": share, "plays": plays, "credits": credits,
                      "shortest": short, "longest": long_, "average": avg,
                      "linked": by[0], "by_players": by[1:]})
    out["games"] = games
    return out


DECODERS = {"0202": decode_0202, "0212": decode_0212, "0222": decode_0222, "00E2": decode_00E2,
            "00CA": decode_00CA, "00C2": decode_stats, "00C3": decode_stats}


def latest_reports(state, serial):
    out = {}
    linux = (state.get("logins", {}).get(serial, {}).get("protocol") or 0) >= 13
    for r in state.get("reports", {}).get(serial, []):
        dec = DECODERS.get(r.get("type"))
        try:
            d = dec(bytes.fromhex(r["raw"]), linux) if dec else None
        except (ValueError, struct.error):
            d = None
        out[r["type"]] = {"at": r.get("at"), "decoded": d, "raw": r["raw"][:4096]}
    return out


def fetched_files(serial):
    root = data("files", serial)
    out = []
    if os.path.isdir(root):
        for dp, _, names in os.walk(root):
            for n in names:
                p = os.path.join(dp, n)
                out.append({"path": os.path.relpath(p, root).replace(os.sep, "/"),
                            "size": os.path.getsize(p), "at": int(os.path.getmtime(p))})
    return sorted(out, key=lambda x: -x["at"])


def cabinet_serials(state):
    s = set(state.get("logins", {})) | set(state.get("locations", {})) | set(state.get("outbox", {}))
    s |= {p.get("cabinet") for p in state.get("players", {}).values() if p.get("cabinet")}
    return sorted(x for x in s if x)


def enriched_state():
    st = get_state()
    extra = {}
    for serial in cabinet_serials(st):
        login = st.get("logins", {}).get(serial, {})
        extra[serial] = {"reports": latest_reports(st, serial), "files": fetched_files(serial),
                         "release": RELEASES.get(login.get("protocol"), "")}
    # The raw report history is large and is summed up above.
    st = dict(st)
    st["reports"] = {k: len(v) for k, v in st.get("reports", {}).items()}
    for p in st.get("players", {}).values():
        p.pop("raw", None)
    for l in st.get("logins", {}).values():
        l.pop("raw", None)
    st["_cabinets"] = extra
    return st


# ------------------------------------------------------------------ the log

def tail(path, max_bytes):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
    except OSError:
        return [], 0
    lines = chunk.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines, size


CALL_RE = re.compile(r"^(\S+) \[(call \d+)\] (.*)$")


def recent_calls(limit=25):
    lines, _ = tail(log_path(), 2 * 1024 * 1024)
    calls, order = {}, []
    for l in lines:
        m = CALL_RE.match(l)
        if not m:
            continue
        t, name, msg = m.groups()
        if msg.startswith("connected from") or msg.startswith("TournaMAXX over TCP"):
            calls[name] = {"name": name, "start": t, "from": msg.split("from ")[-1], "end": None,
                           "port": None, "completed": False, "direct": msg.startswith("TournaMAXX")}
            order.append(name)
            continue
        c = calls.get(name)
        if not c:
            continue
        if "TournaMAXX (port" in msg:
            c["port"] = int(msg.split("port ")[1].split(")")[0])
        elif "<- FF02" in msg:
            c["completed"] = True
        elif msg == "call ended":
            c["end"] = t
    return [calls[n] for n in order[-limit:]][::-1]


# ------------------------------------------------------------------ actions

def safe_name(n):
    n = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(n or "")).strip("._")
    return n or "file"


def edit_player(req):
    """A player's handle, PIN, city and state, in the state and in the record
    the cabinets are sent (0x0081 carries it from +4); every cabinet is sent
    the player again."""
    pid = str(req["id"])
    st = get_state()
    p = st.get("players", {}).get(pid)
    if p is None:
        raise ValueError("no player %s" % pid)
    raw = bytearray(bytes.fromhex(p["raw"]))

    def put(off, n, text):
        b = text.encode("latin1", "replace")[:n - 1]
        raw[off:off + n] = b + b"\0" * (n - len(b))

    fields = {"handle": (0x08, 13), "pin": (0x15, 5), "city": (0x1A, 31), "state": (0x39, 36)}
    for k, (off, n) in fields.items():
        if k in req:
            put(off, n, str(req[k]))
    for k in ("handle", "city", "state"):
        if k in req:
            state_request({"op": "put", "path": ["players", pid, k], "value": str(req[k])})
    state_request({"op": "put", "path": ["players", pid, "raw"], "value": raw.hex()})
    for serial, sent in st.get("players_sent", {}).items():
        if pid in sent:
            state_request({"op": "remove", "path": ["players_sent", serial], "match": pid})
    return True


def build_update(qs, body):
    """An update package from a .zip laid out as the cabinet's C:\\ (mkupdate.py)."""
    name = safe_name(qs.get("name", "update"))
    spec = importlib.util.spec_from_file_location("mkupdate", CFG["mkupdate_script"])
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    out_dir = data("packages", name)
    os.makedirs(out_dir, exist_ok=True)
    msgs = []
    with tempfile.TemporaryDirectory() as tmp:
        folder = os.path.join(tmp, name)
        os.makedirs(folder)
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            for i in z.infolist():
                parts = [p for p in i.filename.replace("\\", "/").split("/") if p]
                if not parts or i.is_dir():
                    continue
                if any(p in ("..", ".") for p in parts) or ":" in i.filename:
                    raise ValueError("bad path in the zip: %s" % i.filename)
                dest = os.path.join(folder, *parts)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(i) as src, open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)
        try:
            entry = mk.make(folder, os.path.join(out_dir, "NETUPDT.EXE"), name, say=msgs.append)
        except mk.Problem as p:
            raise ValueError(str(p))
    return {"entry": entry[name], "log": msgs}


# ------------------------------------------------------------------ HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TournaMAXX-panel"

    def log_message(self, fmt, *args):
        pass

    # -------------------------------------------------------------- plumbing
    def client(self):
        return real_address(self.client_address[0], self.headers.get("X-Real-IP"))

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def send(self, code, body, ctype="application/json", headers=()):
        if isinstance(body, (dict, list)) or ctype == "application/json":
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        if not hasattr(self, "_body"):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise ValueError("too large (the limit is %d MB)" % (MAX_UPLOAD // 1048576))
            self._body = self.rfile.read(n) if n else b""
        return self._body

    def json_body(self):
        b = self.body()
        return json.loads(b.decode("utf-8")) if b else {}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        url = urllib.parse.urlsplit(self.path)
        qs = dict(urllib.parse.parse_qsl(url.query))
        path = url.path
        try:
            if not path.startswith("/api/"):
                return self.static(path)
            if method == "POST" and self.headers.get("X-TMX") != "1":
                return self.send(403, {"error": "missing request header"})
            if path == "/api/login" and method == "POST":
                return self.login()
            if path == "/api/setup":
                return self.first_run(method)
            if path == "/api/public/megalink" and method == "GET":
                res = public_megalink((self.headers.get("Host") or "").split(":")[0])
                if not res["enabled"]:
                    return self.send(404, {"error": "the Mega-Link page is off"})
                return self.send(200, res)
            user = session_user(self.cookie("tmx"))
            if user is None:
                return self.send(401, {"error": "not logged in"})
            self.user = user
            fn = getattr(self, "%s_%s" % (method.lower(), path[5:].replace("/", "_")), None)
            if fn is None:
                return self.send(404, {"error": "no such thing"})
            res = fn(qs)
            if res is not None:
                self.send(200, res)
        except (ValueError, KeyError, RuntimeError, OSError, zipfile.BadZipFile) as e:
            self.send(400, {"error": str(e) or e.__class__.__name__})

    def static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        elif path in ("/megalink", "/megalink/"):
            path = "/megalink.html"
        p = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not p.startswith(STATIC + os.sep) or not os.path.isfile(p):
            return self.send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(p, "rb") as f:
            self.send(200, f.read(), ctype)

    def download(self, path, name):
        with open(path, "rb") as f:
            b = f.read()
        self.send(200, b, "application/octet-stream",
                  [("Content-Disposition", 'attachment; filename="%s"' % safe_name(name))])

    # -------------------------------------------------------------- login
    def login(self):
        addr = self.client()
        req = self.json_body()
        user = req.get("user", "")
        if throttled(addr, user):
            return self.send(429, {"error": "too many attempts: wait five minutes"})
        stored = CFG["users"].get(user) if isinstance(user, str) else None
        if not stored or not check_password(req.get("password", ""), stored):
            failed(addr, user)
            time.sleep(1)
            return self.send(401, {"error": "wrong user name or password"})
        logged_in(addr, user)
        self.start_session(user)

    def start_session(self, user):
        tok = new_session(user)
        # Secure when configured, or when a proxy says the browser came over HTTPS.
        https = CFG.get("secure_cookies") or self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        flags = "; Secure" if https else ""
        self.send(200, {"user": user}, headers=[
            ("Set-Cookie", "tmx=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" %
             (tok, SESSION_HOURS * 3600, flags))])

    def first_run(self, method):
        """GET: whether the first user is still to be made; POST: make it."""
        st = setup_state()
        if method != "POST":
            return self.send(200, st)
        if not st["needed"]:
            return self.send(403, {"error": "the panel already has users: log in"})
        if not st["open"]:
            return self.send(403, {"error": "first-run setup closed %d minutes after the panel started: "
                                            "restart the panel (or the container) to open it again" % SETUP_MINUTES})
        req = self.json_body()
        check_new_user(req.get("user"), req.get("password"))
        with CFG_LOCK:
            if CFG["users"]:
                return self.send(403, {"error": "someone else made the first user just now"})
            CFG["users"][req["user"]] = hash_password(req["password"])
            save_config()
        print("first user made: %s" % req["user"], flush=True)
        self.start_session(req["user"])

    def post_logout(self, qs):
        with AUTH_LOCK:
            SESSIONS.pop(self.cookie("tmx") or "", None)
        self.send(200, {}, headers=[("Set-Cookie", "tmx=; Path=/; Max-Age=0")])

    def get_session(self, qs):
        return {"user": self.user}

    # -------------------------------------------------------------- overview
    def get_overview(self, qs):
        svc = SERVICE.status()
        err = None
        try:
            st = get_state()
        except RuntimeError as e:
            st, err = read_state_file(), str(e)
        mod = server_module()
        tours = st.get("tournaments", [])
        counts = {"tournaments": len(tours),
                  "running": sum(1 for t in tours if mod.tournament_status(t) == 2),
                  "players": len(st.get("players", {})), "scores": len(st.get("scores", [])),
                  "cabinets": len(cabinet_serials(st)),
                  "outbox": sum(len(v) for v in st.get("outbox", {}).values())}
        cabs = []
        for serial in cabinet_serials(st):
            l = st.get("logins", {}).get(serial, {})
            cabs.append({"serial": serial, "name": st.get("locations", {}).get(serial, {}).get("name", ""),
                         "release": RELEASES.get(l.get("protocol"), ""), "version": l.get("version"),
                         "last": l.get("at"), "outbox": len(st.get("outbox", {}).get(serial, []))})
        _, size = tail(log_path(), 0)
        return {"service": svc, "server": CFG["server"], "counts": counts, "cabinets": cabs,
                "calls": recent_calls(), "log_size": size, "error": err, "now": now(),
                "host": socket.gethostname(), "interfaces": interfaces(),
                "panel": {"port": int(CFG["port"]), "listening": listening()}}

    def post_service(self, qs):
        action = self.json_body().get("action")
        if action not in ("start", "stop", "restart"):
            raise ValueError("unknown action")
        SERVICE.apply_settings()
        getattr(SERVICE, action)()
        time.sleep(0.7)
        return SERVICE.status()

    # -------------------------------------------------------------- state
    def get_state(self, qs):
        return enriched_state()

    def post_op(self, qs):
        req = self.json_body()
        if req.get("op") not in ("put", "delete", "append", "remove"):
            raise ValueError("unknown op")
        return {"result": state_request(req)}

    def post_player(self, qs):
        return {"result": edit_player(self.json_body())}

    def get_raw(self, qs):
        return {"text": json.dumps(get_state(), indent=1)}

    def post_raw(self, qs):
        text = self.json_body().get("text", "")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("the state is a JSON object")
        with STORE_LOCK:
            backup_state(force=True)
        LAST_BACKUP[0] = now()
        state_request({"op": "replace", "value": value})
        return {"ok": True}

    def get_backups(self, qs):
        return backups()

    def post_backups_restore(self, qs):
        name = safe_name(self.json_body().get("name"))
        with open(data("backups", name), encoding="utf-8") as f:
            value = json.load(f)
        with STORE_LOCK:
            backup_state(force=True)
        state_request({"op": "replace", "value": value})
        return {"ok": True}

    def post_backups_make(self, qs):
        with STORE_LOCK:
            return {"name": backup_state(force=True)}

    # -------------------------------------------------------------- files
    def post_upload(self, qs):
        """A file for the outbox's send_file; kept in outgoing/."""
        b = self.body()
        os.makedirs(data("outgoing"), exist_ok=True)
        name = "%s-%s" % (datetime.datetime.now().strftime("%Y%m%d%H%M%S"), safe_name(qs.get("name")))
        write_atomic(data("outgoing", name), b)
        return {"path": data("outgoing", name), "size": len(b)}

    def post_updates_upload(self, qs):
        """A ready NETUPDT.EXE for an update package."""
        b = self.body()
        name = safe_name(qs.get("name", "update"))
        os.makedirs(data("packages", name), exist_ok=True)
        p = data("packages", name, "NETUPDT.EXE")
        write_atomic(p, b)
        return {"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}

    def post_updates_build(self, qs):
        return build_update(qs, self.body())

    def get_download(self, qs):
        kind = qs.get("kind")
        if kind == "log":
            return self.download(log_path(), "modem-server.log")
        if kind == "state":
            b = json.dumps(get_state(), indent=1).encode("utf-8")
            return self.send(200, b, "application/octet-stream",
                             [("Content-Disposition", 'attachment; filename="state.json"')])
        if kind == "backup":
            return self.download(data("backups", safe_name(qs.get("name"))), qs.get("name"))
        if kind == "file":
            root = os.path.realpath(data("files", safe_name(qs.get("serial"))))
            p = os.path.realpath(os.path.join(root, qs.get("path", "")))
            if not p.startswith(root + os.sep):
                raise ValueError("bad path")
            return self.download(p, os.path.basename(p))
        raise ValueError("unknown download")

    # -------------------------------------------------------------- log
    def get_log(self, qs):
        n = min(int(qs.get("lines", 400)), 5000)
        q = qs.get("q", "").lower()
        lines, size = tail(log_path(), 4 * 1024 * 1024 if q else 400 * 1024)
        if q:
            lines = [l for l in lines if q in l.lower()]
        return {"lines": lines[-n:], "size": size}

    # -------------------------------------------------------------- settings
    def get_settings(self, qs):
        return {"server": CFG["server"], "service": CFG["service"], "users": sorted(CFG["users"]),
                "data_dir": CFG["data_dir"], "backups": backups(), "panel": self.panel_access()}

    def panel_access(self):
        return {"listen": listen_config(), "listening": listening(), "port": int(CFG["port"]),
                "interfaces": interfaces(), "via": self.connection.getsockname()[0],
                "docker": os.path.exists("/.dockerenv")}

    def post_panel_listen(self, qs):
        """Where the panel listens; applied at once.  Leaving out the address
        this request came in on needs "force"."""
        body = self.json_body()
        addrs = clean_addresses(body.get("listen") or [])
        here = self.connection.getsockname()[0]
        if not covers(addrs, here) and not body.get("force"):
            return {"confirm": "You are connected through %s, which this leaves out: this page stops "
                               "answering there, and you open the panel again on one of the new addresses." % here}
        set_listeners(addrs)
        with CFG_LOCK:
            CFG["listen"] = addrs[0] if len(addrs) == 1 else addrs
            save_config()
        return {"panel": self.panel_access(), "cut": not covers(addrs, here)}

    def post_settings(self, qs):
        body = self.json_body()
        req = body.get("server", {})
        s = dict(CFG["server"])
        port = int(req.get("port", s["port"]))
        tcp = [int(p) for p in req.get("tcp_ports", s.get("tcp_ports", []))]
        admin = int(req.get("admin_port", s.get("admin_port", 2324)))
        switch = int(req.get("switch_port", s.get("switch_port", 0)) or 0)
        dns = int(req.get("dns_port", s.get("dns_port", 0)) or 0)
        dns_answer = str(req.get("dns_answer", s.get("dns_answer", "")) or "").strip()
        if dns_answer:
            socket.inet_aton(dns_answer)          # OSError on a bad address: the request fails
        for p in [port, admin] + tcp + [x for x in (switch, dns) if x]:
            if not 1 <= p <= 65535:
                raise ValueError("port %d out of range" % p)
        if len({port, admin, *tcp}) != 2 + len(tcp):
            raise ValueError("every TCP port must be different")
        s.update(port=port, tcp_ports=tcp, admin_port=admin, switch_port=switch, dns_port=dns, dns_answer=dns_answer)
        with CFG_LOCK:
            CFG["server"] = s
            save_config()
        SERVICE.apply_settings()
        restarted = False
        if body.get("restart") and SERVICE.status()["active"]:
            SERVICE.restart()
            restarted = True
        return {"server": s, "restarted": restarted}

    # -------------------------------------------------------------- Mega-Link
    def get_megalink(self, qs):
        st = get_state()
        return {"config": st.get("megalink", {}), "switch_port": int(CFG["server"].get("switch_port") or 0),
                "status": switch_status(), "running": SERVICE.status()["active"]}

    def post_password(self, qs):
        req = self.json_body()
        if not check_password(req.get("old", ""), CFG["users"].get(self.user, "")):
            raise ValueError("the current password is wrong")
        if len(req.get("new", "")) < 10:
            raise ValueError("use at least 10 characters")
        with CFG_LOCK:
            CFG["users"][self.user] = hash_password(req["new"])
            save_config()
        end_sessions(self.user, keep=self.cookie("tmx"))
        return {"ok": True}

    # -------------------------------------------------------------- users
    def get_users(self, qs):
        return {"users": [{"name": u, "you": u == self.user} for u in sorted(CFG["users"])]}

    def post_users_add(self, qs):
        req = self.json_body()
        check_new_user(req.get("user"), req.get("password"))
        with CFG_LOCK:
            if req["user"] in CFG["users"]:
                raise ValueError("there is already a user %s" % req["user"])
            CFG["users"][req["user"]] = hash_password(req["password"])
            save_config()
        return self.get_users(qs)

    def post_users_password(self, qs):
        """Set another user's password (yours: under Your password)."""
        req = self.json_body()
        if req.get("user") not in CFG["users"]:
            raise ValueError("no such user")
        check_new_user(req["user"], req.get("password"))
        with CFG_LOCK:
            CFG["users"][req["user"]] = hash_password(req["password"])
            save_config()
        end_sessions(req["user"], keep=self.cookie("tmx"))
        return self.get_users(qs)

    def post_users_delete(self, qs):
        name = self.json_body().get("user")
        if name == self.user:
            raise ValueError("you cannot delete yourself")
        with CFG_LOCK:
            if name not in CFG["users"]:
                raise ValueError("no such user")
            if len(CFG["users"]) == 1:
                raise ValueError("the last user cannot be deleted")
            del CFG["users"][name]
            save_config()
        end_sessions(name)
        return self.get_users(qs)


# ------------------------------------------------------------------ network interfaces

def _windows_interfaces():
    """GetAdaptersAddresses: every adapter that is up, by its friendly name."""
    import ctypes
    from ctypes import wintypes

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("sockaddr", ctypes.c_void_p), ("length", ctypes.c_int)]

    class UNICAST(ctypes.Structure):          # IP_ADAPTER_UNICAST_ADDRESS, as far as needed
        pass
    UNICAST._fields_ = [("length", ctypes.c_ulong), ("flags", wintypes.DWORD),
                        ("next", ctypes.POINTER(UNICAST)), ("address", SOCKET_ADDRESS)]

    class ADAPTER(ctypes.Structure):          # IP_ADAPTER_ADDRESSES, as far as OperStatus
        pass
    ADAPTER._fields_ = [("length", ctypes.c_ulong), ("index", wintypes.DWORD),
                        ("next", ctypes.POINTER(ADAPTER)), ("name", ctypes.c_char_p),
                        ("unicast", ctypes.POINTER(UNICAST)), ("anycast", ctypes.c_void_p),
                        ("multicast", ctypes.c_void_p), ("dns", ctypes.c_void_p),
                        ("suffix", ctypes.c_wchar_p), ("description", ctypes.c_wchar_p),
                        ("friendly", ctypes.c_wchar_p), ("mac", ctypes.c_ubyte * 8),
                        ("mac_length", ctypes.c_ulong), ("flags", ctypes.c_ulong), ("mtu", ctypes.c_ulong),
                        ("type", ctypes.c_ulong), ("oper", ctypes.c_int)]

    gaa = ctypes.windll.iphlpapi.GetAdaptersAddresses
    gaa.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    gaa.restype = ctypes.c_ulong
    size = ctypes.c_ulong(16384)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        # AF_UNSPEC; skip anycast, multicast and DNS servers
        r = gaa(0, 0x0E, None, buf, ctypes.byref(size))
        if r != 111:                          # ERROR_BUFFER_OVERFLOW: size now says how much
            break
    if r:
        raise OSError("GetAdaptersAddresses: error %d" % r)
    out = []
    p = ctypes.cast(buf, ctypes.POINTER(ADAPTER))
    while p:
        a = p.contents
        addrs = []
        u = a.unicast
        while u:
            raw = ctypes.string_at(u.contents.address.sockaddr, u.contents.address.length)
            fam = int.from_bytes(raw[:2], "little")
            if fam == 2:
                addrs.append(socket.inet_ntop(socket.AF_INET, raw[4:8]))
            elif fam == 23:
                addrs.append(socket.inet_ntop(socket.AF_INET6, raw[8:24]))
            u = u.contents.next
        if a.oper == 1:                       # IfOperStatusUp
            out.append((a.friendly or a.description or "", addrs))
        p = a.next
    return out


def _linux_interfaces():
    """Each interface's (primary) IPv4 address, and its IPv6 ones."""
    import fcntl
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _, name in socket.if_nameindex():
            found[name] = []
            try:
                r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))   # SIOCGIFADDR
                found[name].append(socket.inet_ntoa(r[20:24]))
            except OSError:
                pass
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                hexaddr, *_, name = line.split()
                found.setdefault(name, []).append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(hexaddr)))
    except OSError:
        pass
    return list(found.items())


def interfaces():
    """[{"name", "addresses"}], loopback first; unnamed where the system does not say."""
    try:
        if sys.platform == "win32":
            found = _windows_interfaces()
        elif sys.platform.startswith("linux"):
            found = _linux_interfaces()
        else:
            raise OSError("no interface list here")
    except (OSError, AttributeError, ImportError):
        addrs = {"127.0.0.1"}
        try:
            addrs.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
        except OSError:
            pass
        found = [("", sorted(addrs))]
    out = []
    for name, addrs in found:
        addrs = sorted(dict.fromkeys(addrs), key=lambda a: (":" in a, socket.inet_pton(
            socket.AF_INET6 if ":" in a else socket.AF_INET, a)))
        if addrs:
            out.append({"name": name, "addresses": addrs,
                        "loopback": all(a.startswith("127.") or a == "::1" for a in addrs)})
    out.sort(key=lambda i: not i["loopback"])
    return out


# ------------------------------------------------------------------ where the panel listens

ALL_V4, ALL_V6 = "0.0.0.0", "::"
LISTEN_LOCK = threading.Lock()
LISTENERS = {}            # address -> PanelServer, each serving in its own thread


class PanelServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    # On Windows SO_REUSEADDR would let the panel share a port another program holds.
    allow_reuse_address = os.name != "nt"

    def __init__(self, host, port):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, port), Handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            # "::" beside "0.0.0.0" on one port: IPv6 only, or the two collide
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        socketserver.TCPServer.server_bind(self)     # HTTPServer's also looks the host's name up
        self.server_name, self.server_port = self.server_address[:2]


def clean_addresses(addrs):
    """Checked, written the standard way, without repeats; a family's wildcard
    replaces that family's other addresses (they cannot share its port)."""
    out = []
    for a in addrs:
        a = str(a).strip().strip("[]")
        if not a:
            continue
        fam = socket.AF_INET6 if ":" in a else socket.AF_INET
        try:
            a = socket.inet_ntop(fam, socket.inet_pton(fam, a))
        except OSError:
            raise ValueError("not an IP address: %s" % a)
        if a not in out:
            out.append(a)
    if ALL_V4 in out:
        out = [a for a in out if ":" in a or a == ALL_V4]
    if ALL_V6 in out:
        out = [a for a in out if ":" not in a or a == ALL_V6]
    if not out:
        raise ValueError("choose at least one address")
    return out


def listen_config():
    """The config's "listen": one address, or a list of them."""
    v = CFG.get("listen") or "127.0.0.1"
    return clean_addresses(v if isinstance(v, list) else str(v).split(","))


def covers(addrs, addr):
    """Whether listening on addrs takes connections to addr."""
    return addr in addrs or (ALL_V6 if ":" in addr else ALL_V4) in addrs


def listening():
    with LISTEN_LOCK:
        return sorted(LISTENERS, key=lambda a: (":" in a, a))


def serve(host, port):
    srv = PanelServer(host, port)
    threading.Thread(target=srv.serve_forever, daemon=True, name="panel %s" % host).start()
    return srv


def close(srv):
    srv.shutdown()
    srv.server_close()


def set_listeners(addrs):
    """Listen on exactly these addresses.  The ones to go are closed first (an
    address and its wildcard cannot both hold the port); if a new one cannot
    be opened, what was there is put back and the error raised."""
    port = int(CFG["port"])
    with LISTEN_LOCK:
        gone = [a for a in LISTENERS if a not in addrs]
        for a in gone:
            close(LISTENERS.pop(a))
        added = []
        try:
            for a in addrs:
                if a not in LISTENERS:
                    LISTENERS[a] = serve(a, port)
                    added.append(a)
        except OSError as e:
            bad = a
            for a in added:
                close(LISTENERS.pop(a))
            for a in gone:
                try:
                    LISTENERS[a] = serve(a, port)
                except OSError as e2:
                    print("could not listen on %s port %d again: %s" % (a, port, e2), flush=True)
            raise ValueError("cannot listen on %s port %d: %s" % (bad, port, e.strerror or e))


def start_listening():
    """The configured addresses; one that is gone (a changed DHCP lease, an
    unplugged adapter) is skipped, and with none left, 127.0.0.1."""
    port = int(CFG["port"])
    addrs = listen_config()
    for a in addrs:
        try:
            LISTENERS[a] = serve(a, port)
        except OSError as e:
            print("cannot listen on %s port %d: %s" % (a, port, e.strerror or e), flush=True)
    if not LISTENERS and addrs != ["127.0.0.1"]:
        print("listening on 127.0.0.1 instead: choose the addresses again under Settings", flush=True)
        try:
            LISTENERS["127.0.0.1"] = serve("127.0.0.1", port)
        except OSError:
            pass
    if not LISTENERS:
        sys.exit("the control panel cannot listen on port %d" % port)
    for a in listening():
        host = "[%s]" % a if ":" in a else a
        where = {ALL_V4: " (every IPv4 address)", ALL_V6: " (every IPv6 address)"}.get(a, "")
        print("control panel on http://%s:%d/%s" % (host, port, where), flush=True)


def main():
    global SERVICE
    ap = argparse.ArgumentParser(description="TournaMAXX-Revival control panel")
    ap.add_argument("--config", default=os.path.join(HERE, "panel.json"))
    ap.add_argument("--set-password", metavar="USER", help="set (or add) a user's password and exit")
    a = ap.parse_args()
    load_config(a.config)
    os.makedirs(CFG["data_dir"], exist_ok=True)
    if a.set_password:
        pw = os.environ.get("TMX_PASSWORD") or getpass.getpass("Password for %s: " % a.set_password)
        try:
            check_new_user(a.set_password, pw)
        except ValueError as e:
            sys.exit(str(e))
        CFG["users"][a.set_password] = hash_password(pw)
        save_config()
        print("password set for %s" % a.set_password)
        return
    try:
        trusted_proxies()
    except ValueError as e:          # a bad "trusted_proxies" in the config
        sys.exit("trusted_proxies: %s" % e)
    STARTED[0] = now()
    if not CFG["users"]:
        print("no users yet: open the panel within %d minutes to make the first one "
              "(or run with --set-password NAME)" % SETUP_MINUTES, flush=True)
    svc = CFG["service"]
    SERVICE = SystemdService(svc.get("unit", "tournamaxx")) if svc.get("mode") == "systemd" else ProcessService()
    try:
        start_listening()
    except ValueError as e:          # a bad "listen" in the config
        sys.exit("listen: %s" % e)
    if svc.get("mode") != "systemd" and svc.get("autostart", True):
        try:
            SERVICE.start()
        except (OSError, RuntimeError) as e:
            print("could not start the server: %s" % e)

    def stop(signum, frame):
        raise KeyboardInterrupt      # docker stop / systemctl stop: stop the server too
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            time.sleep(1)            # the listeners serve in their threads
    except KeyboardInterrupt:
        pass
    finally:
        if svc.get("mode") != "systemd":
            SERVICE.stop()


if __name__ == "__main__":
    main()
