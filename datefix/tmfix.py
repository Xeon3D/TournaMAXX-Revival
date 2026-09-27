#!/usr/bin/env python3
"""Tournament date fix for DOS Megatouch MAXX MEGACDLL.EXE (Diamond V6.03,
Double Diamond V7.01, Emerald V8.04): store tournament times in minutes.

These releases keep a tournament's START, END and SHOWDATE in Tourney.dbf as
`%8d` of (time - 930000000) seconds, which stops fitting on 2002-08-21: the
ninth digit is cut off and the time reads back as early 2002.  Emerald 2
stores minutes.  This rewrites the load and save code in place, with the same
registers and every call left at its own address, so that they store
(time - 930000000) / 60 and load value * 60 + 930000000 (good until 2038).

The version shown on screen goes from x.yy to x.20 (V8.04 -> V8.20), so a
fixed game can be told apart; nothing compares it (only printed and logged).

Usage: tmfix.py check <exe>              -> unpatched / patched / unknown
       tmfix.py patch <in.exe> <out.exe> -> writes the fixed exe
Existing Tourney.dbf records are in the old encoding: V8.04's update package
deletes the file; on the others the server's next call sends the running
tournaments again, which rewrites them.  Prints input and output SHA256.
"""
import hashlib
import re
import os
import struct
import sys

import capstone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lefile as le  # noqa: E402

BASE = 930000000
LOAD_OLD = b'\x81\x80\x13\x01\x00\x00' + struct.pack('<I', BASE)      # add dword [eax+0x113], BASE
LOAD_NEW = b'\x6b\x90\x13\x01\x00\x00\x3c\x81\xc2' + struct.pack('<I', BASE)  # imul edx,[eax+0x113],60; add edx,BASE
SAVE_OLD = b'\x2d' + struct.pack('<I', BASE)                         # sub eax, BASE
VERSION = re.compile(rb'(PG\d{4} V)(\d)\.(\d\d)([\t ]+\(\d\d/\d\d/\d\d\))')
MD = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
MD.detail = True


def insns(img, va, n):
    return list(MD.disasm(img.read(va, n), va))


def is_(i, mnem, ops):
    return i.mnemonic == mnem and i.op_str.replace(' ', '') == ops.replace(' ', '')


def disp(i):
    for op in i.operands:
        if op.type == capstone.x86.X86_OP_MEM:
            return op.mem.disp
    raise ValueError(i)


def plan_load(img, site):
    """The load block: pairs of `mov eax,[ebp-4]` and either `add dword
    [eax+d], BASE` or `mov byte [eax+d], 0`, up to the epilogue, which has to
    restore edx (so edx is free)."""
    start = site - 3
    code = insns(img, start, 0x200)
    adds, clears, k = [], [], 0
    while not is_(code[k], 'mov', 'esp, ebp'):
        a, b = code[k], code[k + 1]
        if not is_(a, 'mov', 'eax, dword ptr [ebp - 4]'):
            raise ValueError('load block: unexpected %s %s at %x' % (a.mnemonic, a.op_str, a.address))
        if b.mnemonic == 'add' and b.op_str.endswith(hex(BASE)):
            adds.append(disp(b))
        elif b.mnemonic == 'mov' and b.op_str.startswith('byte ptr [eax') and b.op_str.endswith(', 0'):
            clears.append(disp(b))
        else:
            raise ValueError('load block: unexpected %s %s at %x' % (b.mnemonic, b.op_str, b.address))
        k += 2
    end = code[k].address
    epi = [(i.mnemonic, i.op_str) for i in code[k + 1:k + 6]]
    if ('pop', 'edx') not in epi:
        raise ValueError('load block: edx is not saved by the function')
    new = b'\x8b\x45\xfc'
    for d in adds:
        new += b'\x6b\x90' + struct.pack('<i', d) + b'\x3c'      # imul edx, [eax+d], 60
        new += b'\x81\xc2' + struct.pack('<I', BASE)            # add edx, BASE
        new += b'\x89\x90' + struct.pack('<i', d)               # mov [eax+d], edx
    for d in clears:
        new += b'\xc6\x80' + struct.pack('<i', d) + b'\x00'      # mov byte [eax+d], 0
    if len(new) > end - start:
        raise ValueError('load block: no room')
    return [(start, new + b'\x90' * (end - start - len(new)))], len(adds)


def plan_save(img, site):
    """A save block: field -> sub BASE -> [ebp-8] -> ebx -> writer(eax=this,
    edx=field slot, ebx=value).  The call stays where it is."""
    start = site - 9
    c = insns(img, start, 60)
    want = ['mov eax, dword ptr [ebp - 4]', None, 'sub eax, %s' % hex(BASE),
            'mov dword ptr [ebp - 8], eax', 'mov ebx, dword ptr [ebp - 8]',
            'mov eax, dword ptr [ebp - 4]', None, None, 'mov eax, dword ptr [ebp - 4]', None]
    for i, w in zip(c, want):
        if w and (i.mnemonic + ' ' + i.op_str) != w:
            raise ValueError('save block: unexpected %s %s at %x' % (i.mnemonic, i.op_str, i.address))
    if not (c[1].op_str.startswith('eax, dword ptr [eax') and c[6].op_str.startswith('eax, dword ptr [eax')
            and c[7].mnemonic == 'lea' and c[7].op_str.startswith('edx, [eax') and c[9].mnemonic == 'call'):
        raise ValueError('save block: unexpected shape at %x' % start)
    f, slot, fld = disp(c[1]), disp(c[6]), disp(c[7])
    new = b'\x8b\x45\xfc' + b'\x8b\x80' + struct.pack('<i', f)          # mov eax,[ebp-4]; mov eax,[eax+f]
    new += b'\x99\x6a\x3c\x5b\xf7\xfb'                                  # cdq; push 60; pop ebx; idiv ebx
    new += b'\x8d\x98' + struct.pack('<i', -(BASE // 60))               # lea ebx, [eax - BASE/60]
    new += b'\x8b\x45\xfc' + b'\x8b\x90' + struct.pack('<i', slot)      # mov eax,[ebp-4]; mov edx,[eax+slot]
    new += b'\x81\xc2' + struct.pack('<i', fld)                         # add edx, fld
    room = c[9].address - start
    if len(new) > room:
        raise ValueError('save block: no room')
    return [(start, new + b'\x90' * (room - len(new)))]


def plan(img):
    code_b, code_n = img.objs[0][0], img.objs[0][1]
    code = img.read(code_b, code_n)
    if code.count(LOAD_NEW):
        return 'patched', []
    loads = [code_b + i for i in range(len(code)) if code.startswith(LOAD_OLD, i)]
    saves, i = [], code.find(SAVE_OLD)
    while i >= 0:
        saves.append(code_b + i)
        i = code.find(SAVE_OLD, i + 1)
    if len(loads) != 1 or not saves:
        return 'unknown', []
    edits, nfields = plan_load(img, loads[0])
    for s in saves:
        edits += plan_save(img, s)
    if len(saves) != nfields:
        raise ValueError('%d fields loaded, %d saved' % (nfields, len(saves)))
    # No relocation may land in what changes.
    for va, new in edits:
        for s in list(img.fix) + list(img.imports):
            if va - 3 < s < va + len(new):
                raise ValueError('fixup at %x inside the edit at %x' % (s, va))
    return 'unpatched', edits


def sha(b):
    return hashlib.sha256(b).hexdigest()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'check':
        print(plan(le.load(sys.argv[2]))[0])
        return
    if len(sys.argv) != 4 or sys.argv[1] != 'patch':
        sys.exit(__doc__)
    src, dst = sys.argv[2], sys.argv[3]
    data = open(src, 'rb').read()
    img = le.load(src)
    state, edits = plan(img)
    if state != 'unpatched':
        sys.exit('%s: %s, nothing to do' % (src, state))
    out = bytearray(data)
    for va, new in edits:
        for k, byte in enumerate(new):
            out[img.file_offset(va + k)] = byte
        print('%x: %d bytes' % (va, len(new)))
    versions = set()

    def bump(m):
        versions.add((m.group(2) + b'.' + m.group(3)).decode())
        return m.group(1) + m.group(2) + b'.20' + m.group(4)
    out = bytearray(VERSION.sub(bump, bytes(out)))
    print('version %s -> %s.20' % (', '.join(sorted(versions)) or 'none found',
                                   ', '.join(sorted({v[0] for v in versions}))))
    open(dst, 'wb').write(out)
    print('in  %s %s' % (sha(data), src))
    print('out %s %s' % (sha(bytes(out)), dst))


if __name__ == '__main__':
    main()
