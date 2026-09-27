#!/usr/bin/env python3
"""Load an LE (DOS/4GW-style) exe into a flat image with its internal fixups applied.

Usage as a module:  img = load(path)  ->  Image with .objs [(base, size, flags)],
                    .mem (bytearray covering all objects), .base, .read(va, n),
                    .xrefs(va) (fixup sites that point at va), .strings()
Usage as a script:  lefile.py <exe>             -> object table, fixup counts
                    lefile.py <exe> str <text>  -> VA of each occurrence, and the
                                                   code sites whose fixups point at it
Read-only on the input.
"""
import struct
import sys


class Image:
    def __init__(self, d):
        self.d = d
        le = d.find(b'LE\x00\x00')
        h = d[le:le + 0xc4]
        self.le = le
        pgsz = struct.unpack('<I', h[0x28:0x2c])[0]
        objtab, nobj = struct.unpack('<II', h[0x40:0x48])
        pagemap = struct.unpack('<I', h[0x48:0x4c])[0]
        fpt, frt = struct.unpack('<II', h[0x68:0x70])
        datapages = struct.unpack('<I', h[0x80:0x84])[0]
        npages = struct.unpack('<I', h[0x14:0x18])[0]
        lastpg = struct.unpack('<I', h[0x2c:0x30])[0]
        self.eip_obj, self.eip = struct.unpack('<II', h[0x18:0x20])

        self.objs = []
        for k in range(nobj):
            vs, base, fl, pti, npg, _ = struct.unpack('<6I', d[le + objtab + 24 * k:le + objtab + 24 * k + 24])
            self.objs.append((base, vs, fl, pti, npg))
        self.base = min(o[0] for o in self.objs)
        top = max(o[0] + ((o[1] + 0xfff) & ~0xfff) for o in self.objs)
        self.mem = bytearray(top - self.base)

        # page map: LE entries are 4 bytes, page number in the upper 3 (big-endian-ish)
        def page_file_off(p):  # p is 1-based logical page
            e = d[le + pagemap + 4 * (p - 1):le + pagemap + 4 * p]
            num = (e[0] << 16) | (e[1] << 8) | e[2]
            return datapages + (num - 1) * pgsz

        self.page_va = {}
        self.page_off = {}
        for base, vs, fl, pti, npg in self.objs:
            for j in range(npg):
                p = pti + j
                off = page_file_off(p)
                n = lastpg if p == npages else pgsz
                va = base + j * pgsz
                chunk = d[off:off + n]
                self.mem[va - self.base:va - self.base + len(chunk)] = chunk
                self.page_va[p] = va
                self.page_off[p] = off

        self.fix = {}  # site va -> target va
        self.imports = {}  # site va of import fixups
        fptab = [struct.unpack('<I', d[le + fpt + 4 * i:le + fpt + 4 * i + 4])[0] for i in range(npages + 1)]
        for p in range(1, npages + 1):
            if p not in self.page_va:
                continue
            i, end = le + frt + fptab[p - 1], le + frt + fptab[p]
            pva = self.page_va[p]
            while i < end:
                src, fl = d[i], d[i + 1]
                i += 2
                if src & 0x20:
                    cnt = d[i]; i += 1
                    offs = None
                else:
                    offs = [struct.unpack('<h', d[i:i + 2])[0]]; i += 2
                t = src & 0x0f
                kind = fl & 3
                if kind == 0:                                   # internal reference
                    if fl & 0x40:
                        obj = struct.unpack('<H', d[i:i + 2])[0]; i += 2
                    else:
                        obj = d[i]; i += 1
                    toff = 0
                    if t != 2:
                        if fl & 0x10:
                            toff = struct.unpack('<I', d[i:i + 4])[0]; i += 4
                        else:
                            toff = struct.unpack('<H', d[i:i + 2])[0]; i += 2
                else:                                           # import / entry: skipped
                    obj = None
                    i += 2 if fl & 0x40 else 1                  # module or entry ordinal
                    if kind == 1:                               # import by ordinal
                        if fl & 0x80:
                            ordn = d[i]; i += 1
                        elif fl & 0x10:
                            ordn = struct.unpack('<I', d[i:i + 4])[0]; i += 4
                        else:
                            ordn = struct.unpack('<H', d[i:i + 2])[0]; i += 2
                    elif kind == 2:                             # import by name
                        i += 4 if fl & 0x10 else 2
                if fl & 0x04:                                   # additive
                    i += 4 if fl & 0x20 else 2
                if offs is None:
                    offs = [struct.unpack('<h', d[i + 2 * k:i + 2 * k + 2])[0] for k in range(cnt)]
                    i += 2 * cnt
                if obj is None:
                    for so in offs:
                        self.imports[pva + so] = (d[i - 1] if False else 0)
                    continue
                tva = self.objs[obj - 1][0] + toff
                for so in offs:
                    site = pva + so
                    m = site - self.base
                    if t == 7:
                        self.mem[m:m + 4] = struct.pack('<I', tva)
                        self.fix[site] = tva
                    elif t == 8:
                        self.mem[m:m + 4] = struct.pack('<i', tva - (site + 4))
                        self.fix[site] = tva
                    elif t == 6:
                        self.mem[m:m + 4] = struct.pack('<I', tva)
                        self.fix[site] = tva
                    elif t == 5:
                        self.mem[m:m + 2] = struct.pack('<H', tva & 0xffff)
                        self.fix[site] = tva
        self.rev = {}
        for s, t in self.fix.items():
            self.rev.setdefault(t, []).append(s)

    def file_offset(self, va):
        """Where va's byte is in the exe file (None if not backed by a page)."""
        for p, pva in self.page_va.items():
            if pva <= va < pva + 0x1000:
                return self.page_off[p] + (va - pva)
        return None

    def read(self, va, n):
        return bytes(self.mem[va - self.base:va - self.base + n])

    def cstr(self, va, n=200):
        b = self.read(va, n)
        return b.split(b'\0')[0].decode('latin1')

    def find(self, needle):
        out, i = [], 0
        while True:
            i = self.mem.find(needle, i)
            if i < 0:
                return out
            out.append(self.base + i)
            i += 1

    def xrefs(self, va):
        return sorted(self.rev.get(va, []))

    def code(self):
        return self.objs[0][0], self.objs[0][1]


def load(path):
    return Image(open(path, 'rb').read())


if __name__ == '__main__':
    img = load(sys.argv[1])
    if len(sys.argv) == 2:
        for k, (b, vs, fl, pti, npg) in enumerate(img.objs, 1):
            print('obj %d base %08x size %08x flags %04x pages %d' % (k, b, vs, fl, npg))
        print('entry obj %d eip %x, %d fixups' % (img.eip_obj, img.eip, len(img.fix)))
    elif sys.argv[2] == 'str':
        for va in img.find(sys.argv[3].encode('latin1')):
            print('%08x %r  xrefs: %s' % (va, img.cstr(va, 80), ' '.join('%08x' % s for s in img.xrefs(va))))
