#!/usr/bin/env python3
"""
TournaMAXX-Revival: a TournaMAXX server for DOS Megatouch MAXX cabinets.

Something for an emulated modem to dial (MegaPPBox's: Tools > Modem
settings..., "Dial out to a TCP/IP host", 127.0.0.1 and the port below; any
number the cabinet dials connects here).  The Megatouch releases that go on
line dial an ISP and run PPP with their own TCP/IP stack, so this server:

  * answers PPP: LCP (no authentication asked for), IPCP (the cabinet gets
    10.0.2.15, this server is 10.0.2.2, DNS 10.0.2.3); anything else is
    protocol-rejected;
  * answers DNS queries for any name with 10.0.2.2, and ping;
  * accepts the cabinet's TCP connections: port 15000 (Initial Connection)
    and 17751 (the update call), and speaks TournaMAXX there: tournaments,
    player registration, scores, rankings, locations, the operator's outbox
    (messages, files, settings, ...) and update packages (mkupdate.py);
  * logs everything: every PPP frame, IP packet and TournaMAXX message.

What it knows and keeps lives in the state file (docs/tournamaxx.md, "Running
a server"); editing it while the server runs is fine.  Several cabinets can
call at once.  Clients: Emerald 2 V9.0x, Emerald V8.04, Double Diamond V7.01,
Diamond V6.03.

    python modem-server.py [--port 2323] [--log modem-server.log]
                           [--state modem-server-state.json]

Stop it with Ctrl+C.
"""

import argparse
import datetime
import socket
import struct
import sys
import os
import threading
import time
import hashlib
import json

OUR_IP  = bytes([10, 0, 2, 2])
PEER_IP = bytes([10, 0, 2, 15])
DNS_IP  = bytes([10, 0, 2, 3])

PROTO_IP   = 0x0021
PROTO_LCP  = 0xC021
PROTO_IPCP = 0x8021

LOG = None
# One lock for everything the calls share: the state, the state file and the
# log.  Each PPP frame is handled whole under it (a few milliseconds), so
# calls that are on line at the same time take turns and never interleave
# half way through a change -- two cabinets registering players at once get
# different IDs.  Re-entrant: log() and save_state() take it again.
LOCK = threading.RLock()


def log(conn, msg):
    line = "%s [%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3], conn, msg)
    with LOCK:
        print(line, flush=True)
        if LOG:
            LOG.write(line + "\n")
            LOG.flush()


def hexs(b, n=64):
    s = b[:n].hex(" ")
    return s + (" ..." if len(b) > n else "")


# ---------------------------------------------------------------- HDLC framing

def fcs16(data):
    fcs = 0xFFFF
    for c in data:
        fcs ^= c
        for _ in range(8):
            fcs = (fcs >> 1) ^ 0x8408 if fcs & 1 else fcs >> 1
    return fcs ^ 0xFFFF


def frame(proto, payload):
    body = bytes([0xFF, 0x03]) + struct.pack(">H", proto) + payload
    body += struct.pack("<H", fcs16(body))
    out = bytearray([0x7E])
    for c in body:
        if c < 0x20 or c in (0x7E, 0x7D):
            out += bytes([0x7D, c ^ 0x20])
        else:
            out.append(c)
    out.append(0x7E)
    return bytes(out)


# ---------------------------------------------------------------- IP helpers

def ip_checksum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def ip_packet(proto, src, dst, payload, ident=0):
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, 0, 64, proto, 0, src, dst)
    hdr = hdr[:10] + struct.pack("!H", ip_checksum(hdr)) + hdr[12:]
    return hdr + payload


def dotted(b):
    return ".".join(str(x) for x in b)


def cstr(s, n):
    b = s.encode("latin1")[:n - 1]
    return b + b"\0" * (n - len(b))


def ctext(b):
    return b.split(b"\0")[0].decode("latin1")


# ------------------------------------------------------------ TournaMAXX state
#
# What the server knows between calls lives in a JSON file next to the log
# (--state): the tournaments it hands out, the players it has given permanent
# IDs to, and every score the cabinets have uploaded.  It can be edited while
# the server runs: each call reads it again if it changed.  Delete the file
# to start again.

STATE = None
STATE_PATH = None


def default_state():
    now = int(time.time())
    return {
        "next_player_id": 1001,
        # GAME is the game's number in the cabinet's launcher (MEGACDLL
        # 0x55dc0): 48 Wild 8, 13 Zip 21, 15 Quick Match.  Times are Unix
        # times; the cabinet is sent them as seconds from now.
        "tournaments": [
            dict(id=1, game=48, status=2, start=now - 3600, end=now + 7 * 86400,
                 credits=1, gameopts=0, name="TOURNAMAXX TEST",
                 desc="A test tournament from TournaMAXX-Revival",
                 randseed=12345, seedinc=1,
                 groups=["NATIONAL", "REGIONAL", "LOCAL"],
                 prizes=["FIRST PRIZE", "SECOND PRIZE", "THIRD PRIZE"],
                 showdate=now - 3600),
        ],
        "players": {},      # permanent id -> what the cabinet sent
        "scores": [],       # every uploaded score entry
    }


STATE_MTIME = None   # the file's time when last read or written by us


def load_state(path):
    global STATE, STATE_PATH, STATE_MTIME
    STATE_PATH = path
    try:
        with open(path, encoding="utf-8") as f:
            STATE = json.load(f)
        STATE_MTIME = os.path.getmtime(path)
    except (OSError, ValueError):
        STATE = default_state()
        save_state()


def reload_state_if_changed():
    """Called as each call starts: the state file may have been edited by
    hand since (tournaments, locations, the outbox), so read it again.  A
    file that does not parse -- an edit half done -- is left alone."""
    global STATE, STATE_MTIME
    try:
        mtime = os.path.getmtime(STATE_PATH)
    except OSError:
        return
    if mtime == STATE_MTIME:
        return
    try:
        with LOCK:
            with open(STATE_PATH, encoding="utf-8") as f:
                STATE = json.load(f)
            STATE_MTIME = mtime
        log("server", "state file changed on disk: read it again")
    except ValueError as e:
        log("server", "state file changed but does not parse (%s): keeping the old state" % e)


def save_state():
    global STATE_MTIME
    with LOCK:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=1)
        STATE_MTIME = os.path.getmtime(STATE_PATH)


def operator_page(lines):
    """0x0A11's body: the cabinet writes 1500 bytes of it to
    D:\\Database\\Operate.txt, which SETUP.DLL (0x19d24) reads as up to 15
    lines of 100 bytes, stopping at the first empty one, and shows during
    Initial Connection above "IS THE FOLLOWING INFORMATION CORRECT?"."""
    body = b"".join(cstr(l, 100) for l in lines if l)[:1400]
    return body + b"\0" * (1500 - len(body))


def tournament_status(t, now=None):
    """What a tournament is, by the clock (MEGACDLL 0xa419c): 1 announced,
    2 running, 4 final (winners shown for final_days after END), 5 to be
    removed.  The cabinet itself moves 1 to 2 at START and 2 to 3 at END;
    4 and 5 only ever come from the server."""
    now = now or int(time.time())
    if now < t["start"]:
        return 1
    if now < t["end"]:
        return 2
    if now < t["end"] + t.get("final_days", 7) * 86400:
        return 4
    return 5


def tournament_body(t, status):
    """0x0021, 498-byte body (MEGACDLL 0x9d668 -> tourney.dbf)."""
    now = int(time.time())
    b = struct.pack("<IIIiiII", t["id"], t["game"], status, t["start"] - now,
                    t["end"] - now, t["credits"], t["gameopts"])
    b += cstr(t["name"], 51) + cstr(t["desc"], 101)
    b += struct.pack("<II", t["randseed"], t["seedinc"])
    b += b"".join(cstr(g, 51) for g in t["groups"])
    b += b"".join(cstr(p, 51) for p in t["prizes"])
    b += struct.pack("<i", t["showdate"] - now)
    assert len(b) == 0x1F6 - 4, len(b)
    return b


# Read-only questions asked at the end of an update call.  An empty 0x0201,
# 0x0211 or 0x0221 reads a settings block (with a body it would set it);
# 0x00C1 and 0x00E1 are reports.  0x00C9 is left out: the cabinet clears the
# counters it reports.
REPORTS = [
    (0x0201, "operator settings?"),
    (0x0211, "per-game settings?"),
    (0x0221, "dial-up settings?"),
    (0x00C1, "game statistics?"),
    (0x00E1, "report E1?"),
]

# The commands each older client answers, by the protocol version in its
# login (their dispatchers: Double Diamond V7.01 MEGACDLL 0x95bae, a table;
# Diamond V6.03 0x9083f, a chain of compares).  Emerald V8.04 (7) and
# Emerald 2 (9) answer everything sent here; so do versions not listed.
KNOWS = {
    6: {0x0011, 0x0021, 0x0041, 0x0051, 0x0067, 0x0071, 0x0073, 0x0081, 0x00B1,
        0x00C1, 0x00C9, 0x00D1, 0x00E1, 0x0101, 0x0103, 0x0111, 0x0112, 0x0121,
        0x0A01, 0x0A11, 0xFF01},
    3: {0x0011, 0x0021, 0x0031, 0x0041, 0x0051, 0x0067, 0x0071, 0x0081, 0x00B1,
        0x00C1, 0x00D1, 0x0101, 0x0103, 0x0111, 0x0112, 0x0121, 0x0A01, 0x0A11,
        0xFF01, 0xFF11},
}
# The command an outbox item starts with.
OUTBOX_COMMAND = {"message": 0x0A11, "delete": 0x0121, "send_file": 0x0111,
                  "fetch_file": 0x0101, "settings": 0x0201, "prices": 0x0211,
                  "dialup": 0x0221, "isp": 0x0A01, "location_entry": 0x00D1,
                  "counters": 0x00C9}


class TournaMaxx:
    """The TournaMAXX server end, as far as it is known (DOS MAXX, Emerald 2).

    Messages both ways are [type u16][length u16][body], little-endian, the
    length counting the 4-byte header.  The server speaks first and drives;
    the cabinet answers each command with the next type up.  See
    docs/tournamaxx.md.  Port 15000 is the Initial Connection (no databases
    open: login and COMPLETE only); port 17751 (0x4557) is the update call.
    """

    def __init__(self, name, port=15000):
        reload_state_if_changed()      # hand edits take effect on the next call
        self.name = name
        self.port = port
        self.buf = b""
        self.pending = []
        self.serial = ""
        self.rank_msgs = 0
        self.reports = None
        self.asked = 0
        self.box_item = None
        self.box_data = b""
        self.box_pos = 0
        self.box_done = False
        # What this call has delivered, as (kind, key) pairs: kept only once
        # the cabinet answers COMPLETE with 0xFF02.  A call that breaks off
        # is rolled back by the cabinet (Emerald V8.04 restores its
        # databases), so everything in it has to go again next time.
        self.uncommitted = []
        self.loc_queue = None
        self.protocol = None
        self.version = None
        self.updates_checked = False

    def knows(self, t):
        """Whether this cabinet's client answers command t."""
        known = KNOWS.get(self.protocol)
        return known is None or t in known

    @staticmethod
    def msg(t, body=b""):
        return struct.pack("<HH", t, 4 + len(body)) + body

    def say(self, t, body=b"", what=""):
        log(self.name, "  TournaMAXX: -> %04X %s" % (t, what))
        return self.msg(t, body)

    def opened(self):
        log(self.name, "  TournaMAXX (port %d): hello" % self.port)
        return self.say(0x0011, what="hello")

    def next_tournament(self):
        t, status = self.pending.pop(0)
        if status in (1, 2):
            self.mark("had", t["id"])
        return self.say(0x0021, tournament_body(t, status),
                        "tournament %d, %r, status %d" % (t["id"], t["name"], status))

    def delivered(self, kind, key):
        """Whether this cabinet has had key already: tournament key announced
        or running (kind "had"), its final ("final") or removal
        ("removed"), or player key (kind "players_sent", by serial)."""
        if (kind, str(key)) in self.uncommitted:
            return True
        if kind == "players_sent":
            return str(key) in STATE.get(kind, {}).get(self.serial, [])
        return self.serial in STATE.get(kind, {}).get(str(key), [])

    def mark(self, kind, key):
        if not self.delivered(kind, key):
            self.uncommitted.append((kind, str(key)))

    def commit(self):
        """The cabinet said COMPLETE: what this call delivered is kept."""
        for kind, key in self.uncommitted:
            if kind == "players_sent":
                STATE.setdefault(kind, {}).setdefault(self.serial, []).append(key)
            else:
                STATE.setdefault(kind, {}).setdefault(key, []).append(self.serial)
        if self.uncommitted:
            log(self.name, "  TournaMAXX: kept %d deliveries" % len(self.uncommitted))
        self.uncommitted = []
        save_state()

    def closing(self):
        """After the rankings: STATUS 4 for tournaments that have ended (the
        cabinet archives their rows into SC<id>.dbf, which the winners
        screen reads, so the final rankings have to be in first), STATUS 5
        for those whose winners have been shown long enough."""
        todo = []
        for t in STATE["tournaments"]:
            st = tournament_status(t)
            if not self.delivered("had", t["id"]):
                continue            # never here: a final for it stalls V8.04
            if st == 4 and not self.delivered("final", t["id"]):
                todo.append((t, 4))
                self.mark("final", t["id"])
            elif st == 5 and not self.delivered("removed", t["id"]):
                todo.append((t, 5))
                self.mark("removed", t["id"])
        return todo

    def register(self, body):
        """0x0052 with a player: give them a permanent ID.  The answer is the
        cabinet's own record back as 0x0051, with the ID at +4 (MEGACDLL
        0x9db94 writes it over the player's temporary one)."""
        temp = struct.unpack("<I", body[0:4])[0]
        pid = STATE["next_player_id"]
        STATE["next_player_id"] += 1
        STATE["players"][str(pid)] = {
            "tempid": temp, "cabinet": self.serial,
            "handle": ctext(body[0x08:0x15]), "city": ctext(body[0x1A:0x39]).strip(),
            "state": ctext(body[0x39:0x5D]).strip(), "raw": body.hex()}
        save_state()
        log(self.name, "  player: temporary %d -> permanent %d" % (temp, pid))
        reply = struct.pack("<I", pid) + body[4:]
        return self.say(0x0051, reply, "player %d" % pid)

    def store_scores(self, body):
        for i in range(0, len(body) - 51, 52):
            e = body[i:i + 52]
            tourn, player, newplays = struct.unpack("<III", e[0:12])
            scores = list(struct.unpack("<5I", e[12:32]))
            dates = list(struct.unpack("<5I", e[32:52]))
            STATE["scores"].append(dict(tournament=tourn, player=player, newplays=newplays,
                                        scores=scores, dates=dates, at=int(time.time()),
                                        cabinet=self.serial))
            log(self.name, "  score: tournament %d, player %d, %d new plays, scores %s" %
                (tourn, player, newplays, scores))
        save_state()

    def rankings(self):
        """0x0073 messages: every tournament's standings, in the three groups
        the cabinet shows (0 National: everyone; 1 Regional: players of the
        same state as this cabinet's; 2 Local: players of this cabinet).
        Entry, 34 bytes (MEGACDLL 0x9ea94): u16 group, u32 tournament,
        u32 player, u32 rank, u32 score[5] (the total is their sum).  At most
        60 entries a message."""
        # A cabinet uploads only the scores played since its last call (the
        # rankings it is sent replace its own table), so a player's standing
        # is their best five over every upload.  The cabinet shows the total
        # divided by five.
        allsc = {}
        for s in STATE["scores"]:
            allsc.setdefault((s["tournament"], s["player"]), []).extend(x for x in s["scores"] if x)
        best = {k: (sorted(v, reverse=True) + [0] * 5)[:5] for k, v in allsc.items()}
        players = STATE["players"]
        here = {k for k, p in players.items() if p.get("cabinet") == self.serial}
        states = {players[k].get("state") for k in here}
        entries = []
        # Standings go out while a tournament runs, and once more with its
        # final; after that the cabinet keeps them in SC<id>.dbf.
        live = set()
        for t in STATE["tournaments"]:
            st = tournament_status(t)
            if st in (1, 2) or (st == 4 and not self.delivered("final", t["id"])):
                live.add(t["id"])
        for tid in sorted({k[0] for k in best} & live):
            field = [(sum(sc), pid, sc) for (t, pid), sc in best.items() if t == tid]
            field.sort(key=lambda x: -x[0])
            for group in range(3):
                rank = 0
                for total, pid, sc in field:
                    p = players.get(str(pid), {})
                    if group == 1 and p.get("state") not in states:
                        continue
                    if group == 2 and str(pid) not in here:
                        continue
                    rank += 1
                    entries.append(struct.pack("<HIII5I", group, tid, pid, rank, *sc))
        if not self.knows(0x0073):
            return self.rankings_71(entries)
        out = b""
        for i in range(0, len(entries), 60):
            chunk = entries[i:i + 60]
            out += self.say(0x0073, b"".join(chunk), "rankings (%d entries)" % len(chunk))
        return out, (len(entries) + 59) // 60

    def rankings_71(self, entries):
        """The same standings as 0x0071, for clients without 0x0073 (Diamond
        V6.03): one 92-byte entry per player and tournament (MEGACDLL V8.04
        0x9c270), u32 tournament, u32 player, u32 total[3], u32 rank[3],
        u32 score[3][5], by group; rank -1 where the player is not ranked.
        At most 20 entries a message, each answered by an empty 0x0072."""
        rows = {}
        for e in entries:
            group, tid, pid, rank = struct.unpack("<HIII", e[:14])
            sc = struct.unpack("<5I", e[14:34])
            r = rows.setdefault((tid, pid), ([0] * 3, [0xFFFFFFFF] * 3, [[0] * 5 for _ in range(3)]))
            r[0][group], r[1][group], r[2][group] = sum(sc), rank, list(sc)
        body = [struct.pack("<II3I3I15I", tid, pid, *tot, *rk, *(x for g in sc for x in g))
                for (tid, pid), (tot, rk, sc) in sorted(rows.items())]
        out = b""
        for i in range(0, len(body), 20):
            chunk = body[i:i + 20]
            out += self.say(0x0071, b"".join(chunk), "rankings, old form (%d players)" % len(chunk))
        return out, (len(body) + 19) // 20

    def received(self, data):
        self.buf += data
        out = b""
        while len(self.buf) >= 4:
            t, n = struct.unpack("<HH", self.buf[:4])
            if n < 4 or len(self.buf) < n:
                break
            m, self.buf = self.buf[:n], self.buf[n:]
            body = m[4:]
            txt = "".join(chr(x) if 32 <= x < 127 else "." for x in body[:96])
            log(self.name, "  TournaMAXX: <- %04X, %d bytes: %s" % (t, n, hexs(body, 96)))
            log(self.name, "              as text: %s" % txt)

            # The update call: tournaments out, then what the cabinet holds,
            # its new players, its scores; then COMPLETE.
            if t == 0x0012:
                self.serial = ctext(body[2:16])   # the machine serial
                self.protocol = struct.unpack("<H", body[0:2])[0]
                if self.protocol in (3, 6) and len(body) >= 0x3F:
                    # The 81-byte login of Diamond and Double Diamond carries
                    # the game's version, scanned from its version text
                    # ("PG3002 V%d.%d "): major at +37, minor at +3B.
                    self.version = "%d.%02d" % struct.unpack("<II", body[0x37:0x3F])
                STATE.setdefault("logins", {})[self.serial] = {
                    "protocol": self.protocol, "version": self.version, "port": self.port,
                    "at": int(time.time()), "raw": body.hex()}
                save_state()
            if t == 0xFF02:
                self.commit()
            if t == 0x0012 and self.port != 17751:
                # Initial Connection: the registration Merit holds for the
                # site, which SETUP.DLL shows with "IS THE FOLLOWING
                # INFORMATION CORRECT?", then COMPLETE.
                loc = STATE.setdefault("locations", {}).get(self.serial, {})
                lines = [loc.get("name", ""), loc.get("city_state", ""), loc.get("country", ""),
                         loc.get("telephone", ""), "MACHINE SERIAL %s" % self.serial]
                out += self.say(0x0A11, operator_page(lines), "registration page")
                out += self.say(0xFF01, b"COMPLETE.\0", "COMPLETE.")
            elif t == 0x0012:
                # Announced and running tournaments go out first.
                self.pending = [(x, tournament_status(x)) for x in STATE["tournaments"]
                                if tournament_status(x) in (1, 2)]
                if self.pending:
                    out += self.next_tournament()
                else:
                    out += self.say(0x0041, what="which tournaments?")
            elif t == 0x0022 and self.pending:
                out += self.next_tournament()
            elif t == 0x0022 and self.reports is not None:
                out += self.run_outbox()         # the closing 0x0021s are done
            elif self.box_item and (t in (0x0102, 0x0104, 0x0113, 0x0114, 0x0122, 0x0202, 0x0212,
                                          0x0222, 0x00D2, 0xFF02)
                                    or self.box_item.get("do") == "counters"):
                out += self.outbox_answer(t, body)
            elif t == 0x0022:
                out += self.say(0x0041, what="which tournaments?")
            elif t == 0x0042:
                # Players registered at the cabinet come up one per 0x0051;
                # the empty 0x0052 that ends them also readies the first
                # score batch, so 0x0067 has to come after.
                out += self.say(0x0051, what="new players?")
            elif t == 0x0052 and n > 4:
                out += self.register(body)
            elif t == 0x0052:
                out += self.say(0x0067, what="scores?")
            elif t == 0x0068 and n > 4:
                self.store_scores(body)
                out += self.say(0x0067, what="more scores?")
            elif t == 0x0068:
                # Scores are in.  Players from other cabinets first (their
                # handles are what the rankings show), then the standings
                # (each 0x0073 answered by an empty 0x0074).
                self.reports = [r for r in REPORTS if self.knows(r[0])]
                out += self.after_scores()
            elif t in (0x0082, 0x00D2) and self.rank_msgs == 0 and self.box_item is None:
                out += self.after_scores()
            elif t in (0x0074, 0x0072):
                self.rank_msgs -= 1
                if self.rank_msgs <= 0:
                    out += self.after_rankings()
            elif self.reports is not None and self.asked:
                # Report answers are kept (a request may bring more than one
                # message); the answer to the current request moves on.
                STATE.setdefault("reports", {}).setdefault(self.serial, []).append(
                    {"type": "%04X" % t, "at": int(time.time()), "raw": body.hex()})
                save_state()
                if t in (self.asked + 1, 0xFF02):
                    out += self.next_report()
        return out

    def location(self):
        """0x00B1 (no answer): the cabinet's LOCATION INFO screen (NAME, CITY
        STATE, COUNTRY, TELEPHONE #), kept in C:\\NTNVRAM.DAT.  Four strings
        of 51 at +11, +44, +77, +AA; the thirteen option bytes before them
        are only stored when below 3, so 0xFF leaves them as they are.  The
        text comes from the state file's "locations", by machine serial."""
        loc = STATE.setdefault("locations", {}).setdefault(self.serial, {
            "name": "TOURNAMAXX CABINET", "city_state": "SET IN modem-server-state.json",
            "country": "", "telephone": ""})
        save_state()
        body = b"\xff" * 13 + b"".join(
            cstr(loc.get(k, ""), 51) for k in ("name", "city_state", "country", "telephone"))
        return self.say(0x00B1, body, "location %r" % loc.get("name"))

    def send_rankings(self):
        ranks, self.rank_msgs = self.rankings()
        return ranks if ranks else self.after_rankings()

    def after_scores(self):
        """Once the scores are in: the locations the players belong to (each
        0x00D1 answered by 0x00D2), the players (0x0081, answered by 0x0082),
        then the standings."""
        if self.loc_queue is None:
            self.loc_queue = self.locations_needed()
        if self.loc_queue:
            key, rec, what = self.loc_queue.pop(0)
            self.mark("locations_sent", key)
            return self.say(0x00D1, rec, what)
        players = self.players_to_send()
        return players if players else self.send_rankings()

    @staticmethod
    def location_id(serial):
        """A cabinet's location number: its machine serial."""
        return int(serial) if str(serial).isdigit() else 0

    def location_record(self, serial):
        """0x00D1, 154 bytes for Location.dbf: u32 ID, NAME[52], CITY[31],
        STATE[36], COUNTRY[31], from the cabinet's "locations" entry (the
        one its LOCATION INFO screen gets)."""
        loc = STATE.get("locations", {}).get(serial, {})
        city, _, state = loc.get("city_state", "").partition(",")
        return (struct.pack("<I", self.location_id(serial)) + cstr(loc.get("name", ""), 52) +
                cstr(city.strip(), 31) + cstr(state.strip(), 36) + cstr(loc.get("country", ""), 31))

    def locations_needed(self):
        """The locations of the players about to be sent, where this cabinet
        does not have them as they are now.  Rankings show a player's
        location name; Diamond V6.03 shows "Unknown" when it has none."""
        todo = []
        for cab in sorted({p.get("cabinet") for _, p in self.unsent_players()}):
            if not self.location_id(cab):
                continue
            rec = self.location_record(cab)
            key = "%s:%s" % (cab, hashlib.sha1(rec).hexdigest()[:12])
            if not self.delivered("locations_sent", key):
                todo.append((key, rec, "location %s, %r" % (cab, ctext(rec[4:56]))))
        return todo

    def unsent_players(self):
        return [(pid, p) for pid, p in STATE["players"].items()
                if not self.delivered("players_sent", pid)]

    def players_to_send(self):
        """0x0081: players this cabinet has not been sent yet, its own
        included, so that every ranked player has a handle, city, state and
        location there.  Entries of 97 bytes (MEGACDLL 0x9e6bc): u32 ID, then
        the player's record as their cabinet sent it (from +4: u32, HANDLE,
        PIN, CITY, STATE), u32 LOCATION (the home cabinet's location number,
        see location_record).  At most 20 a message; the cabinet answers
        0x0082 and gets the next batch."""
        todo = self.unsent_players()[:20]     # 20 x 97 bytes fit a message
        if not todo:
            return b""
        body = b""
        for pid, p in todo:
            raw = bytes.fromhex(p["raw"])
            body += (struct.pack("<I", int(pid)) + raw[4:0x5D] +
                     struct.pack("<I", self.location_id(p.get("cabinet", ""))))
            self.mark("players_sent", pid)
        return self.say(0x0081, body, "%d players" % len(todo))

    def after_rankings(self):
        """The location, finals and removals (each 0x0021 answered by
        0x0022), the outbox, then the reports."""
        out = self.location()
        self.pending = self.closing()
        if self.pending:
            return out + self.next_tournament()
        return out + self.run_outbox()

    # ------------------------------------------------------------ the outbox
    #
    # What the operator has queued for a cabinet, in the state file under
    # "outbox" -> machine serial, done at its next update call:
    #   {"do": "message", "text": "..."}                 0x0A11
    #   {"do": "send_file", "from": "local", "to": "C:\\PATH"}   0x0111/0x0112
    #   {"do": "fetch_file", "path": "C:\\PATH"}          0x0101/0x0103, saved
    #                                                  under files\<serial>\
    #   {"do": "delete", "path": "C:\\PATH", "reboot": false}    0x0121
    #                                  (an empty path only reboots)
    # Done items move to "outbox_done".
    #
    # Update packages, in the state file under "updates" -> name:
    #   {"protocol": 7, "send": [["local file", "C:\\PATH"], ...],
    #    "result": "C:\\RESULT.TXT"}
    # or, for Diamond and Double Diamond (whose logins carry the version, and
    # whose TEST.BAT runs only NetUpdt.exe, so there is no result file):
    #   {"protocol": 6, "version": "7.01", "becomes": "7.20", "send": [...]}
    # "version" limits it to cabinets logging in with that version; the
    # update counts as installed when the cabinet logs in as "becomes".
    # go once to every cabinet whose login carries that protocol version:
    # the files, then a reboot.  A C:\NETUPDT.BAT among them is run by the
    # cabinet's TEST.BAT at boot, then deleted.  At the next call the result
    # file is fetched and deleted; its text becomes the cabinet's entry in
    # "update_status" -> name -> serial.

    CHUNK = 1024

    def queue_updates(self):
        box = STATE.setdefault("outbox", {}).setdefault(self.serial, [])
        for name, u in STATE.get("updates", {}).items():
            if u.get("protocol") not in (None, self.protocol):
                continue
            status = STATE.setdefault("update_status", {}).setdefault(name, {})
            st = status.get(self.serial)
            if any(i.get("update") == name for i in box):
                continue                              # still going out
            if st == "queued" and u.get("becomes") and not u.get("result"):
                if self.version == u["becomes"]:
                    status[self.serial] = "installed: logs in as V%s" % self.version
                else:
                    status[self.serial] = "not installed: still logs in as V%s" % self.version
                log(self.name, "  update %s: %s" % (name, status[self.serial]))
                continue
            if u.get("version") and self.version != u["version"]:
                continue
            if st is None:
                items = [{"do": "send_file", "from": src, "to": dst, "update": name}
                         for src, dst in u.get("send", [])]
                items.append({"do": "delete", "path": "", "reboot": True, "update": name})
                status[self.serial] = "queued"
                # The update may clear the cabinet's tournaments: it gets the
                # running ones again, and no finals for the others.
                for sers in STATE.get("had", {}).values():
                    if self.serial in sers:
                        sers.remove(self.serial)
                self.uncommitted = [x for x in self.uncommitted if x[0] != "had"]
            elif st == "queued" and u.get("result"):
                items = [{"do": "fetch_file", "path": u["result"], "update": name},
                         {"do": "delete", "path": u["result"], "reboot": False, "update": name}]
                status[self.serial] = "checking"
            elif st == "checking":
                # The result fetch broke off (a fetched result is recorded
                # when it arrives): ask again.
                items = [{"do": "fetch_file", "path": u["result"], "update": name},
                         {"do": "delete", "path": u["result"], "reboot": False, "update": name}]
            else:
                continue
            box[0:0] = items
            log(self.name, "  update %s: %s" % (name, status[self.serial]))
        save_state()

    def run_outbox(self):
        if not self.updates_checked and self.port == 17751:
            self.updates_checked = True
            self.queue_updates()
        box = STATE.setdefault("outbox", {}).setdefault(self.serial, [])
        out = b""
        while box:
            item = box[0]
            what = item.get("do")
            if not self.knows(OUTBOX_COMMAND.get(what, 0)):
                self.finish_item("not for this cabinet (its client, protocol %s, has no %04X)"
                                 % (self.protocol, OUTBOX_COMMAND.get(what, 0)))
                continue
            if what == "message":
                out += self.say(0x0A11, operator_page(item.get("text", "").split("\n")),
                                "operator page")
                self.finish_item("sent")
                continue
            self.box_item = item
            if what == "delete":
                body = bytes([1 if item.get("reboot") else 0]) + item.get("path", "").encode("latin1") + b"\0"
                return out + self.say(0x0121, body, "delete %r, reboot %s" % (item.get("path"), bool(item.get("reboot"))))
            if what == "send_file":
                try:
                    with open(item["from"], "rb") as f:
                        self.box_data = f.read()
                except OSError as e:
                    self.box_item = None
                    self.finish_item("cannot read %s: %s" % (item.get("from"), e))
                    continue
                self.box_pos = 0
                self.box_done = False
                body = b"\0" + item["to"].encode("latin1") + b"\0"
                return out + self.say(0x0111, body, "send %s -> %s (%d bytes)" %
                                      (item["from"], item["to"], len(self.box_data)))
            if what == "fetch_file":
                self.box_data = b""
                body = b"\0" + item["path"].encode("latin1") + b"\0"
                return out + self.say(0x0101, body, "fetch %s" % item["path"])
            if what == "settings":
                # 0x0201 with a body: the last 0x0202 this cabinet sent,
                # with the bytes named in "set" ({"+0A": 30, ...}, message
                # offsets) changed.  +15 is the start of the game list:
                # 0xFF there leaves the games alone.
                base = self.last_report("0202")
                if base is None:
                    self.box_item = None
                    self.finish_item("no 0x0202 read yet")
                    continue
                b = bytearray(b"\0\0\0\0" + base[:0x11]) + b"\xff"
                for k, v in item.get("set", {}).items():
                    b[int(k.lstrip("+"), 16)] = int(v) & 0xFF
                for k, v in item.get("set16", {}).items():
                    o = int(k.lstrip("+"), 16)
                    b[o:o + 2] = struct.pack("<H", int(v))
                if item.get("clear_scores"):
                    # High-score clears from +15: (game, category) pairs,
                    # category 0xFF for all; 0xFF ends the list.
                    b = b[:0x15]
                    for game, cat in item["clear_scores"]:
                        b += bytes([int(game), int(cat) & 0xFF])
                    b += b"\xff\xff"
                if "volume" in item:
                    # +0A is a mixer level, 0-127; the operator menu shows it
                    # as a percentage of 127, rounded down.
                    b[0x0A] = min(127, max(1, -(-int(item["volume"]) * 127 // 100)))
                return out + self.say(0x0201, bytes(b[4:]), "settings %s" % item.get("set"))
            if what == "dialup":
                # 0x0221 with a body: the last 0x0222, with fields changed.
                base = self.last_report("0222")
                if base is None:
                    self.box_item = None
                    self.finish_item("no 0x0222 read yet")
                    continue
                b = bytearray(b"\0\0\0\0" + base)
                fields = {"init": (0x004, 100), "prefix": (0x068, 11), "phone": (0x073, 41),
                          "login": (0x09C, 41), "password": (0x0C5, 41), "server": (0x0EE, 41),
                          "dns1": (0x117, 16), "dns2": (0x127, 16)}
                for k, v in item.get("set", {}).items():
                    if k == "update_hour":
                        b[0x137] = int(v)
                    elif k in fields:
                        off, n = fields[k]
                        b[off:off + n] = cstr(str(v), n)
                return out + self.say(0x0221, bytes(b[4:]), "dial-up %s" % item.get("set"))
            if what == "isp":
                # 0x0A01 (no answer): ISP login at +04, password at +68.
                b = bytearray(0x100)
                b[0x00:0x29] = cstr(item.get("login", ""), 41)
                b[0x64:0x8D] = cstr(item.get("password", ""), 41)
                out += self.say(0x0A01, bytes(b), "ISP login %r" % item.get("login"))
                self.box_item = None
                self.finish_item("sent")
                continue
            if what == "location_entry":
                # 0x00D1: 154-byte records for location.dbf: u32 ID, NAME,
                # CITY, STATE, COUNTRY.  The cabinet answers 0x00D2.
                rec = (struct.pack("<I", int(item.get("id", 0))) + cstr(item.get("name", ""), 52) +
                       cstr(item.get("city", ""), 31) + cstr(item.get("state", ""), 36) +
                       cstr(item.get("country", ""), 31))
                return out + self.say(0x00D1, rec, "location %s" % item.get("id"))
            if what == "prices":
                # 0x0211 with a body: (game, credits) pairs; the cabinet
                # answers 0x0212 with every game's price.
                body = b"".join(bytes([int(g), int(c) & 0x0F]) for g, c in item.get("set", {}).items())
                return out + self.say(0x0211, body, "prices %s" % item.get("set"))
            if what == "counters":
                # 0x00C9: the event counters; the cabinet clears them once sent.
                return out + self.say(0x00C9, what="event counters")
            self.box_item = None
            self.finish_item("unknown")
        return out + self.next_report()

    def last_report(self, typ):
        for r in reversed(STATE.get("reports", {}).get(self.serial, [])):
            if r["type"] == typ:
                return bytes.fromhex(r["raw"])
        return None

    def finish_item(self, result):
        box = STATE["outbox"][self.serial]
        item = box.pop(0)
        item["result"] = result
        item["at"] = int(time.time())
        STATE.setdefault("outbox_done", {}).setdefault(self.serial, []).append(item)
        save_state()
        log(self.name, "  outbox: %s: %s" % (item.get("do"), result))

    def outbox_answer(self, t, body):
        item = self.box_item
        what = item.get("do")
        if what in ("delete", "settings", "dialup", "location_entry", "counters", "prices"):
            # The answer is what the cabinet now holds: keep it.
            STATE.setdefault("reports", {}).setdefault(self.serial, []).append(
                {"type": "%04X" % t, "at": int(time.time()), "raw": body.hex()})
            self.box_item = None
            self.finish_item("done, answered %04X" % t)
        elif what == "send_file":
            if t == 0x0114:
                self.box_item = None
                self.finish_item("refused by the cabinet")
            elif self.box_done:
                self.box_item = None
                self.finish_item("sent")
            else:
                # 0x0112: +04 offset, +08 file size, +0C bytes here, +10 data;
                # a chunk of 0 bytes ends the file.
                chunk = self.box_data[self.box_pos:self.box_pos + self.CHUNK]
                body = struct.pack("<III", self.box_pos, len(self.box_data), len(chunk)) + chunk
                self.box_pos += len(chunk)
                if not chunk:
                    self.box_done = True
                return self.say(0x0112, body, "%d/%d" % (self.box_pos, len(self.box_data)))
        elif what == "fetch_file":
            if t == 0x0104:
                self.box_item = None
                self.finish_item("the cabinet cannot open it")
                self.update_result(item, "no result file")
            else:
                # 0x0102: +04 offset, +08 file size, +0C bytes here, +10 data.
                off, size, count = struct.unpack("<III", body[0:12])
                self.box_data += body[12:12 + count]
                if count:
                    return self.say(0x0103, what="next chunk (%d/%d)" % (len(self.box_data), size))
                name = item["path"].replace(":", "").replace("\\", os.sep).lstrip(os.sep)
                dest = os.path.join(os.path.dirname(os.path.abspath(STATE_PATH)), "files", self.serial, name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(self.box_data)
                self.box_item = None
                self.finish_item("saved as %s (%d bytes)" % (dest, len(self.box_data)))
                self.update_result(item, self.box_data.decode("latin1").strip() or "empty result")
        return self.run_outbox()

    def update_result(self, item, text):
        """A fetched update result file: its text is the update's outcome."""
        if item.get("update"):
            STATE.setdefault("update_status", {}).setdefault(item["update"], {})[self.serial] = text
            save_state()
            log(self.name, "  update %s: %s" % (item["update"], text))

    def next_report(self):
        """Read-only questions asked at the end of an update call; then
        COMPLETE.  The answers are kept in the state file under "reports"."""
        if self.reports:
            self.asked, what = self.reports.pop(0)
            return self.say(self.asked, what=what)
        self.reports = None
        self.asked = 0
        return self.say(0xFF01, b"COMPLETE.\0", "COMPLETE.")


class Session:
    def __init__(self, sock, name):
        self.sock = sock
        self.name = name
        self.ident = 1
        self.lcp_open = False
        self.ipcp_sent = False
        self.ppp_seen = False

    def send(self, proto, payload):
        self.sock.sendall(frame(proto, payload))

    def next_id(self):
        self.ident = (self.ident + 1) & 0xFF
        return self.ident

    # ---------------------------------------------------------------- LCP
    def lcp(self, data):
        code, ident, length = struct.unpack(">BBH", data[:4])
        opts = data[4:length]
        names = {1: "Configure-Request", 2: "Configure-Ack", 3: "Configure-Nak", 4: "Configure-Reject",
                 5: "Terminate-Request", 6: "Terminate-Ack", 7: "Code-Reject", 8: "Protocol-Reject",
                 9: "Echo-Request", 10: "Echo-Reply", 11: "Discard-Request"}
        log(self.name, "LCP %s id %d %s" % (names.get(code, code), ident, hexs(opts)))
        if code == 1:
            # Take whatever the cabinet asks for, and ask for nothing ourselves:
            # no authentication, default MRU and ACCM on our side.
            self.send(PROTO_LCP, struct.pack(">BBH", 2, ident, length) + opts)
            if not self.lcp_open:
                self.send(PROTO_LCP, struct.pack(">BBH", 1, self.next_id(), 4))
        elif code == 2:
            self.lcp_open = True
            log(self.name, "LCP open")
        elif code == 5:
            self.send(PROTO_LCP, struct.pack(">BBH", 6, ident, 4))
            log(self.name, "the cabinet hung up the PPP link")
        elif code == 9:
            self.send(PROTO_LCP, struct.pack(">BBH", 10, ident, length) + b"\0\0\0\0" + opts[4:])

    # ---------------------------------------------------------------- IPCP
    def ipcp(self, data):
        code, ident, length = struct.unpack(">BBH", data[:4])
        opts = data[4:length]
        log(self.name, "IPCP code %d id %d %s" % (code, ident, hexs(opts)))
        if code == 1:
            nak, rej, i = b"", b"", 0
            while i + 2 <= len(opts):
                t, l = opts[i], opts[i + 1]
                o = opts[i:i + l]
                if t == 3:                      # IP-Address
                    if o[2:6] != PEER_IP:
                        nak += bytes([3, 6]) + PEER_IP
                elif t in (129, 131):           # primary / secondary DNS
                    if o[2:6] != DNS_IP:
                        nak += bytes([t, 6]) + DNS_IP
                else:                           # VJ compression and the rest
                    rej += o
                i += max(l, 2)
            if rej:
                self.send(PROTO_IPCP, struct.pack(">BBH", 4, ident, 4 + len(rej)) + rej)
            elif nak:
                self.send(PROTO_IPCP, struct.pack(">BBH", 3, ident, 4 + len(nak)) + nak)
            else:
                self.send(PROTO_IPCP, struct.pack(">BBH", 2, ident, length) + opts)
                log(self.name, "IPCP: the cabinet is %s, the server %s" % (dotted(PEER_IP), dotted(OUR_IP)))
            if not self.ipcp_sent:
                self.ipcp_sent = True
                o = bytes([3, 6]) + OUR_IP
                self.send(PROTO_IPCP, struct.pack(">BBH", 1, self.next_id(), 4 + len(o)) + o)
        elif code == 5:
            self.send(PROTO_IPCP, struct.pack(">BBH", 6, ident, 4))

    # ---------------------------------------------------------------- IP
    def ip(self, pkt):
        if len(pkt) < 20 or (pkt[0] >> 4) != 4:
            log(self.name, "IP (not v4) %s" % hexs(pkt))
            return
        ihl = (pkt[0] & 15) * 4
        proto = pkt[9]
        src, dst = pkt[12:16], pkt[16:20]
        body = pkt[ihl:]
        if proto == 17 and len(body) >= 8:
            sp, dp, ul = struct.unpack("!HHH", body[:6])
            data = body[8:ul]
            log(self.name, "UDP %s:%d -> %s:%d %d bytes %s" % (dotted(src), sp, dotted(dst), dp, len(data), hexs(data, 32)))
            if dp == 53:
                self.dns(src, sp, dst, data)
        elif proto == 6 and len(body) >= 20:
            sp, dp, seq, ack, off, flags = struct.unpack("!HHIIBB", body[:14])
            names = "".join(n for bit, n in ((2, "S"), (16, "A"), (8, "P"), (1, "F"), (4, "R")) if flags & bit)
            data = body[(off >> 4) * 4:]
            log(self.name, "TCP %s:%d -> %s:%d [%s] %d bytes %s" % (dotted(src), sp, dotted(dst), dp, names, len(data), hexs(data, 32)))
            self.tcp(src, sp, dst, dp, seq, ack, flags, data)
        elif proto == 1 and len(body) >= 8 and body[0] == 8:
            log(self.name, "ICMP echo %s -> %s" % (dotted(src), dotted(dst)))
            reply = bytearray(body)
            reply[0] = 0
            reply[2:4] = b"\0\0"
            reply[2:4] = struct.pack("!H", ip_checksum(bytes(reply)))
            self.send(PROTO_IP, ip_packet(1, dst, src, bytes(reply)))
        else:
            log(self.name, "IP proto %d %s -> %s %s" % (proto, dotted(src), dotted(dst), hexs(body, 32)))

    # ---------------------------------------------------------------- TCP
    # Just enough TCP to let the cabinet open a connection and send: every
    # SYN is answered, every segment acknowledged and logged, a FIN answered.
    # Nothing is sent back -- what Merit's server said first is not known.
    def tcp_send(self, src, sp, dst, dp, seq, ack, flags, payload=b""):
        tcp = struct.pack("!HHIIBBHHH", sp, dp, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags, 8192, 0, 0) + payload
        pseudo = src + dst + struct.pack("!BBH", 0, 6, len(tcp))
        tcp = tcp[:16] + struct.pack("!H", ip_checksum(pseudo + tcp)) + tcp[18:]
        self.send(PROTO_IP, ip_packet(6, src, dst, tcp))

    def tcp(self, src, sp, dst, dp, seq, ack, flags, data):
        key = (src, sp, dst, dp)
        if not hasattr(self, "conns"):
            self.conns = {}
        if flags & 4:                                   # RST
            self.conns.pop(key, None)
            return
        if flags & 2 and not flags & 16:                # SYN: accept
            c = self.conns[key] = {"snd": 1000, "rcv": (seq + 1) & 0xFFFFFFFF, "bytes": 0,
                                   "open": False, "app": TournaMaxx(self.name, dp) if dp in (15000, 17751) else None}
            self.tcp_send(dst, dp, src, sp, c["snd"], c["rcv"], 0x12)
            c["snd"] += 1
            log(self.name, "  accepted %s:%d" % (dotted(dst), dp))
            return
        c = self.conns.get(key)
        if c is None:
            self.tcp_send(dst, dp, src, sp, ack, seq + len(data), 0x14)
            return
        reply = b""
        if not c["open"] and flags & 16:                # handshake done
            c["open"] = True
            if c["app"]:
                reply += c["app"].opened()
        if data and seq == c["rcv"]:
            c["rcv"] = (c["rcv"] + len(data)) & 0xFFFFFFFF
            c["bytes"] += len(data)
            if c["app"]:
                reply += c["app"].received(data)
            else:
                log(self.name, "  data from the cabinet (%d bytes): %s" % (len(data), data.hex(" ")))
                txt = "".join(chr(x) if 32 <= x < 127 else "." for x in data)
                log(self.name, "  as text: %s" % txt)
        if flags & 1:                                   # FIN
            c["rcv"] = (c["rcv"] + 1) & 0xFFFFFFFF
            self.tcp_send(dst, dp, src, sp, c["snd"], c["rcv"], 0x11)
            c["snd"] += 1
            log(self.name, "  the cabinet closed %s:%d after %d bytes" % (dotted(dst), dp, c["bytes"]))
            return
        while reply:                                    # 512-byte segments
            seg, reply = reply[:512], reply[512:]
            self.tcp_send(dst, dp, src, sp, c["snd"], c["rcv"], 0x18, seg)
            c["snd"] += len(seg)
            data = b""
        if data:
            self.tcp_send(dst, dp, src, sp, c["snd"], c["rcv"], 0x10)

    def dns(self, src, sp, dst, q):
        if len(q) < 12:
            return
        # The first question's name, then an A record for it: this server.
        i, labels = 12, []
        while i < len(q) and q[i]:
            labels.append(q[i + 1:i + 1 + q[i]].decode("latin1"))
            i += q[i] + 1
        qend = i + 5
        log(self.name, "  DNS query for %s -> %s" % (".".join(labels), dotted(OUR_IP)))
        ans = q[:2] + b"\x81\x80" + b"\0\x01\0\x01\0\0\0\0" + q[12:qend]
        ans += b"\xc0\x0c\0\x01\0\x01\0\0\0\x3c\0\x04" + OUR_IP
        udp = struct.pack("!HHHH", 53, sp, 8 + len(ans), 0) + ans
        self.send(PROTO_IP, ip_packet(17, dst, src, udp))

    # ---------------------------------------------------------------- frames
    def handle_frame(self, f):
        if len(f) < 4:
            return
        if fcs16(f[:-2]) != struct.unpack("<H", f[-2:])[0]:
            log(self.name, "bad FCS %s" % hexs(f, 32))
            return
        f = f[:-2]
        if f[:2] == b"\xff\x03":
            f = f[2:]
        if f[0] & 1:                    # compressed protocol field
            proto, data = f[0], f[1:]
        else:
            proto, data = struct.unpack(">H", f[:2])[0], f[2:]
        if proto == PROTO_LCP:
            self.lcp(data)
        elif proto == PROTO_IPCP:
            self.ipcp(data)
        elif proto == PROTO_IP:
            self.ip(data)
        else:
            log(self.name, "protocol %04X rejected %s" % (proto, hexs(data, 32)))
            rej = struct.pack(">H", proto) + data
            self.send(PROTO_LCP, struct.pack(">BBH", 8, self.next_id(), 4 + len(rej)) + rej)

    def run(self):
        buf, esc, text = bytearray(), False, bytearray()
        while True:
            try:
                chunk = self.sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            for c in chunk:
                if c == 0x7E:
                    if not self.ppp_seen and text:
                        log(self.name, "text: %r" % bytes(text))
                        text.clear()
                    self.ppp_seen = True
                    if buf:
                        with LOCK:
                            self.handle_frame(bytes(buf))
                    buf.clear()
                    esc = False
                elif not self.ppp_seen:
                    text.append(c)
                    if c in (10, 13):
                        if text.strip():
                            log(self.name, "text: %r" % bytes(text))
                        text.clear()
                elif c == 0x7D:
                    esc = True
                else:
                    buf.append(c ^ 0x20 if esc else c)
                    esc = False
        log(self.name, "call ended")
        self.sock.close()


def main():
    global LOG
    ap = argparse.ArgumentParser(description="TournaMAXX-Revival: a TournaMAXX server for Megatouch MAXX cabinets on an emulated modem.")
    ap.add_argument("--port", type=int, default=2323)
    ap.add_argument("--log", default="modem-server.log")
    ap.add_argument("--state", default="modem-server-state.json")
    a = ap.parse_args()
    LOG = open(a.log, "a", encoding="utf-8")
    load_state(a.state)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", a.port))
    srv.listen(4)
    log("server", "waiting for calls on port %d (log: %s)" % (a.port, a.log))
    n = 0
    try:
        while True:
            s, addr = srv.accept()
            n += 1
            name = "call %d" % n
            log(name, "connected from %s:%d" % addr)
            threading.Thread(target=Session(s, name).run, daemon=True).start()
    except KeyboardInterrupt:
        log("server", "stopped")


if __name__ == "__main__":
    main()
