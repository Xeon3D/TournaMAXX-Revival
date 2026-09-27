#!/usr/bin/env python3
"""Make a TournaMAXX update package for DOS Megatouch MAXX cabinets.

    python tools/mkupdate.py <folder> [-o NETUPDT.EXE] [--name NAME]
                             [--protocol N] [--result C:\\RESULT.TXT]

<folder> holds the files as they are to land on the cabinet's C:\\, e.g.

    <folder>\\NETUPDT.BAT
    <folder>\\MERIT2\\EXEC\\MEGACDLL.NEW

and becomes one PKZIP 2.04g self-extractor, NETUPDT.EXE, made the way
Merit made its network updates: the cabinet's TEST.BAT runs
"NetUpdt -d -o c:\\" at boot (extract everything under C:\\, with its
folders, overwriting), deletes NetUpdt.exe, then runs the C:\\NETUPDT.BAT
that came out of it (if any) and deletes that too.  The server sends the
package to C:\\NETUPDT.EXE and reboots the cabinet (tools/modem-server.py,
"updates" in its state file; this prints the entry to add).

Needs PKZIP.EXE and ZIP2EXE.EXE from PKZIP 2.04g (--pkzip, or the
MEGAPPBOX_PKZIP environment variable: the folder holding them; mkupdate.exe
has its own) and DOSBox-X
(--dosbox, or MEGAPPBOX_DOSBOX, or C:\\DOSBox-X\\dosbox-x.exe, or dosbox-x on
the PATH), which runs them.  The folder given is only read.  Every file is
checked in the finished package against its source, byte for byte.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

NAME_83 = re.compile(r"^[A-Z0-9_$~!#%&'()@^`{}-]{1,8}(\.[A-Z0-9_$~!#%&'()@^`{}-]{1,3})?$")
PKZIP_FILES = ("PKZIP.EXE", "ZIP2EXE.EXE")


def die(msg):
    sys.exit("mkupdate: " + msg)


def find_file(folder, name):
    """name in folder, whatever its case."""
    for f in os.listdir(folder):
        if f.upper() == name:
            return os.path.join(folder, f)
    return None


def find_dosbox(given):
    for c in (given, os.environ.get("MEGAPPBOX_DOSBOX"), r"C:\DOSBox-X\dosbox-x.exe",
              shutil.which("dosbox-x")):
        if c and os.path.isfile(c):
            return c
    die("DOSBox-X not found: give --dosbox or set MEGAPPBOX_DOSBOX")


def find_pkzip(given):
    # mkupdate.exe (tools/mkupdate.spec) carries its own copies.
    bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "pkzip")
    folder = given or os.environ.get("MEGAPPBOX_PKZIP") or (
        bundled if getattr(sys, "frozen", False) else None)
    if not folder:
        die("PKZIP 2.04g not found: give --pkzip (the folder with PKZIP.EXE and "
            "ZIP2EXE.EXE) or set MEGAPPBOX_PKZIP")
    found = {n: find_file(folder, n) for n in PKZIP_FILES}
    missing = [n for n, p in found.items() if not p]
    if missing:
        die("%s has no %s" % (folder, ", ".join(missing)))
    return found


def payload(folder):
    """(DOS path, source path) of every file under folder; names must be 8.3."""
    files, bad = [], []
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        for n in sorted(names):
            src = os.path.join(root, n)
            rel = os.path.relpath(src, folder).split(os.sep)
            for part in rel:
                if not NAME_83.match(part.upper()):
                    bad.append(os.path.join(*rel))
                    break
            else:
                files.append(("\\".join(p.upper() for p in rel), src))
    if bad:
        die("not DOS 8.3 names: " + ", ".join(bad))
    if not files:
        die("%s has no files" % folder)
    return files


def build(files, pk, dosbox, work):
    """PKZIP -ex -rp, then ZIP2EXE, in DOSBox-X; returns NETUPDT.EXE's path."""
    for d in ("PK", "PKG", "OUT"):
        os.makedirs(os.path.join(work, d))
    for name, path in pk.items():
        shutil.copyfile(path, os.path.join(work, "PK", name))
    for dos, src in files:
        dst = os.path.join(work, "PKG", *dos.split("\\"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    with open(os.path.join(work, "BUILD.BAT"), "w", newline="\r\n") as f:
        f.write("@echo off\ncd \\PKG\n\\PK\\PKZIP -ex -rp \\OUT\\NETUPDT.ZIP *.*\n"
                "cd \\OUT\n\\PK\\ZIP2EXE NETUPDT.ZIP\n")
    conf = os.path.join(work, "build.conf")
    with open(conf, "w") as f:
        f.write('[sdl]\nautolock=false\n[cpu]\ncycles=max\nturbo=true\n'
                '[autoexec]\nmount c "%s"\nc:\n'
                'call BUILD.BAT > BUILD.LOG\nexit\n' % work)
    kw = {}
    if os.name == "nt":                     # keep the DOSBox-X window out of the way
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                  # SW_SHOWMINNOACTIVE
        kw["startupinfo"] = si
    subprocess.run([dosbox, "-fastlaunch", "-nopromptfolder", "-nomenu", "-noconsole",
                    "-nolog", "-conf", conf], check=False, timeout=600, **kw)
    out = os.path.join(work, "OUT", "NETUPDT.EXE")
    if not os.path.isfile(out):
        log = os.path.join(work, "BUILD.LOG")
        text = open(log, errors="replace").read() if os.path.isfile(log) else "(no log)"
        die("ZIP2EXE made nothing; DOS output:\n" + text)
    return out


def verify(exe, files):
    """Every source file is in the package, whole, under its DOS path."""
    with zipfile.ZipFile(exe) as z:
        if z.testzip() is not None:
            die("the package fails its own CRC check")
        inside = {i.filename.replace("/", "\\").upper(): i for i in z.infolist()
                  if not i.filename.endswith("/")}
        for dos, src in files:
            if dos not in inside:
                die("%s is missing from the package" % dos)
            with open(src, "rb") as f:
                if z.read(inside[dos]) != f.read():
                    die("%s differs in the package" % dos)
        extra = set(inside) - {d for d, _ in files}
        if extra:
            die("unexpected files in the package: " + ", ".join(sorted(extra)))


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("folder", help="files as they are to land on C:\\")
    ap.add_argument("-o", "--output", help="the package (default: NETUPDT.EXE next to the folder)")
    ap.add_argument("--name", help="update name for the server entry (default: the folder's name)")
    ap.add_argument("--protocol", type=int,
                    help="send only to cabinets whose login has this protocol version "
                         "(Emerald V8.04: 7)")
    ap.add_argument("--result", help="a file NETUPDT.BAT writes, fetched on the next call "
                                     "(e.g. C:\\RESULT.TXT)")
    ap.add_argument("--pkzip", help="folder with PKZIP 2.04g's PKZIP.EXE and ZIP2EXE.EXE")
    ap.add_argument("--dosbox", help="dosbox-x executable")
    ap.add_argument("--keep", action="store_true", help="keep the work folder (for debugging)")
    a = ap.parse_args()

    folder = os.path.abspath(a.folder)
    if not os.path.isdir(folder):
        die("%s is not a folder" % folder)
    output = os.path.abspath(a.output or os.path.join(os.path.dirname(folder), "NETUPDT.EXE"))
    if output.startswith(folder + os.sep):
        die("the package cannot go inside the folder it is made from")
    files = payload(folder)
    if not any(d == "NETUPDT.BAT" for d, _ in files):
        print("note: no NETUPDT.BAT: the files are only extracted, nothing runs")
    pk = find_pkzip(a.pkzip)
    dosbox = find_dosbox(a.dosbox)

    work = tempfile.mkdtemp(prefix="mkupdate-")
    try:
        exe = build(files, pk, dosbox, work)
        verify(exe, files)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        shutil.copyfile(exe, output)
    finally:
        if a.keep:
            print("work folder kept: " + work)
        else:
            shutil.rmtree(work, ignore_errors=True)

    size = sum(os.path.getsize(s) for _, s in files)
    print("%d file(s), %d bytes -> %s, %d bytes" % (len(files), size, output, os.path.getsize(output)))
    for dos, src in files:
        print("  %s  C:\\%s" % (sha256(src), dos))
    print("  %s  %s" % (sha256(output), output))
    entry = {"send": [[output, "C:\\NETUPDT.EXE"]]}
    if a.protocol is not None:
        entry["protocol"] = a.protocol
    if a.result:
        entry["result"] = a.result
    name = a.name or os.path.basename(folder)
    print('\nFor the server\'s state file, under "updates":')
    print(json.dumps({name: entry}, indent=1))


if __name__ == "__main__":
    main()
