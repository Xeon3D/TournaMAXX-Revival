# syntax=docker/dockerfile:1
# TournaMAXX-Revival: the server and its web control panel in one image.
#
#   docker run -d --name tournamaxx -v tournamaxx:/data \
#       -p 8080:8080 -p 2323:2323 -p 15000:15000 -p 17751:17751 \
#       -e TMX_ADMIN_PASSWORD=... xeon3d/tournamaxx-revival
#
# The panel (http://host:8080/) runs the server and restarts it if it stops.
# Everything the server keeps (state, log, fetched files, packages, backups)
# and the panel's config live in /data.  Put the panel behind HTTPS
# (TMX_SECURE_COOKIES=true) before exposing it to the internet.
#
# No RUN steps: the image builds for any platform python:slim has without
# emulation.

FROM python:3.13-slim

LABEL org.opencontainers.image.title="TournaMAXX-Revival" \
      org.opencontainers.image.description="A TournaMAXX server for Merit Megatouch MAXX cabinets, with a web control panel" \
      org.opencontainers.image.source="https://github.com/Xeon3D/TournaMAXX-Revival" \
      org.opencontainers.image.licenses="GPL-3.0-only"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMX_DATA=/data

# Read-only code, whatever the permissions of the checkout it is built from.
COPY --chmod=u=rwX,go=rX modem-server.py mkupdate.py LICENSE README.md /app/
COPY --chmod=u=rwX,go=rX panel/ /app/panel/
COPY --chmod=u=rwX,go=rX docs/ /app/docs/
COPY --chmod=u=rwX,go=rX docker/entrypoint.py /app/docker/entrypoint.py
# /data, owned by the user the image runs as.
COPY --chown=1000:1000 --chmod=u=rwX,go=rX docker/data/ /data/

USER 1000:1000
WORKDIR /data
VOLUME /data

# 8080 the control panel; 2323 the emulators' modem calls; 15000 and 17751
# TournaMAXX straight over TCP.
EXPOSE 8080 2323 15000 17751

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python3", "-c", "import urllib.request as u, urllib.error as e\ntry: u.urlopen('http://127.0.0.1:8080/api/session', timeout=4)\nexcept e.HTTPError as x: raise SystemExit(x.code != 401)"]

ENTRYPOINT ["python3", "/app/docker/entrypoint.py"]
