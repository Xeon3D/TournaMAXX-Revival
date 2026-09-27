# TournaMAXX protocol (DOS Megatouch MAXX)

Worked out from `MERIT2\EXEC\MEGACDLL.EXE` of MAXX Emerald 2 V9.01 (PG3002,
SHA-256 `1e490ccd…2b4c`), decompiled with Ghidra, and from calls the game
made to `modem-server.py`. Addresses are that executable's (LE object 1
at 0x10000, fixups applied) unless another release is named. The older
DOS releases speak the same protocol with fewer commands; see
[Older releases](#older-releases).

| Release | Login protocol | Login size | Server status |
|---|---|---|---|
| Emerald 2 V9.00 / V9.01 | 9 | 166 bytes | works |
| Emerald V8.04 | 7 | 146 bytes | works |
| Double Diamond V7.01 | 6 | 81 bytes | works |
| Diamond V6.03 | 3 | 81 bytes | works |

What is marked *verified* has been seen on the wire from the running game;
the rest is read from the code.

## Getting there

1. The cabinet dials and runs PPP with its own TCP/IP stack (an InterNiche-
   style stack inside the executable). ISP login `MERIT800@B-2000.NET` /
   `CONSERVATION`; the dial settings in `C:\DIALINFO.*` are obfuscated, the
   cabinet sends them decoded in its login. *Verified.*
2. It resolves **`us.accessmerit.com`** (through 12.127.16.67). *Verified.*
3. It opens TCP to that address, on one of two ports: *verified*
   - **15000**, from *Initial Connection* (Dial-Up Network screen): login
     only; the databases are not open, and a data command makes the cabinet
     drop the line;
   - **17751** (0x4557), from *Update from Server* and the daily update at
     the hour ticked in the UPDATE grid: the full session. The session
     object's mode field (+0x39) is this port number, and only at 0x4557 are
     the tournament, player, score and location databases opened (0xa1dd4).
4. The game's log (`C:\DEBUG.DAT`) calls this the *TournaMAXX Server*.

Emerald 2 can also reach it over Ethernet (`ETHERNET\`, "Ethernet IP Address
set to …").

## Framing

Every message, both ways, is

    u16 type   u16 length   body[length - 4]

little-endian, `length` counting the 4-byte header. The cabinet peeks the
4-byte header, then reads `length` bytes. *Verified.*

## Who talks

The **server** drives. It sends an odd-numbered command; the cabinet runs its
handler and answers with the next number up. Emerald 2's dispatcher (switch
at 0x9ced0) knows the 24 types below; anything else makes it send 0xFF02 and
log "SERVER ERROR REPORTED". Older releases know fewer, and the server sends
each only what its protocol version has (see [Older releases](#older-releases)).

| Server sends | Handler | Cabinet answers | What it is |
|---|---|---|---|
| 0x0011 | 0x9d014 | 0x0012 | hello → login. *Verified.* |
| 0x0021 | 0x9d668 | 0x0022 (empty) | a tournament. *Verified.* |
| 0x0041 | 0x9d338 | 0x0042 | "which tournaments do you hold?" *Verified.* |
| 0x0051 | 0x9db94 | 0x0052 | new players: empty = "next one?", with a body = "here is its permanent ID". *Verified.* |
| 0x0067 | 0x9e98c | 0x0068 | score upload, one batch per request. *Verified.* |
| 0x0073 | 0x9ea94 | 0x0074 (empty) | rankings. *Verified.* |
| 0x0081 | 0x9e6bc | 0x0082 (empty) | players (with their locations). *Verified.* |
| 0x00B1 | 0x9f9a8 | none | the cabinet's location (LOCATION INFO screen) and dial-up options, into `C:\NTNVRAM.DAT`. *Verified.* |
| 0x00C1 | 0x9fab8 | 0x00C2, 0x00C3 | game statistics, two periods. *Verified.* |
| 0x00C9 | 0xa0268 | 0x00CA | event counters (cleared once sent). *Verified.* |
| 0x00D1 | 0xa2ccc | 0x00D2 (empty) | locations. *Verified.* |
| 0x00E1 | 0xa3034 | 0x00E2 | the last 14 calls. *Verified.* |
| 0x0101, 0x0103 | 0x9f2c4, 0x9f460 | 0x0102 / 0x0104 | the server fetches a file |
| 0x0111, 0x0112 | 0x9f59c, 0x9f6d0 | 0x0113 / 0x0114 | the server sends a file |
| 0x0121 | 0x9f8a4 | 0x0122 | delete a file, and/or reboot after the call. *Verified.* |
| 0x0201 | 0xa07fc | 0x0202 | operator settings: read, or set then read. *Verified.* |
| 0x0211 | 0xa0d9c | 0x0212 | per-game settings: read, or set then read. *Verified (read).* |
| 0x0221 | 0xa0ed8 | 0x0222 | dial-up settings: read, or set then read. *Verified.* |
| 0x0A01 | 0xa2c20 | none | the ISP login and password. *Verified.* |
| 0x0A11 | 0xa2c6c | none | the site's registration page, shown during Initial Connection. *Verified.* |
| 0xFF01 | | 0xFF02 (empty) | status line; `COMPLETE.` ends the session. *Verified.* |
| 0xFF11 | 0x9d2c8 | 0xFF02 | acknowledgment only |

Status lines the cabinet knows: `COMPLETE.`, `SERVER ERROR.`,
`INVALID MACHINE SERIAL NUMBER.`, `NO MACHINE SERIAL NUMBER.`,
`INVALID KEY.`, `COMMUNICATIONS VERSION MISMATCH.`

### An update session

What `modem-server.py` does, and the cabinet accepts: *verified*

    0011 hello            → 0012 login
    0021 tournament  (×n) → 0022
    0041                  → 0042 tournaments held
    0051             (×n) → 0052 a new player → 0051 with its permanent ID …
                          → 0052 empty (this also readies the first score batch)
    0067             (×n) → 0068 scores … → 0068 empty
    00D1 locations   (×n) → 00D2   the home cabinets of the players below,
                                   where this cabinet lacks them or they changed
    0081 players     (×n) → 0082   every player this cabinet has not been
                                   sent yet, its own included, 20 a message
    0073 rankings    (×n) → 0074   (Diamond V6.03: 0071 → 0072)
    00B1 location               (no answer)
    0021 finals      (×n) → 0022   tournaments that have ended: STATUS 4
                                   (after their final rankings), later 5;
                                   only to cabinets that had the tournament
    outbox, update packages        whatever is queued for this cabinet
    0201, 0211, 0221, 00C1, 00E1   reports (read only; those the client has)
    FF01 "COMPLETE."      → FF02, and the cabinet hangs up

The order matters: 0x0067 before the empty 0x0052 makes the cabinet send an
unprepared buffer (length 0xAAAA and whatever follows it in memory). Players
must be sent before the rankings that name them, and locations before the
players that carry them.

A call that breaks off before COMPLETE is rolled back by the cabinet (it
restores its databases from the `.bak` copies at the next boot), so the
server counts what a call delivered (players, locations, tournaments,
finals, removals) as delivered only once 0xFF02 arrives.

## Messages

### 0x0012 login (cabinet). *Verified.*

166 bytes at protocol 9, 146 at 7:

    +04 u16   protocol version (Emerald 2: 9; Emerald V8.04: 7)
    +06 char  machine serial ("1234567")
    +16 char  "supersecretpasswordthing"
    …         key data, "10d62a81", the cabinet's clock, the access number,
              the ISP login and password, the game's key id ("SA304801 R01")

81 bytes at protocols 6 (Double Diamond V7.01) and 3 (Diamond V6.03), which
also carry the game's version, scanned from its version text with
`"PG3002 V%d.%d "` (so V7.01 is 7 and 1):

    +04 u16   protocol version (6 or 3)
    +06 char  machine serial
    +3B u32   version, major
    +3F u32   version, minor

### 0x0021 tournament (server), 502 bytes. *Verified.*

Written into `D:\Database\tourney.dbf`.

    +004 u32  ID                  score file: D:\Database\SC<ID:06>.dbf
    +008 u32  GAME                the launcher's game number (0x55dc0):
                                  48 Wild 8, 13 Zip 21, 15 Quick Match, …
    +00C u32  STATUS              1 announced, 2 running, 3 ended (the
                                  cabinet moves 1→2 at START and 2→3 at END,
                                  0xa419c), 4 final, 5 remove. 4 and 5 only
                                  update a tournament the cabinet has: 4
                                  moves its rows from Scores.dbf into
                                  SC<ID>.dbf, which the results read, so the
                                  final rankings go first; 5 deletes it and
                                  its SC file. 2 and 4 verified.
    +010 i32  START               seconds from now
    +014 i32  END                 seconds from now
    +018 u32  CREDITS             cost to play
    +01C u32  GAMEOPTS
    +020 char NAME[51]            shown as the EVENT
    +053 char DESC[101]
    +0B8 u32  RANDSEED            every cabinet deals the same
    +0BC u32  SEEDINC
    +0C0 char GROUP1..3[51] each
    +159 char PRIZE1..3[51] each
    +1F2 i32  SHOWDATE            seconds from now (Diamond V6.03 has no
                                  SHOWDATE)

START and END are relative to the cabinet's own clock: it adds its time to
them. How the cabinet then stores them is where Emerald V8.04 and older
break (see [the date limit](#the-2002-date-limit)).

### 0x0042 tournaments held (cabinet), 142 bytes. *Verified.*

    +04 u32   (session state)
    +08 u32   (session state)
    +0C char  LASTUPDT[9]    YYYYMMDD of the last tournament update, or "00000000"
    +15 u8    count (up to 10)
    +16 u32   ID[10]
    +3E u32   TOTCREDITS[10]
    +66 u32   TOTPLAYS[10]

### 0x0052 new player (cabinet), 374 bytes. *Verified.*

    +04 u32   TEMPID (the cabinet's own, e.g. 9999999)
    +08 u32
    +0C char  HANDLE[13]
    +19 char  PIN[5]
    +1E char  CITY[31]
    +3D char  STATE[36]
    +61 …     DATEADDED, LASTUPDATE, BIRTHDAY (YYYYMMDD), FIRSTNAME,
              LASTNAME, ADDRESS, POSTALCODE, COUNTRY, PHONE, EMAIL, …

The server answers with 0x0051 carrying the same record, the permanent player
ID at +04; the cabinet renumbers the player, and their scores, to it.
*Verified.* An empty 0x0052 means no new players are left.

### 0x0068 score upload (cabinet). *Verified.*

Up to 38 entries of 52 bytes, only records marked DIRTY; an empty 0x0068
(length 4) means nothing is left. A cabinet uploads only the scores played
since it was last sent rankings.

    +00 u32   TOURNID
    +04 u32   PLAYERID
    +08 u32   NEWPLAYS
    +0C u32   the new scores (up to five, best first)
    +20 u32   their dates (time_t)

### 0x0073 rankings (server). *Verified.*

The cabinet answers 0x0074 at once. The rankings replace the cabinet's own
score table. Up to 60 entries of 34 bytes:

    +00 u16   group: 0 National, 1 Regional, 2 Local
    +02 u32   TOURNID
    +06 u32   PLAYERID
    +0A u32   RANK
    +0E u32   the player's five scores

The ranking screens show the sum of the five divided by five. The handle,
city and state come from the cabinet's own player database, and the location
name from its location database, so the players (0x0081) and their
locations (0x00D1) have to be sent first. A player it does not know shows as
"PLAYER" (V8.04: 0x9cea4).

### 0x0071 rankings, older form (server). *Verified (Diamond V6.03).*

The only rankings message Diamond V6.03 has; Emerald V8.04 and Double
Diamond V7.01 have both. The cabinet answers 0x0072 at once. Up to 20
entries of 92 bytes, one per player and tournament, covering the three
groups (V8.04 0x9c270, Diamond 0x9239c):

    +00 u32   TOURNID
    +04 u32   PLAYERID
    +08 u32   total[3]      the sum of the five scores, per group
    +14 u32   rank[3]       per group; -1: not ranked in that group
    +20 u32   score[3][5]   per group

Where a player or their location is unknown it shows "Player", "Network",
"Earth" and "Unknown".

### 0x0081 players (server). *Verified.*

Entries of 97 bytes, at most 20 a message; the cabinet answers 0x0082. The
server sends every player a cabinet has not been sent yet, the cabinet's own
included, so that each has a location there.

    +00 u32   player ID (0: skipped)
    +04 u32
    +08 char  HANDLE[13]
    +15 char  PIN[5]
    +1A char  CITY[31]
    +39 char  STATE[36]
    +5D u32   LOCATION       the location ID (0x00D1) of the player's home
                             cabinet; the server uses that cabinet's serial

Bytes +04 to +5C are the player's record as their cabinet sent it in 0x0052.
An empty HANDLE or PIN deletes the player; a player the cabinet already has
is updated.

### 0x00D1 locations (server)

Entries of 154 bytes, into `location.dbf`; the cabinet answers 0x00D2.
*Verified.* The server sends one for each cabinet whose players it sends:
ID the cabinet's serial, the rest from that cabinet's `locations` entry in
the state file (`city_state` split at the comma).

    +00 u32   ID
    +04 char  NAME[52]     (empty: the location is deleted)
    +38 char  CITY[31]
    +57 char  STATE[36]
    +7B char  COUNTRY[31]

### 0x00B1 location and dial-up options (server, no answer)

Written into `C:\NTNVRAM.DAT`, which SETUP.DLL's LOCATION INFO screen reads
(an empty string shows as `? ? ? ? ?`):

    +04 u8[13] options; only values below 3 are stored, so 0xFF leaves one
               as it is
    +11 char   NAME[51]
    +44 char   CITY STATE[51]
    +77 char   COUNTRY[51]
    +AA char   TELEPHONE #[51]

### 0x0A11 registration page (server, no answer). *Verified.*

The cabinet writes 1500 bytes of the body to `D:\Database\Operate.txt`.
SETUP.DLL (0x19d24) reads it as up to 15 lines of 100 bytes, stopping at the
first empty one, and after an Initial Connection shows them under "IS THE
FOLLOWING INFORMATION CORRECT?" with YES / NO: the registration Merit holds
for the site, for the operator to confirm. Send it on port 15000, before
COMPLETE.

### 0x0121 delete / reboot (server)

    +04 u8    reboot at the end of the call ("TournaMAXX Server: Reboot
              Requested" in DEBUG.DAT)
    +05 char  a path to delete (empty: none); logged in XFERLOG.TXT

With the file transfers below, this is how a cabinet's software is updated.

### File transfer

The first transfer of a session closes the databases and deletes their `.bak`
copies, so a database file can be replaced whole. Finished transfers are
logged in `C:\MERIT2\N_TOURN\USER\XFERLOG.TXT`.

The server fetches a file:

    0101  +05 path                      → 0104 (cannot open)  or
                                        → 0102 first chunk
    0102  (cabinet, 1996 bytes) +04 offset, +08 file size, +0C bytes in this
          chunk, +10 data
    0103                                → 0102 next chunk; 0 bytes: the end

The server sends a file:

    0111  +05 destination path          → 0113 ready (C:\FTPTEMP.DAT opened) / 0114
    0112  +08 file size, +0C bytes in this chunk, +10 data
                                        → 0113; a chunk of 0 bytes ends it: the
                                          temporary file is renamed to the
                                          destination (0114 if that fails)

### Settings

An empty 0x0201 / 0x0211 / 0x0221 reads a block; with a body it is written
first, then read back. *Reading verified.*

- **0x0202** (285 bytes): the system settings. They live in the exported
  structure `_NVRAMDATA` (0x17a050, 32 KB, saved as `C:\NVRAM.DAT`); the
  second column is the offset in it, which is also the offset in
  NVRAM.DAT. Labels are SETUP.DLL's buttons for the field.

      +04 u8   0x3d7   adult (Strip) games: 0 off, 1 on, 2 on between the
                       two hours below
      +05 u8   0x3ed   nudity allowed
      +06 u8   0x3ee   TOPLESS (0) / FULLNUDE (1)
      +07 u8   0x1167  adult games on from hour (bits 0–4)  SXONUP/SXONDN
      +08 u8   0x1167  adult games off at hour (bits 5–9)   SXOFFUP/SXOFFDN
      +09 u8   0x3e2   adult attract screens (0: the "mini" attract loop)
      +0A u8   —       volume, a mixer level 0–127; the operator menu
                       shows it as a percentage of 127, rounded down
      +0B u8   0x3dc   6 Star on (shows SET 6 STAR PIN)
      +0C u8   0x3e9   6 Star opens HIGH SCORES
      +0D u8   0x3ea   6 Star opens VIDEO BILLBOARD
      +0E u8   0x3eb   6 Star opens VOLUME CONTROL
      +0F u8   0x3f4   6 Star opens SCREEN CALIBRATION
      +10 u8   0x401   6 Star opens UPDATE FROM SERVER
      +11 u16  0x389   the 6 Star PIN: the order in which the six stars are
                       touched, as a 4-digit number of star numbers 1–6
                       (default 4123)

  *6 Star* is the staff shortcut: from the info button of the main menu,
  touching the six stars in the programmed order opens the screens switched
  on above (the manuals' "DIP switch set to YES for each screen"). It is
  not the Security PIN: that one (4–8 digits, a string at
  `_NVRAMDATA`+0x116b, guarding the operator menus when bit 0x40 of
  +0x1169 is set) is not carried by the protocol.
      +13 u8   0x3e6   adult content (lifts the high-score name filter,
                       BADNAME.DAT; gates the AC setting)
      +14 u8   0x1167  AC level 1–4 (bits 10–12)             ACUP/ACDN
      +15 …            (unset in the reply)
      +BD u8[96] 0x3cf the raw block _NVRAMDATA[0x3cf:0x42f], which
                       holds all of the above and a few more

  In a 0x0201 each field is only written when in range (+04 < 3; flags < 2;
  hours < 24; +0A < 0x80; +11 < 10000; +14 < 5), and +05…+09 only when +04
  is set, +0C…+10 only when +0B is set, +14 only when +13 is 1. From +15 a
  0x0201 carries **high-score clears**: pairs (game, category) up to game
  0x54 — the category matters for the trivia games (3 and 33, seven
  categories each; 0xFF: all); a first byte from 0x54 up clears every
  game's high scores; 0xFF clears none. (MEGACDLL 0x1bda0: ten 19-byte
  entries per game.) *Verified: the volume, the adult-game mode and hours, high-score clears,
  the 6 Star fields as stored.*

- **0x0212** (172 bytes): the **price of each game**, 84 pairs (game number,
  credits per play 0–15; 0: not offered). *Verified: Wild 8 set to 3CR.* The low 4 bits of the game's
  record (below); shown as "%dCR". While a tournament runs its CREDITS
  replace the game's price. A 0x0211 carries the same pairs.

- **The per-game record**, 36 bytes at `_NVRAMDATA`+0x42f + game × 0x24:

      +00 u8   flags (bits 0–6: offered / in a menu)
      +01 u16  low 4 bits: price in credits; upper 12: the game's number
      +03 u32  (set from the SQCL / DEFAULT / OVCL / CCCL screen)
      +07 u32  current period: play times, packed (shortest / average /
               longest, 10-bit fields)
      +0B u16×5 current period: games by number of players
      +15 u32  lifetime: play times, packed
      +19 u16×5 lifetime: games by number of players

- **0x0222** (315 bytes): the Dial-Up Network screen. *Verified:*

      +004 char  modem init string   "AT&FE1V1&C1&D2S95=45S2=43S12=3S24=0"
      +068 char  dial prefix
      +073 char  access number        "0860008484"
      +09C char  ISP login            "MERIT800@B-2000.NET"
      +0C5 char  ISP password         "CONSERVATION"
      +0EE char  server               "us.accessmerit.com"
      +117 char  DNS 1 (text)         "12.127.16.67"
      +127 char  DNS 2 (text)         "12.127.17.71"
      +137 u8    UPDATE hour, 0–23    15
      +138 u8 ×3 (options)

  A 0x0221 carrying a 0x0222 with a field changed writes it: the UPDATE hour
  moved from 3 pm to 10 am. *Verified.*

- **0x0A01** (no answer) sets the ISP login (+04) and password (+68), when the
  cabinet uses Merit's own ISP account.

### Reports

- **0x00C2 / 0x00C3**: game statistics, current period and lifetime: a
  54-byte header (totals, the period's start year and month), then for each
  game offered 23 bytes: game number, price, share of play (%), total plays,
  the game's number (upper 12 bits of the record), three 10-bit play times,
  and the plays by number of players — from the per-game record above.
- **0x00CA** (the answer to 0x00C9): 14-byte records, only those not zero:

      +0 u8   code          +1 u8 index
      +2 u32  a             +6 u32 b             +A u32 c

  - code 3, index 0–6: the coin inputs; a current, b lifetime count
    (`_NVRAMDATA`+0x3f / +0x4d, the books' E3/E4 COINS)
  - code 0x21, index 0–6: the bill acceptor, likewise (+0x5b / +0x69,
    B1/B2)
  - codes 11 (index 0–4), 83, 58, 69, 52, 53: per-game counters for
    licensed games (52 and 53 are the Jumble word games; their daily
    puzzles are `MISC\USER\WJDDC.DAT`): a and b running totals, c the plays
    since the last report — after a successful 0x00CA the cabinet zeroes c.
    Most likely the royalty report. *Verified: answered 0x00CA.*
- **0x00E2**: the last 14 calls, 14 bytes each: u32 start, u32 end (time_t),
  u8 status, u8 error code, … *Verified.*

## Running a server: `modem-server.py`

    python modem-server.py --port 2323 --log modem-server.log --state modem-server-state.json

In MegaPPBox: *Tools > Modem on COM2*, then *Tools > Modem settings…*: "Dial
out to a TCP/IP host", 127.0.0.1, port 2323. Several cabinets can call at
once; their messages are handled one at a time. With `--tcp-ports 15000,17751` it also
takes TournaMAXX straight over TCP on those ports (no modem, no PPP), and
with `--admin-port N` it takes requests from the control panel
(`panel/panel.py`) on 127.0.0.1: one JSON object per connection, `get`,
`put`, `delete`, `append`, `remove` or `replace` (see `apply_admin`), done
under the server's lock so that they never cross a call's own changes. The state file (created with
a test tournament if missing) holds the following. It can be edited while the
server runs: each call reads it again if it changed (an edit that does not
parse is ignored until it does).

What the operator sets:

- `tournaments`: id, game, start/end (Unix times), credits, gameopts, name,
  desc, randseed, seedinc, groups, prizes, showdate, final_days. The status
  is worked out from the clock.
- `locations` → machine serial: name, city_state, country, telephone. The
  cabinet's LOCATION INFO screen (0x00B1), the registration page of Initial
  Connection, and the location other cabinets show for its players (0x00D1).
  A cabinet's entry is made with placeholder text at its first call.
- `outbox` → machine serial: done at that cabinet's next update call, then
  moved to `outbox_done` with the outcome. An item the cabinet's client has
  no command for is skipped as "not for this cabinet".
  - `{"do": "message", "text": "line\nline"}`: a registration page (0x0A11)
  - `{"do": "send_file", "from": "local path", "to": "C:\\PATH"}`
  - `{"do": "fetch_file", "path": "C:\\PATH"}`: saved under `files\<serial>\`
  - `{"do": "delete", "path": "C:\\PATH", "reboot": false}`: an empty path
    only reboots
  - `{"do": "settings", ...}` (0x0201): `"volume": 30` (the percentage the
    operator menu shows), `"set": {"+0A": 38}` (bytes at message offsets),
    `"set16": {"+11": 4123}` (16-bit values), `"clear_scores": [[48, 255]]`
    (high-score clears, game and category). Built on the last 0x0202 the
    cabinet reported, so it needs one earlier call.
  - `{"do": "prices", "set": {"48": 3}}` (0x0211: game, credits per play)
  - `{"do": "dialup", "set": {"update_hour": 10, "phone": "...", "login": "...",
    "password": "...", "server": "...", "dns1": "...", "dns2": "..."}}`
  - `{"do": "isp", "login": "...", "password": "..."}`
  - `{"do": "location_entry", "id": 1, "name": "...", "city": "...",
    "state": "...", "country": "..."}`
  - `{"do": "counters"}`: read (and so clear) the event counters
- `updates` → name: software update packages (see
  [Update packages](#update-packages)):
  - `"send": [["local file", "C:\\PATH"], ...]`: the files; the cabinet is
    then rebooted
  - `"protocol": 7`: only for cabinets whose login has that protocol
  - `"result": "C:\\RESULT.TXT"`: a file the update writes; fetched (and
    deleted) at the next call, its text becomes the outcome
  - `"version": "7.01"`, `"becomes": "7.20"` (protocols 6 and 3, whose
    logins carry the version): only for cabinets logging in with `version`;
    installed when one logs in with `becomes`

What the server keeps:

- `players` (with `next_player_id`), `scores`: what the cabinets have sent.
- `reports` → machine serial: the settings, statistics and call history each
  update call reads.
- `logins` → machine serial: the last login (protocol, version, raw bytes).
- `update_status` → update → machine serial: queued, checking, or the outcome.
- `had`, `final`, `removed`, `players_sent`, `locations_sent`: what each
  cabinet has been given, so that nothing is sent twice.

## The cabinet's databases (`D:\Database\`)

dBase III files whose records are encrypted from byte 1 (after the delete
flag) as one stream: PC1 with the 10-byte key "M@xxR0cks!", where the round
index never advances (V8.04 0x93fb0). `tools/dbfcrypt.py` decodes them. At
boot, a `.bak` next to a file replaces it (the rollback of a broken call).


- **player.dbf**: ID N8, TEMPID N8, HANDLE C12, PIN C4, LOCATION N8, MODIFY N8,
  FIRSTNAME C20, LASTNAME C20, ADDRESS C30, CITY C30, STATE C35,
  POSTALCODE C16, COUNTRY C30, PHONE C20, BIRTHDAY D8, EMAIL C50,
  DATEADDED D8, LASTUPDATE D8, OPTIONAL C50, SEX C1, LANGUAGE N8, DUMMY C32
- **tourney.dbf**: ID N8, GAME N8, NAME C50, DESC C100, STATUS N8,
  LASTUPDT D8, START N8, END N8, CREDITS N8, TOTPLAYS N8, TOTCREDITS N8,
  GAMEOPTS N8, RANDSEED N8, SEEDINC N8, PRIZE1-3 C50, GROUP1-3 C50,
  SHOWDATE C8, DUMMY C24
- **scores.dbf / SC<id>.dbf**: DIRTY C1, TOURNID N8, PLAYERID N8, NEWPLAYS N8,
  NUMPLAYS N8, LANGUAGE N8, HANDLE C12, CITY C30, STATE C35, LOCATION C51,
  TOTAL1-3 N8, RANK1-3 N8, SCORE11-35 N8 (three groups × five), SCDATE1-5 C11,
  DUMMY C32
- **location.dbf**: ID N8, NAME C51, CITY C30, STATE C35, COUNTRY C30, DUMMY C32

## Older releases

The server tells them apart by the protocol version in the login and sends
each only the commands its dispatcher has (`KNOWS` in `modem-server.py`):

| Command | Emerald 2 (9) | Emerald V8.04 (7) | Double Diamond V7.01 (6) | Diamond V6.03 (3) |
|---|---|---|---|---|
| 0x0011–0x0067, 0x0081, 0x00B1, 0x00C1, 0x00D1, files, 0x0121, 0x0A01, 0x0A11, 0xFF01 | yes | yes | yes | yes |
| 0x0073 rankings | yes | yes | yes | — |
| 0x0071 rankings, older form | — | yes | yes | yes |
| 0x00C9 counters, 0x00E1 call history | yes | yes | yes | — |
| 0x0201, 0x0211, 0x0221 settings | yes | yes | — | — |
| 0xFF11 | yes | yes | — | yes |
| 0x0031 (not known) | — | — | — | yes |

- Emerald V8.04 and Double Diamond V7.01 dispatch through a table like
  Emerald 2's (V7.01 at 0x95bae); Diamond V6.03 through a chain of compares
  (0x9083f). A command a client does not have gets no answer, and the call
  hangs until the cabinet gives up.
- A final (status 4) for a tournament the cabinet never had gets no 0x0022
  from V8.04 either, so the server sends finals and removals only to
  cabinets that had the tournament.

### The 2002 date limit

Emerald V8.04, Double Diamond V7.01 and Diamond V6.03 keep a tournament's
START, END and SHOWDATE in Tourney.dbf as `%8d` of (time − 930,000,000)
seconds (V8.04 0x97762 load, 0x9784f save). From 2002-08-21 that needs nine
digits; the ninth is cut off, and the time reads back as early 2002, so with
a present-day clock every tournament shows as ENDED. Emerald 2 stores minutes
instead. The server can't help: START and END are relative to the cabinet's
own clock.

`datefix/tmfix.py` fixes the executable: the load and save code is rewritten
in place (same registers, every call at its own address) to store minutes,
good until 2038, and the version shown becomes x.20 (V8.20, V7.20, V6.20).
Nothing in the game compares the version text; Double Diamond and Diamond
put it in their login, so the server sees the new version. Verified on all
three: dates correct, rankings shown.

### Update packages

At boot a cabinet's `TEST.BAT` runs `C:\NetUpdt.exe -d -o c:\` (a PKZIP 2.04g
self-extractor: extract under C:\, with folders, overwriting) and deletes it;
Emerald V8.04's then also runs `C:\NetUpdt.bat` and deletes that. That is
how Merit shipped network updates. The server's `updates` send such a file
with 0x0111/0x0112, reboot the cabinet with an empty-path 0x0121, and on the
next call either fetch the update's result file or read the version in the
login. `mkupdate.py` makes the self-extractor from a folder laid out as the
cabinet's C:\ (PKSFX 2.04g's stub, then an ordinary zip).

- **Emerald V8.04**: the package holds `NETUPDT.BAT` and
  `MERIT2\EXEC\MEGACDLL.NEW` (735 KB; the exe alone is 1.7 MB). The batch
  file installs the new exe only if it and the old one are both 1,713,893
  bytes, deletes the old-format `Tourney.dbf`, and writes `C:\TMFIX.TXT`.
  *Verified: "TMFIX-804 INSTALLED".*
- **Double Diamond V7.01, Diamond V6.03**: their `TEST.BAT` runs only the
  self-extractor and they have no `FIND.EXE`, so the package holds
  `MERIT2\EXEC\MEGACDLL.EXE` itself, sent only to cabinets whose login says
  7.01 / 6.03; the update counts as installed when they log in as 7.20 /
  6.20. The running tournaments are sent again at that call and stored in
  the new form. *Verified.*

A transfer that breaks off leaves an incomplete self-extractor, which finds
no zip directory and extracts nothing.

## Still open

- 0x00B1's thirteen option bytes.
- Diamond V6.03's 0x0031.
- The per-game record's field at +03, and the names of games 11, 58, 69, 83.
- The Linux MAXX releases (Ruby onward), which have their own client.
