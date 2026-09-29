#!/usr/bin/env python3
"""
Mega-Link over the internet: a switch for MegaPPBox's "Remote Switch".

Mega-Link is the cabinets' own network: Megatouch MAXX cabinets on one
Ethernet segment, playing head-to-head and linked games.  MegaPPBox's Remote
Switch network type (src/network/net_switch.c) sends each Ethernet frame the
emulated card transmits as one UDP datagram to host:port (8086 by default),
and takes frames back only from that address:

    datagram = [SHA3-256(secret), 32 bytes, when the card has a secret] + frame

An empty datagram every 20 seconds keeps the NAT mapping open.  This switch
is the other end: cabinets whose datagrams carry the same secret's hash are
in one room -- one Ethernet segment -- and each frame goes to the others in
it (to the one cabinet that owns the destination address, once it is known;
broadcasts and unknown addresses to all).  The frames are passed on as they
came, hash and all.

The rooms (name, secret, how many cabinets) come from the caller; a room
without a secret, for cabinets whose card has none, is optional.  Datagrams
for no known room are dropped.

    python megalink_switch.py --port 8086 --room "Main=megalink" --room "Bar=s3cret"

Standard library only.  modem-server.py runs one with --switch-port, its
rooms from the state file's "megalink" (docs/tournamaxx.md).
"""

import argparse
import hashlib
import socket
import threading
import time

HASH_LEN = 32
FRAME_MIN = 14                # destination, source, type
FRAME_MAX = 1518
IDLE = 60                     # s without a datagram: the cabinet has left (keep-alives are every 20 s)
MAX_PPS = 4000                # frames a second from one cabinet before the rest are dropped
MAX_CABINETS = 512            # in all rooms together


def room_hash(secret):
    return hashlib.sha3_256(secret.encode("utf-8")).digest()


def mac_text(b):
    return ":".join("%02X" % x for x in b)


class Cabinet:
    def __init__(self, addr, key, t):
        self.addr = addr
        self.key = key
        self.since = t
        self.last = t
        self.frames_in = 0
        self.frames_out = 0
        self.bytes_in = 0
        self.macs = set()
        self.ips = set()
        self.second = int(t)
        self.in_second = 0
        self.dropped = 0


class Switch:
    """rooms() returns the room list, each {"name", "secret", "max"} ("secret"
    empty or missing: the open room, for cards without one); called at most
    once a second, so the rooms may change while the switch runs."""

    def __init__(self, port, rooms, log=print, host="0.0.0.0"):
        self.rooms_fn = rooms
        self.log = log
        self.lock = threading.Lock()
        self.cabinets = {}            # addr -> Cabinet
        self.macs = {}                # room key -> {mac: addr}
        self.rooms = {}               # room key (hash, or b"" for the open room) -> config
        self.rooms_src = None
        self.rooms_at = 0
        self.full = {}                # addr -> time last refused because its room was full
        self.unknown = {}             # addr -> time of its last datagram for no room
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(5)
        self.port = port

    # ------------------------------------------------------------ rooms
    def read_rooms(self, t):
        """The caller's room list, at most once a second: read outside our lock,
        since the caller may take its own lock (and ask us for status under it)."""
        if t - self.rooms_at < 1:
            return None
        self.rooms_at = t
        try:
            return self.rooms_fn() or []
        except Exception as e:           # a half-edited state file: keep what we had
            self.log("megalink", "rooms not read: %s" % e)
            return None

    def set_rooms(self, src):
        if src is None or src == self.rooms_src:
            return
        self.rooms_src = src
        rooms = {}
        for r in src:
            secret = r.get("secret") or ""
            rooms[room_hash(secret) if secret else b""] = r
        self.rooms = rooms
        for addr, c in list(self.cabinets.items()):
            if c.key not in rooms:
                self.drop(addr)
        self.log("megalink", "rooms: %s" % (", ".join(r.get("name", "?") for r in src) or "none"))

    def room_of(self, d):
        if len(d) >= HASH_LEN + FRAME_MIN and d[:HASH_LEN] in self.rooms:
            return d[:HASH_LEN], d[HASH_LEN:]
        if b"" in self.rooms and len(d) >= FRAME_MIN:
            return b"", d
        return None, None

    def in_room(self, key):
        return [c for c in self.cabinets.values() if c.key == key]

    def drop(self, addr):
        c = self.cabinets.pop(addr, None)
        if c:
            table = self.macs.get(c.key, {})
            for mac in c.macs:
                if table.get(mac) == addr:
                    del table[mac]
            self.log("megalink", "%s:%d left %s" % (addr + (self.rooms.get(c.key, {}).get("name", "?"),)))

    # ------------------------------------------------------------ frames
    def datagram(self, d, addr, t):
        c = self.cabinets.get(addr)
        if not d:                                   # keep-alive
            if c:
                c.last = t
            return
        key, frame = self.room_of(d)
        if key is None or len(frame) > FRAME_MAX:
            self.unknown[addr] = t
            return
        if c and c.key != key:                      # the card's secret changed
            self.drop(addr)
            c = None
        if c is None:
            room = self.rooms[key]
            if len(self.in_room(key)) >= int(room.get("max") or 8) or len(self.cabinets) >= MAX_CABINETS:
                if t - self.full.get(addr, 0) > 60:
                    self.log("megalink", "%s:%d refused: %s is full" % (addr + (room.get("name", "?"),)))
                self.full[addr] = t
                return
            c = self.cabinets[addr] = Cabinet(addr, key, t)
            self.log("megalink", "%s:%d joined %s" % (addr + (room.get("name", "?"),)))
        c.last = t
        if int(t) != c.second:
            c.second, c.in_second = int(t), 0
        c.in_second += 1
        if c.in_second > MAX_PPS:
            c.dropped += 1
            return
        c.frames_in += 1
        c.bytes_in += len(frame)

        dst, src = frame[0:6], frame[6:12]
        table = self.macs.setdefault(key, {})
        if not src[0] & 1:
            c.macs.add(src)
            table[src] = addr
        etype = frame[12:14]
        ip = None
        if etype == b"\x08\x00" and len(frame) >= 34:
            ip = frame[26:30]
        elif etype == b"\x08\x06" and len(frame) >= 42:
            ip = frame[28:32]
        if ip and ip != b"\0\0\0\0":
            c.ips.add(socket.inet_ntoa(ip))

        if not dst[0] & 1 and table.get(dst) in self.cabinets and table[dst] != addr:
            targets = [self.cabinets[table[dst]]]
        else:
            targets = [o for o in self.in_room(key) if o.addr != addr]
        for o in targets:
            try:
                self.sock.sendto(d, o.addr)
                o.frames_out += 1
            except OSError:
                pass

    def expire(self, t):
        for addr, c in list(self.cabinets.items()):
            if t - c.last > IDLE:
                self.drop(addr)
        for d in (self.full, self.unknown):
            for addr in [a for a, at in d.items() if t - at > 300]:
                del d[addr]

    def run(self):
        self.log("megalink", "switch on UDP port %d" % self.port)
        last_expire = 0
        while True:
            try:
                d, addr = self.sock.recvfrom(HASH_LEN + FRAME_MAX + 64)
            except socket.timeout:
                d, addr = None, None
            except OSError:              # Windows: ICMP port unreachable from a peer that left
                continue
            t = time.time()
            src = self.read_rooms(t)
            with self.lock:
                self.set_rooms(src)
                if d is not None:
                    self.datagram(d, addr, t)
                if t - last_expire >= 5:
                    self.expire(t)
                    last_expire = t

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self

    # ------------------------------------------------------------ status
    def status(self):
        """The rooms and who is in them, for the control panel."""
        t = time.time()
        with self.lock:
            rooms = []
            for key, r in self.rooms.items():
                cabs = self.in_room(key)
                owners = {}
                for c in cabs:
                    for ip in c.ips:
                        owners.setdefault(ip, []).append(c)
                rooms.append({
                    "name": r.get("name", ""), "open": key == b"", "max": int(r.get("max") or 8),
                    "cabinets": [{
                        "address": "%s:%d" % c.addr, "since": int(c.since), "idle": int(t - c.last),
                        "macs": sorted(mac_text(m) for m in c.macs), "ips": sorted(c.ips),
                        "frames_in": c.frames_in, "frames_out": c.frames_out, "bytes_in": c.bytes_in,
                        "dropped": c.dropped} for c in sorted(cabs, key=lambda c: c.since)],
                    "ip_conflicts": sorted(ip for ip, cs in owners.items() if len(cs) > 1),
                })
            return {"port": self.port, "rooms": rooms, "refused_full": len(self.full),
                    "unknown_senders": len(self.unknown), "now": int(t)}


def main():
    ap = argparse.ArgumentParser(description="A Mega-Link switch for MegaPPBox's Remote Switch.")
    ap.add_argument("--port", type=int, default=8086)
    ap.add_argument("--room", action="append", default=[], metavar="NAME=SECRET",
                    help="a room (repeat for more); NAME= with no secret: the open room")
    ap.add_argument("--max", type=int, default=8, help="cabinets a room holds (default 8)")
    a = ap.parse_args()
    rooms = []
    for r in a.room or ["Open="]:
        name, _, secret = r.partition("=")
        rooms.append({"name": name, "secret": secret, "max": a.max})

    def log(who, msg):
        print("%s [%s] %s" % (time.strftime("%H:%M:%S"), who, msg), flush=True)
    Switch(a.port, lambda: rooms, log).run()


if __name__ == "__main__":
    main()
