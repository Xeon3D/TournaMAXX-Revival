#!/usr/bin/env python3
"""
TournaMAXX-Revival's updater for a deploy/install.sh server: run as root by
tournamaxx-update.service when the control panel leaves
/var/lib/tournamaxx/update/request.json (tournamaxx-update.path watches for
it).

It downloads that release's source from GitHub, checks that its VERSION is
the one asked for, and runs its deploy/install.sh --update, which installs
the code and restarts the server and the panel; the data and config stay.
/var/lib/tournamaxx/update/status.json says how it went, and install.log
has the installer's output.  Only a version number is taken from the
request.  Python 3, standard library only.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

REPO = "Xeon3D/TournaMAXX-Revival"
DATA = os.environ.get("TMX_DATA", "/var/lib/tournamaxx")
UPDATE = os.path.join(DATA, "update")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
STATUS = {}


def now():
    return int(time.time())


def write(name, obj):
    path = os.path.join(UPDATE, name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.chmod(path + ".tmp", 0o644)
    os.replace(path + ".tmp", path)


def status(state, message, **extra):
    STATUS.update(state=state, message=message, at=now(), **extra)
    write("status.json", STATUS)
    print("%s: %s" % (state, message), flush=True)


def take_request():
    path = os.path.join(UPDATE, "request.json")
    try:
        with open(path, encoding="utf-8") as f:
            req = json.load(f)
    except (OSError, ValueError):
        req = {}
    try:
        os.remove(path)               # first: the path unit would start this again while it is there
    except OSError:
        pass
    return req if isinstance(req, dict) else {}


def fetch(version, into):
    url = "https://github.com/%s/archive/refs/tags/v%s.tar.gz" % (REPO, version)
    tgz = os.path.join(into, "src.tar.gz")
    req = urllib.request.Request(url, headers={"User-Agent": "TournaMAXX-updater"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tgz, "wb") as f:
        shutil.copyfileobj(r, f)
    with tarfile.open(tgz) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(into, filter="data")
        else:                         # Python before 3.12: check the names by hand
            for m in t.getmembers():
                p = os.path.realpath(os.path.join(into, m.name))
                if not p.startswith(os.path.realpath(into) + os.sep) or m.issym() or m.islnk() or m.isdev():
                    raise RuntimeError("unexpected entry in the download: %s" % m.name)
            t.extractall(into)
    dirs = [d for d in os.listdir(into) if os.path.isdir(os.path.join(into, d))]
    if len(dirs) != 1:
        raise RuntimeError("the download is not laid out as expected")
    src = os.path.join(into, dirs[0])
    try:
        with open(os.path.join(src, "VERSION"), encoding="utf-8") as f:
            got = f.read().strip()
    except FileNotFoundError:
        raise RuntimeError("the v%s download has no VERSION file: it is older than the updater" % version)
    if got != version:
        raise RuntimeError("the v%s download says it is version %s" % (version, got))
    return src


def main():
    if os.geteuid() != 0:
        sys.exit("run as root (tournamaxx-update.service does)")
    req = take_request()
    version = str(req.get("version") or "").lstrip("v")
    STATUS.update(version=version, started=now(), by=req.get("by"))
    try:
        if not VERSION_RE.match(version):
            raise RuntimeError("not a version: %r" % version)
        status("downloading", "downloading %s from GitHub" % version)
        with tempfile.TemporaryDirectory(prefix="tournamaxx-update-") as tmp:
            src = fetch(version, tmp)
            status("installing", "installing %s; the server and the panel restart" % version)
            with open(os.path.join(UPDATE, "install.log"), "wb") as out:
                r = subprocess.run(["sh", os.path.join(src, "deploy", "install.sh"), "--update"],
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=1800)
            if r.returncode:
                raise RuntimeError("install.sh failed (exit %d); see %s" % (r.returncode, os.path.join(UPDATE, "install.log")))
        status("done", "updated to %s" % version)
    except Exception as e:
        status("failed", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
