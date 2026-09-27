#!/usr/bin/env python3
"""
MegaPPBox modem server: something for the emulated modem to dial.

Set the modem's line to "Dial out to a TCP/IP host" (Tools > Modem settings...),
host 127.0.0.1 and the port below, and any number the cabinet dials connects
here.  The Megatouch releases that go on line (TournaMAXX / MegaNET) dial an
ISP and run PPP with their own TCP/IP stack, so this server:

  * answers PPP: LCP (no authentication asked for), IPCP (the cabinet gets
    10.0.2.15, this server is 10.0.2.2, DNS 10.0.2.3); anything else is
    protocol-rejected;
  * answers DNS queries for any name with 10.0.2.2;
  * answers ping;
  * logs everything: text before PPP starts (a login chat, if the cabinet
    expects one), every PPP frame, and every IP packet with its addresses and
    ports -- which is how to learn what the cabinet wants to talk to.

TCP connections the cabinet opens (the MAXX releases: us.accessmerit.com,
port 15000) are accepted, and whatever the cabinet sends is acknowledged and
logged as hex and text.  Nothing is sent back: what Merit's server said is not
known, so the cabinet will time out waiting for it.

    python tools/modem-server.py [--port 2323] [--log modem-server.log]

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
import json

OUR_IP  = bytes([10, 0, 2, 2])
PEER_IP = bytes([10, 0, 2, 15])
DNS_IP  = bytes([10, 0, 2, 3])

PROTO_IP   = 0x0021
PROTO_LCP  = 0xC021
PROTO_IPCP = 0x8021

LOG = None
LOCK = threading.Lock()


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
# IDs to, and every score the cabinets have uploaded.  Delete the file to
# start again.

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
                 credits=1, gameopts=0, name="MEGAPPBOX TEST",
                 desc="A test tournament from tools/modem-server.py",
                 randseed=12345, seedinc=1,
                 groups=["NATIONAL", "REGIONAL", "LOCAL"],
                 prizes=["FIRST PRIZE", "SECOND PRIZE", "THIRD PRIZE"],
                 showdate=now - 3600),
        ],
        "players": {},      # permanent id -> what the cabinet sent
        "scores": [],       # every uploaded score entry
    }


def load_state(path):
    global STATE, STATE_PATH
    STATE_PATH = path
    try:
        with open(path, encoding="utf-8") as f:
            STATE = json.load(f)
    except (OSError, ValueError):
        STATE = default_state()
        save_state()


def save_state():
    with LOCK:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=1)


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


class TournaMaxx:
    """The TournaMAXX server end, as far as it is known (DOS MAXX, Emerald 2).

    Messages both ways are [type u16][length u16][body], little-endian, the
    length counting the 4-byte header.  The server speaks first and drives;
    the cabinet answers each command with the next type up.  See
    docs/tournamaxx.md.  Port 15000 is the Initial Connection (no databases
    open: login and COMPLETE only); port 17751 (0x4557) is the update call.
    """

    def __init__(self, name, port=15000):
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
        return self.say(0x0021, tournament_body(t, status),
                        "tournament %d, %r, status %d" % (t["id"], t["name"], status))

    def delivered(self, kind, tid):
        """Whether this cabinet has had tournament tid's final (kind "final")
        or removal ("removed") already; the state file remembers."""
        return self.serial in STATE.setdefault(kind, {}).get(str(tid), [])

    def mark(self, kind, tid):
        STATE.setdefault(kind, {}).setdefault(str(tid), []).append(self.serial)
        save_state()

    def closing(self):
        """After the rankings: STATUS 4 for tournaments that have ended (the
        cabinet archives their rows into SC<id>.dbf, which the winners
        screen reads, so the final rankings have to be in first), STATUS 5
        for those whose winners have been shown long enough."""
        todo = []
        for t in STATE["tournaments"]:
            st = tournament_status(t)
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
        out = b""
        for i in range(0, len(entries), 60):
            chunk = entries[i:i + 60]
            out += self.say(0x0073, b"".join(chunk), "rankings (%d entries)" % len(chunk))
        return out, (len(entries) + 59) // 60

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
            elif self.box_item and t in (0x0102, 0x0104, 0x0113, 0x0114, 0x0122):
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
                self.reports = list(REPORTS)
                others = self.other_players()
                out += others if others else self.send_rankings()
            elif t == 0x0082:
                out += self.send_rankings()
            elif t == 0x0074:
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
            "name": "MEGAPPBOX CABINET", "city_state": "SET IN modem-server-state.json",
            "country": "", "telephone": ""})
        save_state()
        body = b"\xff" * 13 + b"".join(
            cstr(loc.get(k, ""), 51) for k in ("name", "city_state", "country", "telephone"))
        return self.say(0x00B1, body, "location %r" % loc.get("name"))

    def send_rankings(self):
        ranks, self.rank_msgs = self.rankings()
        return ranks if ranks else self.after_rankings()

    def other_players(self):
        """0x0081: players registered at other cabinets that this one has not
        been sent yet, so their handles, cities and states show in its
        rankings.  Entries of 97 bytes (MEGACDLL 0x9e6bc): u32 ID, then the
        player's record as their own cabinet sent it (from +4: u32, HANDLE,
        PIN, CITY, STATE), u32 LOCATION.  The cabinet answers 0x0082."""
        sent = STATE.setdefault("players_sent", {}).setdefault(self.serial, [])
        todo = [(pid, p) for pid, p in STATE["players"].items()
                if p.get("cabinet") != self.serial and pid not in sent]
        if not todo:
            return b""
        todo = todo[:20]                      # 20 × 97 bytes fit a message
        body = b""
        for pid, p in todo:
            raw = bytes.fromhex(p["raw"])
            body += struct.pack("<I", int(pid)) + raw[4:0x5D] + struct.pack("<I", 0)
            sent.append(pid)
        save_state()
        return self.say(0x0081, body, "%d players from other cabinets" % len(todo))

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
    # Done items move to "outbox_done".

    CHUNK = 1024

    def run_outbox(self):
        box = STATE.setdefault("outbox", {}).setdefault(self.serial, [])
        out = b""
        while box:
            item = box[0]
            what = item.get("do")
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
            self.box_item = None
            self.finish_item("unknown")
        return out + self.next_report()

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
        if what == "delete":
            self.box_item = None
            self.finish_item("done")
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
        return self.run_outbox()

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
    ap = argparse.ArgumentParser(description="A PPP answerer for MegaPPBox's modem.")
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
