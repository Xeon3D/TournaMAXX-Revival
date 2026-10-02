# Running it on ZimaOS (Docker) behind Nginx Proxy Manager

One container holds the TournaMAXX server, the Mega-Link switch and the
control panel. Nginx Proxy Manager (NPM) gives the panel your domain and
HTTPS; the two game ports don't go through NPM at all, since they aren't
HTTP.

| Port | What | Reaches the container through |
|---|---|---|
| 8080 (host 8480) | the control panel, and the public page `/megalink` | NPM (HTTPS) |
| 2323/tcp | the emulators' modem calls | your router, forwarded straight to the ZimaOS host |
| 8086/udp | the Mega-Link switch | your router, forwarded straight to the ZimaOS host |
| 15000, 17751/tcp | TournaMAXX without a modem | not needed over the internet (see below) |

## 1. The container

In a terminal on ZimaOS (Settings > terminal, or SSH):

    sudo mkdir -p /DATA/AppData/tournamaxx
    sudo chown 1000:1000 /DATA/AppData/tournamaxx

The container runs as UID 1000 and keeps everything in that folder: the
state, the log, backups, packages, and the panel's users.

Then in the ZimaOS dashboard: **App Store > Custom Install > Import**, and
paste [deploy/docker-compose.yml](../deploy/docker-compose.yml). It publishes
the panel on host port **8480** (ZimaOS's own dashboard takes 80, and 8080 is
often taken too); change the left-hand number if 8480 is in use.

Open `http://<zima-ip>:8480/` **within 15 minutes** of the container
starting and make the first user. If you miss that, restart the container
and it opens again. (Or set `TMX_ADMIN_PASSWORD` in the compose file before
the first start, and log in as `admin`.) More users: **Settings > Users**.

## 2. Nginx Proxy Manager

**Hosts > Proxy Hosts > Add Proxy Host**:

- Domain Names: your domain, e.g. `tournamaxx.example.com` (its DNS pointing
  at your public address, as for your other NPM hosts)
- Scheme `http`, Forward Hostname / IP: the ZimaOS host's LAN address,
  Forward Port `8480` (if NPM runs on the same ZimaOS box, the LAN address
  still works; `localhost` would be NPM's own container)
- Block Common Exploits: on. Websockets: not needed.
- SSL: request a Let's Encrypt certificate, **Force SSL** and **HTTP/2** on.

The panel then lives at `https://tournamaxx.example.com/`, and the public
Mega-Link page at `https://tournamaxx.example.com/megalink` once it is
switched on (panel: **Mega-Link > Public page**). NPM sends
`X-Forwarded-Proto`, which makes the panel mark its cookie Secure, and
`X-Real-IP`, the browser's address, which the panel's login throttling
(5 wrong passwords per user name and address, 20 per address, then a
five-minute wait) goes by.

The panel only believes `X-Real-IP` from a proxy it trusts, or anyone could
send a new address with each password guess. Tell it where NPM connects
from with `TMX_TRUSTED_PROXIES` in the compose file (comma-separated
addresses or networks; it is read at every start), and recreate the
container:

- NPM on the same ZimaOS box reaches the panel through Docker, from a
  Docker network address (NPM's container, or the network's gateway, such
  as `172.17.0.1`). `172.16.0.0/12` covers Docker's usual networks;
- NPM on another machine: that machine's LAN address, e.g. `192.168.1.10`.

To see the exact address, log in once through NPM and look at the
container's log (`docker logs tournamaxx`, or the app's logs in ZimaOS): it
says `X-Real-IP from 172.18.0.5 ignored: not a trusted proxy`. Trust only
the proxy, not your whole LAN: any machine in a trusted range can send
`X-Real-IP`. Without `TMX_TRUSTED_PROXIES` the panel still works, but
counts every login through NPM as coming from NPM's address.

### Keeping the panel private, the Mega-Link page public (optional)

The panel asks for a login, but you may rather not have it on the internet
at all. Make two proxy hosts to the same `http://<zima-ip>:8480`:

- `tournamaxx.example.com` with an **Access List** that only lets in your LAN
  (Access Lists > Add: Allow `192.168.0.0/16`, or your own range, then
  Deny all; pick it on the proxy host);
- `megalink.example.com`, public, with this under **Advanced > Custom
  Nginx Configuration**, so that it serves only the public page:

      location = / { return 302 /megalink; }
      location ~ ^/(megalink|megalink\.js|style\.css|api/public/megalink)$ {
          proxy_pass http://<zima-ip>:8480;
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-Proto $scheme;
      }
      location ~ ^/ { return 404; }

## 3. Your router

Forward to the ZimaOS host's LAN address:

- **UDP 8086** for Mega-Link (players' emulators send to it; replies go back
  on the same path, so the players open nothing on their side);
- **TCP 2323** if emulators outside your network should make TournaMAXX
  calls (MegaPPBox: *Modem settings*, dial out to `tournamaxx.example.com`,
  port 2323).

Don't forward these through NPM: its "Streams" can carry TCP and UDP, but the
switch would then see every player as NPM's address, and the panel couldn't
tell the cabinets apart. A plain port forward keeps their own addresses.

TCP 15000 and 17751 are for cabinets that reach the server on a real network
with `us.accessmerit.com` resolved to it (a hosts entry, a local DNS
override); nobody on the internet reaches them that way, so leave them
unforwarded.

## 4. Mega-Link

In the panel, **Mega-Link**: add rooms (a name and a secret each; a room
with no secret takes cabinets whose card has none), tick **Public** for the
ones to list, and switch on the public page. The page shows each public
room's secret, how many cabinets are in it, and the steps to connect:
MegaPPBox's Network settings, card type **Remote Switch**, host
`tournamaxx.example.com:8086`, and the room's secret.

The switch is on UDP 8086 by default (`TMX_SWITCH_PORT`, or **Settings >
Server ports**); the panel's Mega-Link page shows who is in each room, their
cabinets' IP and card addresses, and warns when two cabinets use the same IP
address.

## Updating

The compose file has a second service, `updater` (container
`tournamaxx-updater`). With it, the panel's **Settings > Updates** shows an
**Update** button when a new release is out: the updater pulls the new image,
recreates the `tournamaxx` container with the same settings, and puts the old
one back if the new one does not come up healthy. It has the Docker socket
(which is as good as root on the ZimaOS box), so it runs apart from the panel
and only ever pulls `xeon3d/tournamaxx-revival`, taking nothing but a version
number from the panel. Leave the service out if you would rather not give it
that, and update by hand.

By hand: pull the new image and recreate the containers (ZimaOS: the app's
settings > update, or `docker compose pull && docker compose up -d`). That
also updates the updater itself, which the button does not.

Either way, everything in `/DATA/AppData/tournamaxx` stays.

An install from before the updater (0.1.0) has no button yet: import the
new compose file (or add its `updater` service), and update by hand once.
