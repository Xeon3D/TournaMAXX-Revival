#!/usr/bin/env python3
"""Make a TournaMAXX update package for DOS Megatouch MAXX cabinets.

Run without arguments for the window; or

    python tools/mkupdate.py <folder> [-o NETUPDT.EXE] [--name NAME]
                             [--protocol N] [--result C:\\RESULT.TXT]

<folder> holds the files as they are to land on the cabinet's C:\\, e.g.

    <folder>\\NETUPDT.BAT
    <folder>\\MERIT2\\EXEC\\MEGACDLL.NEW

and becomes one PKZIP 2.04g self-extractor, NETUPDT.EXE, the kind Merit
sent as network updates: the cabinet's TEST.BAT runs "NetUpdt -d -o c:\\"
at boot (extract everything under C:\\, with its folders, overwriting),
deletes NetUpdt.exe, then runs the C:\\NETUPDT.BAT that came out of it (if
any) and deletes that too.  The server sends the package to C:\\NETUPDT.EXE
and reboots the cabinet (tools/modem-server.py, "updates" in its state
file; this prints the entry to add).

A ZIP2EXE self-extractor is PKSFX 2.04g's 15,770-byte stub followed by an
ordinary zip whose offsets count from the start of the file.  The stub is
carried below (as ZIP2EXE writes it); the zip is made here, deflated, with
DOS names and attributes.  Nothing else is needed.  Every file is checked
in the finished package against its source, byte for byte.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import zipfile
import zlib

NAME_83 = re.compile(r"^[A-Z0-9_$~!#%&'()@^`{}-]{1,8}(\.[A-Z0-9_$~!#%&'()@^`{}-]{1,3})?$")
STUB_SHA256 = "62a2c23d06c31b410645a2e691a2f203c4884901992bf8467880b508590399ee"
DOS_ARCHIVE, DOS_DIRECTORY = 0x20, 0x10


class Problem(Exception):
    pass


def stub():
    b = base64.b64decode(PKSFX_204G)
    if hashlib.sha256(b).hexdigest() != STUB_SHA256:
        raise Problem("the PKSFX stub in this program is damaged")
    return b


def payload(folder):
    """(DOS path, source path) of every file under folder; names must be 8.3."""
    if not os.path.isdir(folder):
        raise Problem("%s is not a folder" % folder)
    files, bad = [], []
    for root, dirs, names in os.walk(folder):
        dirs.sort()
        for n in sorted(names):
            src = os.path.join(root, n)
            rel = os.path.relpath(src, folder).split(os.sep)
            if all(NAME_83.match(p.upper()) for p in rel):
                files.append(("\\".join(p.upper() for p in rel), src))
            else:
                bad.append(os.path.join(*rel))
    if bad:
        raise Problem("not DOS 8.3 names: " + ", ".join(bad))
    if not files:
        raise Problem("%s has no files" % folder)
    return files


def dos_time(path):
    t = time.localtime(os.path.getmtime(path))
    return max(t[:6], (1980, 1, 1, 0, 0, 0))


def build(files):
    """The self-extractor's bytes: the stub, then the zip, offsets from the
    start of the file.  Entries as PKZIP -ex -rp writes them: each folder's
    contents (subfolders as entries of their own, attribute 0x30) in name
    order, then the subfolders' contents; files deflated."""
    entries = {}                              # DOS path -> (source, is folder)
    for dos, src in files:
        entries[dos] = (src, False)
        parts = dos.split("\\")
        for k in range(1, len(parts)):
            d = "\\".join(parts[:k])
            entries.setdefault(d, (os.path.join(src, *[".."] * (len(parts) - k)), True))
    order = sorted(entries, key=lambda p: (p.count("\\"), p.rsplit("\\", 1)[0] if "\\" in p else "", p))
    out = io.BytesIO()
    out.write(stub())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dos in order:
            src, is_dir = entries[dos]
            name = dos.replace("\\", "/") + ("/" if is_dir else "")
            zi = zipfile.ZipInfo(name, dos_time(os.path.normpath(src)))
            zi.create_system = 0
            if is_dir:
                zi.external_attr = DOS_DIRECTORY | DOS_ARCHIVE
                z.writestr(zi, b"", zipfile.ZIP_STORED)
                continue
            with open(src, "rb") as f:
                data = f.read()
            zi.external_attr = DOS_ARCHIVE
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data, zipfile.ZIP_DEFLATED, 9)
    return out.getvalue()


def verify(exe, files):
    """The package opens as a zip after the stub, passes its CRCs, and holds
    exactly the source files, whole, under their DOS paths."""
    if not exe.startswith(stub()):
        raise Problem("the package does not start with the PKSFX stub")
    with zipfile.ZipFile(io.BytesIO(exe)) as z:
        if z.testzip() is not None:
            raise Problem("the package fails its own CRC check")
        inside = {i.filename.replace("/", "\\"): i for i in z.infolist()
                  if not i.filename.endswith("/")}
        for dos, src in files:
            if dos not in inside:
                raise Problem("%s is missing from the package" % dos)
            with open(src, "rb") as f:
                if z.read(inside[dos]) != f.read():
                    raise Problem("%s differs in the package" % dos)
        extra = set(inside) - {d for d, _ in files}
        if extra:
            raise Problem("unexpected files in the package: " + ", ".join(sorted(extra)))


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def default_output(folder):
    return os.path.join(os.path.dirname(os.path.abspath(folder)), "NETUPDT.EXE")


def make(folder, output, name=None, protocol=None, result=None, say=print):
    """Build and check the package; returns the server entry."""
    folder = os.path.abspath(folder)
    output = os.path.abspath(output or default_output(folder))
    if output.startswith(folder + os.sep):
        raise Problem("the package cannot go inside the folder it is made from")
    files = payload(folder)
    if not any(d == "NETUPDT.BAT" for d, _ in files):
        say("note: no NETUPDT.BAT: the files are only extracted, nothing runs")
    exe = build(files)
    verify(exe, files)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "wb") as f:
        f.write(exe)
    size = sum(os.path.getsize(s) for _, s in files)
    say("%d file(s), %d bytes -> %s, %d bytes" % (len(files), size, output, len(exe)))
    for dos, src in files:
        with open(src, "rb") as f:
            say("  %s  C:\\%s" % (sha256(f.read()), dos))
    say("  %s  %s" % (sha256(exe), output))
    entry = {"send": [[output, "C:\\NETUPDT.EXE"]]}
    if protocol is not None:
        entry["protocol"] = protocol
    if result:
        entry["result"] = result
    return {name or os.path.basename(folder): entry}


# ------------------------------------------------------------------ window

def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("MegaPPBox update package maker")
    root.minsize(640, 480)
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    folder, output = tk.StringVar(), tk.StringVar()
    name, protocol, result = tk.StringVar(), tk.StringVar(), tk.StringVar()

    def pick_folder():
        d = filedialog.askdirectory(title="Folder laid out as the cabinet's C:\\")
        if d:
            folder.set(os.path.normpath(d))
            output.set(default_output(d))
            name.set(os.path.basename(os.path.normpath(d)))

    def pick_output():
        f = filedialog.asksaveasfilename(title="Save the package as", initialfile="NETUPDT.EXE",
                                         defaultextension=".EXE",
                                         filetypes=[("DOS program", "*.EXE"), ("All files", "*.*")])
        if f:
            output.set(os.path.normpath(f))

    rows = [("Folder (the cabinet's C:\\)", folder, pick_folder),
            ("Package", output, pick_output),
            ("Update name", name, None),
            ("Only for protocol version", protocol, None),
            ("Result file (optional)", result, None)]
    for r, (label, var, browse) in enumerate(rows):
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6)
        if browse:
            ttk.Button(frm, text="Browse...", command=browse).grid(row=r, column=2)
    ttk.Label(frm, foreground="gray",
              text="Protocol: Emerald V8.04 logs in as 7, Emerald 2 as 9. Empty: every cabinet. "
                   "Result: a file NETUPDT.BAT writes, e.g. C:\\RESULT.TXT.").grid(
        row=len(rows), column=0, columnspan=3, sticky="w", pady=(2, 8))

    log = tk.Text(frm, height=10, wrap="none", font=("Consolas", 9))
    log.grid(row=len(rows) + 2, column=0, columnspan=3, sticky="nsew")
    ttk.Label(frm, text="For the server's state file, under \"updates\":").grid(
        row=len(rows) + 3, column=0, columnspan=3, sticky="w", pady=(8, 0))
    entry_box = tk.Text(frm, height=8, wrap="none", font=("Consolas", 9))
    entry_box.grid(row=len(rows) + 4, column=0, columnspan=3, sticky="nsew")
    frm.rowconfigure(len(rows) + 2, weight=2)
    frm.rowconfigure(len(rows) + 4, weight=1)

    def say(text):
        root.after(0, lambda: (log.insert("end", text + "\n"), log.see("end")))

    def copy_entry():
        root.clipboard_clear()
        root.clipboard_append(entry_box.get("1.0", "end").strip())

    def run():
        if not folder.get():
            messagebox.showwarning("Update package", "Choose the folder first.")
            return
        try:
            proto = int(protocol.get()) if protocol.get().strip() else None
        except ValueError:
            messagebox.showwarning("Update package", "The protocol version is a number (e.g. 7).")
            return
        log.delete("1.0", "end")
        entry_box.delete("1.0", "end")
        make_btn.state(["disabled"])

        def work():
            try:
                e = make(folder.get(), output.get() or None, name.get() or None, proto,
                         result.get().strip() or None, say)
                root.after(0, lambda: entry_box.insert("1.0", json.dumps(e, indent=1)))
                say("done")
            except Problem as p:
                say("stopped: %s" % p)
                root.after(0, lambda: messagebox.showerror("Update package", str(p)))
            except OSError as p:
                say("stopped: %s" % p)
                root.after(0, lambda: messagebox.showerror("Update package", str(p)))
            finally:
                root.after(0, lambda: make_btn.state(["!disabled"]))
        threading.Thread(target=work, daemon=True).start()

    bar = ttk.Frame(frm)
    bar.grid(row=len(rows) + 1, column=0, columnspan=3, sticky="e", pady=(0, 6))
    ttk.Button(bar, text="Copy server entry", command=copy_entry).pack(side="right", padx=(6, 0))
    make_btn = ttk.Button(bar, text="Make package", command=run)
    make_btn.pack(side="right")
    root.mainloop()


def main():
    if len(sys.argv) == 1:
        gui()
        return
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 2)[2])
    ap.add_argument("folder", help="files as they are to land on C:\\")
    ap.add_argument("-o", "--output", help="the package (default: NETUPDT.EXE next to the folder)")
    ap.add_argument("--name", help="update name for the server entry (default: the folder's name)")
    ap.add_argument("--protocol", type=int,
                    help="send only to cabinets whose login has this protocol version "
                         "(Emerald V8.04: 7)")
    ap.add_argument("--result", help="a file NETUPDT.BAT writes, fetched on the next call "
                                     "(e.g. C:\\RESULT.TXT)")
    a = ap.parse_args()
    out = sys.stdout if sys.stdout else open(os.devnull, "w")
    say = lambda s: print(s, file=out)
    try:
        e = make(a.folder, a.output, a.name, a.protocol, a.result, say)
    except (Problem, OSError) as p:
        if sys.stderr:
            print("mkupdate: %s" % p, file=sys.stderr)
        sys.exit(1)
    say('\nFor the server\'s state file, under "updates":')
    say(json.dumps(e, indent=1))


# PKSFX (R) 2.04g, the self-extractor stub PKZIP 2.04g's ZIP2EXE puts in front
# of the zip (Copr. 1989-1993 PKWARE Inc.), base64.
PKSFX_204G = """
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
"""

if __name__ == "__main__":
    main()
