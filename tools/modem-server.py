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
import threading

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
    def tcp_send(self, src, sp, dst, dp, seq, ack, flags):
        tcp = struct.pack("!HHIIBBHHH", sp, dp, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags, 8192, 0, 0)
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
            c = self.conns[key] = {"snd": 1000, "rcv": (seq + 1) & 0xFFFFFFFF, "bytes": 0}
            self.tcp_send(dst, dp, src, sp, c["snd"], c["rcv"], 0x12)
            c["snd"] += 1
            log(self.name, "  accepted %s:%d" % (dotted(dst), dp))
            return
        c = self.conns.get(key)
        if c is None:
            self.tcp_send(dst, dp, src, sp, ack, seq + len(data), 0x14)
            return
        if data and seq == c["rcv"]:
            c["rcv"] = (c["rcv"] + len(data)) & 0xFFFFFFFF
            c["bytes"] += len(data)
            log(self.name, "  data from the cabinet (%d bytes): %s" % (len(data), data.hex(" ")))
            txt = "".join(chr(x) if 32 <= x < 127 else "." for x in data)
            log(self.name, "  as text: %s" % txt)
        if flags & 1:                                   # FIN
            c["rcv"] = (c["rcv"] + 1) & 0xFFFFFFFF
            self.tcp_send(dst, dp, src, sp, c["snd"], c["rcv"], 0x11)
            c["snd"] += 1
            log(self.name, "  the cabinet closed %s:%d after %d bytes" % (dotted(dst), dp, c["bytes"]))
            return
        if data or flags & 1:
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
    a = ap.parse_args()
    LOG = open(a.log, "a", encoding="utf-8")

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
