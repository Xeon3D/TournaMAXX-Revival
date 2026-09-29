#!/usr/bin/env python3
"""
unpack_dstart.py: the game program of a Linux Megatouch MAXX (Ruby onward)
out of its packed /usr/local/bin/start.

    python unpack_dstart.py start dstart [OFFSET]

After a 0x01 byte (at 6268, 6500 or 6688, depending on the release) come
chunks: u32 length, u32, then that many bytes XORed with a stream that
depends only on the byte's position c: 0 when c % 20 == 0, else
(c & 31) + (c & 15) + c % 20 + 35.  The same scheme as Nicholas Navaroli's
unpack_dstart.c for the Force and ION releases; without OFFSET it is found
(where the unpacked data starts with an ELF header).  No key is involved.
"""
import struct
import sys


def key(c):
    return 0 if c % 20 == 0 else ((c & 31) + (c & 15) + c % 20 + 35) & 0xFF


def unpack(d, off):
    o, c, out = off + 1, 0, bytearray()
    while o + 8 <= len(d):
        n = struct.unpack_from("<I", d, o)[0]
        if n == 0 or n > 131072 or o + 8 + n > len(d):
            break
        out += bytes(b ^ key(c + i) for i, b in enumerate(d[o + 8:o + 8 + n]))
        c += n
        o += 8 + n
    return bytes(out)


def find(d):
    for off in range(min(len(d), 200000)):
        if d[off] == 1 and off + 13 <= len(d) and 4 <= struct.unpack_from("<I", d, off + 1)[0] <= 131072:
            if bytes(b ^ key(i) for i, b in enumerate(d[off + 9:off + 13])) == b"\x7fELF":
                return off
    return None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    d = open(sys.argv[1], "rb").read()
    if d[:4] == b"\x7fELF" and find(d) is None:
        sys.exit("%s is not packed (a keyless image's start already is the program)" % sys.argv[1])
    off = int(sys.argv[3]) if len(sys.argv) > 3 else find(d)
    if off is None:
        sys.exit("no packed program found")
    out = unpack(d, off)
    with open(sys.argv[2], "wb") as f:
        f.write(out)
    print("offset %d: %d bytes" % (off, len(out)))


if __name__ == "__main__":
    main()
