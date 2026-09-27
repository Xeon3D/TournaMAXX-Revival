#!/usr/bin/env python3
r"""Read a DOS Megatouch MAXX database (D:\Database\*.dbf).

    python tools/dbfcrypt.py <file.dbf> [FIELD ...]

prints each record's fields (all, or those named).  The files are dBase III
with the records encrypted from byte 1 (after the delete flag) as one
stream: Merit's PC1 variant (MEGACDLL V8.04 0x93fb0/0x94188/0x942a8), 10-byte
key "M@xxR0cks!", state reset per record, and the round index never
advances.  Read-only.
"""
import struct
import sys


def dec(data, key=b'M@xxR0cks!', encrypt=False):
    k = bytearray(key); x = [0]*5; x1a2 = 0; si = 0; out = bytearray()
    def code():
        nonlocal x1a2, si
        ax = (x1a2) & 0xffff; dx = si; si = x[0]
        if ax: ax = (ax * 0x4e35) & 0xffff
        ax, cx = 0x15a, ax
        ax = (ax * si) & 0xffff; cx = (cx + ax) & 0xffff
        ax, si = si, ax
        ax = (ax * 0x4e35) & 0xffff
        dx = (dx + cx) & 0xffff
        ax = (ax + 1) & 0xffff
        x1a2 = dx; x[0] = ax
        return ax ^ dx
    for c in data:
        x[0] = (k[0] << 8) | k[1]
        inter = code()
        for i in range(1, 5):
            x[i] = x[i-1] ^ ((k[2*i] << 8) | k[2*i+1])
            inter ^= code()
        cfc = (inter >> 8) ^ (inter & 0xff)
        if encrypt:
            for j in range(10): k[j] ^= c
            c ^= cfc
        else:
            c ^= cfc
            for j in range(10): k[j] ^= c
        out.append(c)
    return bytes(out)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d = open(sys.argv[1], 'rb').read()
    hs, rs = struct.unpack('<HH', d[8:12])
    fields = []
    for i in range(32, hs, 32):
        f = d[i:i + 32]
        fields.append((f[:11].split(b'\0')[0].decode(), struct.unpack('<I', f[12:16])[0], f[16]))
    want = sys.argv[2:]
    for r in range(hs, len(d) - rs + 1, rs):
        p = b'?' + dec(d[r + 1:r + rs])
        print({n: p[o:o + w].rstrip(b'\0').decode('latin-1') for n, o, w in fields
               if not want or n in want})


if __name__ == '__main__':
    main()
