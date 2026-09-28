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

    {"listen": "127.0.0.1", "port": 8080,
     "data_dir": ".",                      state, log, files, packages, backups
     "server_script": "../modem-server.py",
     "service": {"mode": "process"}        the panel runs the server itself
              | {"mode": "systemd", "unit": "tournamaxx"},
     "server": {"port": 2323, "tcp_ports": [], "admin_port": 2324},
     "secure_cookies": false,              true behind HTTPS
     "users": {"admin": "pbkdf2_sha256$..."}}

In systemd mode the server's options go to <data_dir>/server.env, which
the unit reads, and the panel runs "sudo -n systemctl start|stop|restart
<unit>" (deploy/ sets up the sudo rule).  Put it behind a TLS proxy
(deploy/nginx-tournamaxx.conf) rather than on the open internet.
"""

import argparse
import datetime
import getpass
import hashlib
import hmac
import http.server
import importlib.util
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
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
        "server": {"port": 2323, "tcp_ports": [], "admin_port": 2324},
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
        if not s or s[1] < now():
            SESSIONS.pop(tok or "", None)
            return None
        s[1] = now() + SESSION_HOURS * 3600
        return s[0]


def throttled(addr):
    with AUTH_LOCK:
        c = FAILS.get(addr)
        return c is not None and c[0] >= 5 and c[1] > now()


def failed(addr):
    with AUTH_LOCK:
        c = FAILS.setdefault(addr, [0, 0])
        c[0] = c[0] + 1 if c[1] > now() or c[0] < 5 else 1
        c[1] = now() + 300


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
    return args


class ProcessService:
    """The panel runs the server as its own child (trying it out, and in the
    Docker image).  A server that stops without being told to is started
    again, as systemd would."""

    def __init__(self):
        self.proc = None
        self.started = None
        self.wanted = False
        self.lock = threading.RLock()
        threading.Thread(target=self.watch, daemon=True).start()

    def status(self):
        running = self.proc is not None and self.proc.poll() is None
        return {"mode": "process", "active": running, "state": "running" if running else "stopped",
                "pid": self.proc.pid if running else None, "since": self.started if running else None}

    def start(self):
        with self.lock:
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
                raise RuntimeError("the server stopped at once; see server-stderr.txt")

    def stop(self):
        with self.lock:
            self.wanted = False
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
                if self.wanted and self.proc is not None and self.proc.poll() is not None:
                    print("the server stopped (exit %s): starting it again" % self.proc.returncode, flush=True)
                    self.proc = None
                    try:
                        self.start()
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


def decode_0202(b):
    if len(b) < 0x11:
        return None
    return {
        "adult_mode": b[0], "nudity": b[1], "fullnude": b[2], "adult_from": b[3], "adult_to": b[4],
        "adult_attract": b[5], "volume_level": b[6], "volume": b[6] * 100 // 127,
        "six_star": b[7], "six_star_scores": b[8], "six_star_billboard": b[9], "six_star_volume": b[10],
        "six_star_calibration": b[11], "six_star_update": b[12],
        "six_star_pin": struct.unpack("<H", b[13:15])[0], "adult_content": b[15], "ac_level": b[16],
    }


def decode_0212(b):
    prices = {}
    for i in range(0, len(b) - 1, 2):
        if b[i] or b[i + 1]:
            prices[str(b[i])] = b[i + 1] & 0x0F
    return {"prices": prices}


def decode_0222(b):
    if len(b) < 0x134:
        return None
    f = lambda a, n: ctext(b[a:a + n])
    return {"init": f(0x000, 100), "prefix": f(0x064, 11), "phone": f(0x06F, 41), "login": f(0x098, 41),
            "password": f(0x0C1, 41), "server": f(0x0EA, 41), "dns1": f(0x113, 16), "dns2": f(0x123, 16),
            "update_hour": b[0x133]}


def decode_00E2(b):
    calls = []
    for i in range(0, len(b) - 13, 14):
        start, end, status, err = struct.unpack("<IIBB", b[i:i + 10])
        if start:
            calls.append({"start": start, "end": end, "status": status, "error": err})
    return {"calls": calls}


def decode_00CA(b):
    names = {3: "coins", 0x21: "bills"}
    rows = []
    for i in range(0, len(b) - 13, 14):
        code, idx, a, bb, c = struct.unpack("<BBIII", b[i:i + 14])
        rows.append({"code": code, "what": names.get(code, "game counter %d" % code),
                     "index": idx, "current": a, "lifetime": bb, "since_report": c})
    return {"counters": rows}


def decode_stats(b):
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
    for r in state.get("reports", {}).get(serial, []):
        dec = DECODERS.get(r.get("type"))
        try:
            d = dec(bytes.fromhex(r["raw"])) if dec else None
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
        return self.headers.get("X-Real-IP") or self.client_address[0]

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
        if throttled(addr):
            return self.send(429, {"error": "too many attempts: wait five minutes"})
        req = self.json_body()
        stored = CFG["users"].get(req.get("user", ""))
        if not stored or not check_password(req.get("password", ""), stored):
            failed(addr)
            time.sleep(1)
            return self.send(401, {"error": "wrong user name or password"})
        FAILS.pop(addr, None)
        tok = new_session(req["user"])
        flags = "; Secure" if CFG.get("secure_cookies") else ""
        self.send(200, {"user": req["user"]}, headers=[
            ("Set-Cookie", "tmx=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" %
             (tok, SESSION_HOURS * 3600, flags))])

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
                "host": socket.gethostname()}

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
                "data_dir": CFG["data_dir"], "backups": backups()}

    def post_settings(self, qs):
        body = self.json_body()
        req = body.get("server", {})
        s = dict(CFG["server"])
        port = int(req.get("port", s["port"]))
        tcp = [int(p) for p in req.get("tcp_ports", s.get("tcp_ports", []))]
        admin = int(req.get("admin_port", s.get("admin_port", 2324)))
        for p in [port, admin] + tcp:
            if not 1 <= p <= 65535:
                raise ValueError("port %d out of range" % p)
        if len({port, admin, *tcp}) != 2 + len(tcp):
            raise ValueError("every port must be different")
        s.update(port=port, tcp_ports=tcp, admin_port=admin)
        with CFG_LOCK:
            CFG["server"] = s
            save_config()
        SERVICE.apply_settings()
        restarted = False
        if body.get("restart") and SERVICE.status()["active"]:
            SERVICE.restart()
            restarted = True
        return {"server": s, "restarted": restarted}

    def post_password(self, qs):
        req = self.json_body()
        if not check_password(req.get("old", ""), CFG["users"].get(self.user, "")):
            raise ValueError("the current password is wrong")
        if len(req.get("new", "")) < 10:
            raise ValueError("use at least 10 characters")
        with CFG_LOCK:
            CFG["users"][self.user] = hash_password(req["new"])
            save_config()
        return {"ok": True}


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
        if len(pw) < 10:
            sys.exit("use at least 10 characters")
        CFG["users"][a.set_password] = hash_password(pw)
        save_config()
        print("password set for %s" % a.set_password)
        return
    if not CFG["users"]:
        sys.exit("no users yet: run with --set-password admin first")
    svc = CFG["service"]
    SERVICE = SystemdService(svc.get("unit", "tournamaxx")) if svc.get("mode") == "systemd" else ProcessService()
    if svc.get("mode") != "systemd" and svc.get("autostart", True):
        try:
            SERVICE.start()
        except (OSError, RuntimeError) as e:
            print("could not start the server: %s" % e)
    httpd = http.server.ThreadingHTTPServer((CFG["listen"], int(CFG["port"])), Handler)
    httpd.daemon_threads = True

    def stop(signum, frame):
        raise KeyboardInterrupt      # docker stop / systemctl stop: stop the server too
    signal.signal(signal.SIGTERM, stop)
    print("control panel on http://%s:%d/" % (CFG["listen"], CFG["port"]), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if svc.get("mode") != "systemd":
            SERVICE.stop()


if __name__ == "__main__":
    main()
