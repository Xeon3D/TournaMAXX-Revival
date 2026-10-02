# TournaMAXX-Revival

A TournaMAXX server for Merit Megatouch MAXX cabinets (the DOS releases), so
their on-line features work again: tournaments, player registration,
national / regional / local rankings, location info, and the operator side
(messages, files, settings, prices, counters and software updates).

It is meant for cabinets running in an emulator whose modem can dial a TCP
host, such as [MegaPPBox](https://github.com/Xeon3D/MegaPPBox): the cabinet
dials, runs PPP with its own TCP/IP stack, looks up Merit's server and makes
its update call, and this answers all of it.

| Release | Login protocol | Status |
|---|---|---|
| Emerald 2 V9.00 / V9.01 | 9 | works |
| Emerald V8.04 | 7 | works (tournament dates need the date fix) |
| Double Diamond V7.01 | 6 | works (date fix) |
| Diamond V6.03 | 3 | works (date fix) |

## Running the server

Python 3, nothing else:

    python modem-server.py --port 2323 --log modem-server.log --state modem-server-state.json

In MegaPPBox: *Tools > Modem on COM2*, then *Tools > Modem settings...*,
"Dial out to a TCP/IP host", host 127.0.0.1, port 2323. On the cabinet, set
a machine serial and dial up (*Initial Connection*, then *Update from
server*).

Everything the server knows lives in the state file: tournaments, players,
scores, each cabinet's location, the operator's outbox and update packages.
It can be edited while the server runs; each call reads it again. The
protocol, the state file and the outbox are described in
[docs/tournamaxx.md](docs/tournamaxx.md).

More options: `--tcp-ports 15000,17751` also takes TournaMAXX straight
over TCP, for cabinets that reach the server on a real network (whose
`us.accessmerit.com` points at it) instead of through a modem;
`--switch-port 8086` runs the Mega-Link switch (below); `--dns-port 53`
answers cabinets on a network card that look up `us.accessmerit.com` (Jade 2
and the other Linux releases on broadband: in MegaPPBox's SLiRP network, give
the cabinet DNS 10.0.2.2); and `--admin-port 2324` takes the control panel's
requests on 127.0.0.1.

## Mega-Link over the internet

MegaPPBox's **Remote Switch** network type sends a cabinet's Ethernet frames
over UDP to a switch, so that cabinets in different places can play
head-to-head and linked games. `megalink_switch.py` is that switch: the
server runs it with `--switch-port 8086`, and the control panel's
**Mega-Link** page sets its rooms (a name and a shared secret each), shows
who is in them, and can switch on a public page, `/megalink`, that lists the
public rooms, how full they are, and how to connect. Players need no port
forwarding; the server needs UDP 8086 open. See
[docs/tournamaxx.md](docs/tournamaxx.md#mega-link-over-the-internet).

## The control panel: `panel/`

A web page to run the server: start, stop and restart it, follow its log,
and manage everything in the state file -- tournaments (and their
standings), cabinets (location, operator settings, prices, dial-up
settings, messages, file transfers, reboots, counters, the reports they
send), players, and update packages (upload a `NETUPDT.EXE`, or a .zip laid
out as `C:\` that it builds with `mkupdate.py`). Python 3 again, nothing
else. While the server runs, its edits go through the admin port, so they
and the calls take turns.

To try it on your own machine (it runs the server itself):

    python panel/panel.py --config panel.json

then open http://127.0.0.1:8080/ and make the first user (a new panel asks
for one during its first 15 minutes; `--set-password NAME` makes one from
the command line instead). More users: **Settings > Users**. The config
(made on first run) sets the ports; see the top of `panel/panel.py`.

## Docker

The server and the panel in one image, for amd64 and arm64:

    docker run -d --name tournamaxx --restart unless-stopped \
        -v tournamaxx:/data \
        -p 8080:8080 -p 2323:2323 -p 15000:15000 -p 17751:17751 -p 8086:8086/udp \
        xeon3d/tournamaxx-revival

The panel is on port 8080; it runs the server and starts it again if it
stops. Open it within 15 minutes of the first start to make its first user
(or set `TMX_ADMIN_PASSWORD`, and log in as `admin`). Everything lives in the
`/data` volume. On the first start `TMX_PORT` (default 2323),
`TMX_TCP_PORTS` (default `15000,17751`) and `TMX_SWITCH_PORT` (default 8086,
UDP) set the ports; after that, change them in the panel and publish the
same ports. Put the panel behind HTTPS before exposing it; behind a proxy
that sends `X-Forwarded-Proto` its cookie is marked Secure by itself.

Wrong passwords are throttled (5 per user name and address, 20 per address,
then five minutes). The panel takes the browser's address from a proxy's
`X-Real-IP` only when the proxy connects from loopback (nginx on the same
host, as in `deploy/nginx-tournamaxx.conf`) or from an address or network
in the config's `"trusted_proxies"` (Docker: `TMX_TRUSTED_PROXIES`, e.g.
`172.16.0.0/12` for a proxy in another container on the same host); an
`X-Real-IP` from anywhere else is ignored, and its sender's address printed
in the panel's log once.

On ZimaOS, or any Docker host behind Nginx Proxy Manager: see
[docs/zimaos.md](docs/zimaos.md) and
[deploy/docker-compose.yml](deploy/docker-compose.yml).

To build it yourself: `docker build -t tournamaxx-revival .`

Each GitHub release publishes the image
(`.github/workflows/docker.yml`): a release tagged `v1.2.3` is pushed as
`1.2.3`, `1.2` and `latest`.

## On a server: `deploy/`

`deploy/install.sh` sets it all up on Debian or Ubuntu, as root, from a copy
of this repository: the server and the panel as systemd services under
their own user, nginx in front of the panel with a Let's Encrypt
certificate, log rotation, and a sudo rule that lets the panel start and
stop the server (and nothing else); the Mega-Link switch runs on UDP 8086:

    sudo sh deploy/install.sh --domain us.accessmerit.com --email you@example.com

It asks for the panel's admin password. Run it again to update; the data in
`/var/lib/tournamaxx` stays. The machine needs TCP 80 and 443 (the panel),
2323 (the emulators' modem calls), UDP 8086 (Mega-Link) and, for direct
connections, 15000 and 17751.

## Update packages: `mkupdate.py`

Makes a `NETUPDT.EXE` the way Merit shipped network updates: a PKZIP 2.04g
self-extractor that the cabinet's `TEST.BAT` runs at boot as
`NetUpdt -d -o c:\`. Point it at a folder laid out as the cabinet's `C:\`:

    python mkupdate.py <folder> --protocol 7 --result C:\RESULT.TXT

or run it without arguments for a window. It prints the entry to add under
`"updates"` in the server's state file. `mkupdate.spec` builds a one-file
Windows `mkupdate.exe` with PyInstaller. It needs nothing else: the PKSFX
stub (PKWARE's code) is carried in the script.

## The tournament date fix: `datefix/`

Emerald V8.04, Double Diamond V7.01 and Diamond V6.03 store tournament times
in a field that stopped fitting on 21 August 2002, so with a present-day
clock every tournament shows as ended. `datefix/tmfix.py` patches the game's
`MERIT2\EXEC\MEGACDLL.EXE` to store minutes instead (as Emerald 2 does), and
changes its version to x.20 (V8.20, V7.20, V6.20). See
[datefix/README.md](datefix/README.md) for making the update packages that
install it from the server.

No game files are included here: the fix is made from your own copy.

## What's here

| Path | What |
|---|---|
| `modem-server.py` | the server (Python 3, standard library only) |
| `megalink_switch.py` | the Mega-Link switch for MegaPPBox's Remote Switch |
| `panel/` | the web control panel (`panel.py` and its page) |
| `deploy/` | `install.sh`, the systemd units, nginx and logrotate config, `docker-compose.yml` |
| `Dockerfile`, `docker/` | the Docker image and its entrypoint |
| `mkupdate.py`, `mkupdate.spec` | the update-package maker and its PyInstaller spec |
| `datefix/` | the tournament date fix: `tmfix.py` (needs `capstone`), its LE loader `lefile.py`, Emerald V8.04's installer `NETUPDT-V804.BAT` |
| `tools/dbfcrypt.py` | reads the cabinet's encrypted `D:\Database\*.dbf` files |
| `tools/unpack_dstart.py` | unpacks a Linux release's game program (`/usr/local/bin/start`) |
| `docs/tournamaxx.md` | the protocol, the state file, the older releases, Mega-Link |
| `docs/zimaos.md` | running it on ZimaOS / Docker behind Nginx Proxy Manager |

## Licence

GPL-2.0, like MegaPPBox; see [LICENSE](LICENSE).

The PKSFX 2.04g self-extractor stub carried in `mkupdate.py` (and so in every
package it makes) is PKWARE's code, not covered by the GPL. PKWARE's PKZIP
2.04g licence asks for a distribution licence for self-extracting files made
with ZIP2EXE.
