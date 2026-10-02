#!/usr/bin/env python3
"""
TournaMAXX-Revival's updater for Docker: a sidecar that updates the panel's
container when the panel asks (Settings > Updates > Update).

It runs from the same image, as root, with the Docker socket; the panel has
neither.  They share the data folder: the panel leaves
/data/update/request.json ({"version": "0.2.0"}), and this

  - pulls the new image (the container's own tag when it follows "latest",
    else the version's tag),
  - stops the container, keeps it under another name, and makes a new one
    with the same settings on the new image (what the image itself set, such
    as its environment and command, comes from the new image),
  - starts it and waits for its health check; if it does not come up
    healthy, the old container comes back,
  - writes /data/update/status.json as it goes, and /data/update/updater.json
    every few seconds so that the panel knows it is here.

Only xeon3d/tournamaxx-revival is pulled, and only a version number is taken
from the request.  Python 3, standard library only.

    TMX_UPDATE_CONTAINER   the container to update (default: tournamaxx)
    TMX_UPDATE_IMAGE       the image it must run (default: xeon3d/tournamaxx-revival)
    DOCKER_SOCKET          (default: /var/run/docker.sock)
"""

import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.parse

DATA = os.environ.get("TMX_DATA", "/data")
UPDATE = os.path.join(DATA, "update")
CONTAINER = os.environ.get("TMX_UPDATE_CONTAINER", "tournamaxx")
IMAGE = os.environ.get("TMX_UPDATE_IMAGE", "xeon3d/tournamaxx-revival")
SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
HEALTH_WAIT = 90          # seconds for the new container to become healthy
BEAT = 5


def now():
    return int(time.time())


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


# ------------------------------------------------------------------ files shared with the panel

def owner():
    st = os.stat(DATA)
    return st.st_uid, st.st_gid


def write(name, obj):
    """Atomically, and owned like /data, so that the panel (another user) can
    read it and replace what it must."""
    os.makedirs(UPDATE, exist_ok=True)
    uid, gid = owner()
    try:
        os.chown(UPDATE, uid, gid)
    except OSError:
        pass
    path = os.path.join(UPDATE, name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f)
    try:
        os.chown(path + ".tmp", uid, gid)
    except OSError:
        pass
    os.replace(path + ".tmp", path)


STATUS = {}


def status(state, message, **extra):
    STATUS.update(state=state, message=message, at=now(), **extra)
    write("status.json", STATUS)
    log("%s: %s" % (state, message))


def take_request():
    path = os.path.join(UPDATE, "request.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            req = json.load(f)
    except (OSError, ValueError):
        req = {}
    os.remove(path)
    return req if isinstance(req, dict) else {}


# ------------------------------------------------------------------ the Docker Engine API

class DockerError(Exception):
    def __init__(self, code, message):
        super().__init__("Docker: %s (%d)" % (message, code))
        self.code = code


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(SOCKET)
        self.sock = s


def docker(method, path, body=None, timeout=60, **query):
    if query:
        path += "?" + urllib.parse.urlencode(query)
    c = UnixConnection(timeout)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        c.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        r = c.getresponse()
        raw = r.read()
    finally:
        c.close()
    if r.status >= 400:
        try:
            msg = json.loads(raw)["message"]
        except (ValueError, KeyError, TypeError):
            msg = raw.decode("utf-8", "replace").strip() or r.reason
        raise DockerError(r.status, msg)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def pull(repo, tag):
    """POST /images/create streams progress lines; an error comes as one of them."""
    out = docker("POST", "/images/create", timeout=1800, fromImage=repo, tag=tag)
    text = out if isinstance(out, str) else json.dumps(out or {})
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("error"):
            raise DockerError(500, j["error"])


def split_ref(ref):
    """'xeon3d/x:latest' -> ('xeon3d/x', 'latest'); a digest is not followed."""
    ref = ref.split("@")[0]
    name, _, tag = ref.rpartition(":")
    if not name or "/" in tag:
        return ref, "latest"
    return name, tag


def same_repo(repo):
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    return repo == IMAGE


# ------------------------------------------------------------------ recreating the container

IMAGE_KEYS = ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "ExposedPorts", "Volumes",
              "StopSignal", "Shell", "OnBuild")
ENDPOINT_KEYS = ("IPAMConfig", "Links", "Aliases", "DriverOpts")


def new_config(c, old_image, ref):
    """The container's create request, on ref.  What the old image set (and
    the container only inherited) is left out, so the new image's own applies."""
    img = old_image.get("Config") or {}
    cfg = dict(c["Config"])
    cfg["Image"] = ref
    if cfg.get("Hostname") == c["Id"][:12]:
        cfg.pop("Hostname")
    cfg["Env"] = [e for e in cfg.get("Env") or [] if e not in (img.get("Env") or [])]
    labels = img.get("Labels") or {}
    cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if labels.get(k) != v}
    for k in IMAGE_KEYS:
        if k in cfg and cfg[k] == img.get(k):
            cfg.pop(k)
    nets = {}
    for name, ep in ((c.get("NetworkSettings") or {}).get("Networks") or {}).items():
        e = {k: ep[k] for k in ENDPOINT_KEYS if ep.get(k)}
        if e.get("Aliases"):
            e["Aliases"] = [a for a in e["Aliases"] if a != c["Id"][:12]]
        nets[name] = e
    cfg["HostConfig"] = c["HostConfig"]
    cfg["NetworkingConfig"] = {"EndpointsConfig": nets}
    return cfg


def wait_healthy(cid):
    """True when it runs and is healthy (or has no health check) within HEALTH_WAIT."""
    end = time.time() + HEALTH_WAIT
    while time.time() < end:
        st = docker("GET", "/containers/%s/json" % cid)["State"]
        if not st.get("Running"):
            return False, "it stopped (exit %s)" % st.get("ExitCode")
        health = (st.get("Health") or {}).get("Status")
        if health in (None, "healthy"):
            return True, health or "running"
        if health == "unhealthy":
            return False, "its health check failed"
        time.sleep(3)
    return False, "it was not healthy after %d seconds" % HEALTH_WAIT


def update(version):
    c = docker("GET", "/containers/%s/json" % CONTAINER)
    repo, tag = split_ref(c["Config"]["Image"])
    if not same_repo(repo):
        raise RuntimeError("container %s runs %s, not %s" % (CONTAINER, c["Config"]["Image"], IMAGE))
    if tag != "latest":
        tag = version              # a container pinned to a version moves to the new one
    ref = "%s:%s" % (repo, tag)

    status("pulling", "pulling %s" % ref, version=version)
    pull(repo, tag)
    new = docker("GET", "/images/%s/json" % urllib.parse.quote(ref, safe=""))
    got = ((new.get("Config") or {}).get("Labels") or {}).get("org.opencontainers.image.version", "")
    if got.lstrip("v") != version:
        raise RuntimeError("%s is version %s, not %s (the release's image may still be building: "
                           "try again in a few minutes)" % (ref, got or "unknown", version))
    if new["Id"] == c["Image"]:
        status("done", "already on %s" % version, version=version)
        return
    old_image = docker("GET", "/images/%s/json" % c["Image"])
    body = new_config(c, old_image, ref)

    name = c["Name"].lstrip("/")
    keep = "%s-before-%s" % (name, version)
    status("restarting", "stopping %s; it comes back on %s" % (name, version), version=version)
    try:
        docker("DELETE", "/containers/%s" % keep, force="true")    # left from an earlier try
    except DockerError as e:
        if e.code != 404:
            raise
    docker("POST", "/containers/%s/stop" % c["Id"], timeout=120, t="30")
    docker("POST", "/containers/%s/rename" % c["Id"], name=keep)
    created = None
    try:
        created = docker("POST", "/containers/create", body, name=name)["Id"]
        docker("POST", "/containers/%s/start" % created)
        status("starting", "waiting for %s to come up" % name, version=version)
        ok, why = wait_healthy(created)
        if not ok:
            raise RuntimeError("the new container did not come up: %s" % why)
    except Exception as e:
        log("rolling back: %s" % e)
        if created:
            try:
                docker("DELETE", "/containers/%s" % created, force="true")
            except DockerError as e2:
                log("could not remove the new container: %s" % e2)
        docker("POST", "/containers/%s/rename" % c["Id"], name=name)
        docker("POST", "/containers/%s/start" % c["Id"])
        raise RuntimeError("%s; the old version is back" % e)
    docker("DELETE", "/containers/%s" % c["Id"], force="true")
    try:
        docker("DELETE", "/images/%s" % c["Image"])               # the old image, when nothing else uses it
    except DockerError:
        pass
    status("done", "updated to %s" % version, version=version)


def main():
    if not os.path.exists(SOCKET):
        sys.exit("no Docker socket at %s: mount /var/run/docker.sock into this container" % SOCKET)
    log("updating container %s (%s) when the panel asks" % (CONTAINER, IMAGE))
    while True:
        try:
            write("updater.json", {"kind": "docker", "at": now(), "container": CONTAINER})
            req = take_request()
        except OSError as e:
            log("cannot use %s: %s" % (UPDATE, e))
            req = None
        if req is not None:
            version = str(req.get("version") or "").lstrip("v")
            STATUS.clear()
            try:
                if not VERSION_RE.match(version):
                    raise RuntimeError("not a version: %r" % version)
                status("queued", "updating to %s" % version, version=version, started=now(), by=req.get("by"))
                update(version)
            except Exception as e:
                status("failed", str(e), version=version)
        time.sleep(BEAT)


if __name__ == "__main__":
    main()
