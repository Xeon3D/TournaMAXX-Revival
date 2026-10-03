# Why MAXX Crown shows no TournaMAXX: NVRAM, settings and the security key

MAXX Crown V16.10 has the same TournaMAXX client as Jade 2 V15.10, and finds
the modem, yet neither its operator setup nor its game menu offers
TournaMAXX. This is what was found, from the Crown and Jade 2 images, their
game program (`/usr/local/bin/start`, unpacked with
[tools/unpack_dstart.py](../tools/unpack_dstart.py)), the operator setup
library and the security keys. Addresses are Crown's (game program loaded at
0x08048000; `opsetup.so` as linked) unless Jade 2 is named.

In short: **TournaMAXX is a key option.** The operator setup shows its
TournaMAXX category only when the security key allows option 20, and the
first Crown dumps (USA, Canada) had it off. Jade 2's keys have it on, and so
do the Crown keys MegaPPBox bundles since 3 October 2026 (below). An
NVRAM byte holds the operator's choice, but only counts when the key leaves
the option to the operator. [tools/meritkey.py](../tools/meritkey.py) reads
the key and makes a copy with the option turned on.

**Status:** the modified Crown key is made and fitted on a test copy of the
image; that TournaMAXX then appears is not confirmed yet.

## Which releases have a modem

One image of each release, read without changing it. DOS releases: is there
the TournaMAXX client's dial-up data (`DIALINFO.*`) and code in the game
program (`MERIT2\EXEC\MEGACDLL.EXE`, present in every DOS release, is the
game itself); Linux releases: the dialer (`/usr/local/bin/wvdial`) and the
modem code in the game program.

| Release | Modem | TournaMAXX |
|---|---|---|
| XL Platinum V1.02, XL Double Platinum V2.00, XL Titanium V3.00, XL Titanium 2 V4.00 | none (no dialer, no PPP) | no |
| MAXX V3.06 | none (only a "has modem" setting name) | no |
| MAXX 2000 V4.00, MAXX 2000 Plus V5.00 | yes: a PPP dialer that also answers calls | no (no TournaMAXX client, no `DIALINFO`) |
| Diamond V6.03, Double Diamond V7.01, Emerald V8.04, Emerald 2 V9.01 | yes | yes |
| Ruby 2 V11.00/V11.05, Sapphire V12.01, Sapphire 2 V13.00, Jade V14.00, Jade 2 V15.10, Crown V16.10 | yes (`wvdial`, `/dev/modem`) | yes (from Sapphire also over a network card) |

What MAXX 2000 and 2000 Plus dialed is not known; it is not this protocol.
Ruby (V10) was not checked.

On every Linux release `/etc/init.d/mountfs` links `/dev/modem` to
`/dev/ttyS1` (COM2); `startpctel` relinks it to a PCTel soft modem on boards
that have one. The game records what it found in `/var/config/hardware.cfg`
(`modem: ActionTec 56K Data/Fax PC Card` with MegaPPBox's modem; `modem:
device open failure` when COM2 has nothing).

## Crown against Jade 2

The two game programs differ in one TournaMAXX-related symbol
(`IsTMaxxMTGame`, Crown only); the TournaMAXX graphics, data folders and
dial-up files are the same, and both find the modem. The difference is in
the settings and the key.

## `/var/config/config.db`

An SQLite **2.1** database, opened with `sqlite_open_crypt` from Merit's own
`libsqlite.so.0`. The "encryption" is the whole file XORed with the 32-bit
key `0x53bef1fc` (the game passes it to `sqlite_open_crypt`, in
`db_common_open_db`): bytes `fc f1 be 53` repeating. Decrypted, it starts
`** This file contains an SQLite 2.1 database **`.

Two tables:

- `menu_layout (category_id, position, game_id, active, timestamp)`: the
  game menu's categories, 450 rows on both Crown and Jade 2;
- `nvram_values (option_id, option_desc, value, timestamp)`: one row on both,
  `70 ENABLE_CARD_BACK_AD_SUPPORT 1`.

Nothing in it concerns TournaMAXX.

## `/var/config/nvram.dat`

The game's settings block `NVRAMData` (0x15d1c bytes) saved as a 128 KB file,
not encrypted; a copy is kept in `/home/maxx/var.bak/config/nvram.dat`, which
the game can restore from.

**Checksum** (`NVCheckOK`, `NVUpdtChecksum`): the 16-bit sum of bytes 1 to
0x1fffd, each taken as signed, stored little-endian at 0x1fffe. A file that
fails it is reset ("Bad CheckSum ... Resetting").

| Offset | |
|---|---|
| 0x00 | 1 |
| 0x01 | the key's game ID, 8 characters (`SA351301`): rewritten from the fitted key |
| 0x0a | the machine serial |
| 0x38 | `R00` |
| 0x8e + n | **operator option n** (0-79): the operator's on/off for key options the key leaves selectable (see below) |
| 0x12ec | u32, a territory code: 0x2000000 disables tournaments (Jade 2: 0x12e4) |
| 0x15454 | u32, written by the TournaMAXX client after a session; turns on the operator setup's MegaNet category (Jade 2: 0x1544c, 274 there) |
| 0x156d4 | bit 0: coinless mode |

The layout moved by 8 bytes between Jade 2 and Crown somewhere between 0xdd
and 0x12e4: compare the two only through each game's own code.

Operator options that matter here:

| Option | Byte | Read by |
|---|---|---|
| 20 | 0xa2 | `NTournSupported`, `UpdateNTournSupported`, the operator setup's `IsOptionOn(20)`: **TournaMAXX** |
| 36 | 0xb2 | `TournSupported(1)`: local **Tournament** setup |
| 19, 51, 57, 58, 61, 62, 66, 69 | 0xa1, 0xc1, 0xc7, 0xc8, 0xcb, 0xcc, 0xd0, 0xd3 | written by the server's V12 features packet (below) |

Crown had option 20 at 0 and Jade 2 at 1; options 50, 57, 59, 62, 64 and 69
were also 0 on Crown and 1 on Jade 2.

## The game menu: `NTournSupported`

`MenuClass::UpdateNTournSupported` sets the game menu's TournaMAXX and
TournaChamp entries from `NTournSupported(8)` and `NTournSupported(2)`, and
clears both when option 20 (0xa2) is 0. `NTournSupported(type)` returns 0
when option 20 is 0 or the territory code is 0x2000000; otherwise it looks
the tournament type up in the tournament database.

Setting 0xa2 to 1 (and the checksum) on a copy of Crown's NVRAM did **not**
bring TournaMAXX into the operator setup: that one is the key's.

## The operator setup: `opsetup.so`

Operator setup is `/usr/local/lib/merit/opsetup.so`, next to the games'
libraries; its screens are laid out by files under
`/usr/local/share/merit/opsetup/` (`main/main.layout`,
`cats/tournamaxx/tournamaxx.layout`, ...). `MainMenu::DisableCategories`
(0xc5444) gives each of `main.layout`'s 13 category buttons (controls
2201-2213) a flag:

| Button | Shown when |
|---|---|
| Credits/Pricing, Games, Hi Scores, Books, System, Diagnostics, Presentation | always |
| **TournaMAXX** | **`IsOptionOn(20)`** |
| TouchTunes | the TouchTunes interface exists (`s_TTunesIface`) |
| Tournament | `TournSupported(1)`: option 36 (0xb2) on, territory not 0x2000000 |
| Credit Card | the key is not in New Jersey rental mode (`KeyMgr::IsNJRentalMode`) |
| Promotion | always, except in New Jersey rental mode |
| MegaNet | (NVRAM 0x15454 ≠ 0, or `NTournSupported(2)` and options 41 / 11 allow) and `MegaNetOptionsAvalible(1)` |

`main.layout` is the same on Crown and Jade 2. With MegaPPBox's keys, Jade 2
shows TournaMAXX and MegaNet; Crown shows neither.

`IsOptionOn(n)` (0xc3720) asks the key first:

    state = KeyMgr::GetOptionState(n)
      -1 (n not 0-79), 0  ->  off
      1                   ->  on
      2, 3                ->  NVRAM[0x8e + n], the operator's choice

`KeyMgr::IsOptionSelectable(n)` is true for states 2 and 3. The states come
from `KeyMgr::ExplodeOptions` (0xb87d4): the 20 bytes at
`DallasKeyP3 + 0x10`, four options a byte, two bits each, high bits first:
option n is bits `6 - 2(n % 4)` of byte `n / 4`. **Option 20 is the top two
bits of byte 5.**

## The security key

A Dallas **DS1991** iButton: three password-protected subkeys (8-byte ID,
8-byte password, 48 bytes of data) and a 64-byte scratchpad. MegaPPBox keeps
them as 264-byte "full" dumps: subkeys at 0, 64, 128 (ID, password, data),
the scratchpad at 192, the ROM ID at 256, last byte first (Crown USA:
`82 1f 6f 03 00 40 00 ff`).

The game (`ReadKeyData`, 0x0828af60) reads the ROM ID into `k_laser`, works
out the subkey passwords from it, reads the three subkeys into `k_id1-3` and
`k_data1-3` and the scratchpad into `k_scratch`, then calls `decrypt`
(0x0828c0c0). `InitVars` allocates 64 bytes for each of `DallasKeyP1-3`,
and the key data is copied in as a subkey is laid out: ID at +0, data at
+0x10. So `DallasKeyP3 + 0x10` is subkey 2's data.

### The cipher

A 4-byte key from ROM ID bytes 1-5 (`r` = the ROM ID in read order, all sums
modulo 256):

    k0 = ((r3 - r4 - r5) ^ r1) ^ 0x7f
    k1 = (r1 + r2 - r3 - r4) ^ 0xf3
    k2 = (2*r2 + r1) ^ (r4 - 0x44)
    k3 = r4 + r5 + r1 + r4 + 0x7f

Each byte of a block:

    plain = (cipher - state) ^ k[i % 4]          state += 0x16

`decrypt` runs it over subkey 0's ID then its data (the state carrying on),
the same for subkeys 1 and 2, then 60 bytes of the scratchpad, each block
starting from its own byte of `BlkVCode`. `ReadKeyData(0)` sets those to
`12 78 37 f3`, `ReadKeyData(n)` to random values from seed n; both Crown
dumps decode with **`c2 28 e7`** for the subkeys and **`f3`** for the
scratchpad (it decodes to zeros), the same for both keys: where the game
gets `c2 28 e7` was not traced. `merit_encrypt` (0x0828bee0) is the
inverse.

The same key formula does not decode MegaPPBox's Jade keys: Jade 2's game
works its key out otherwise (not looked into).

### What the Crown keys hold

The first dumps looked into (these tables describe them):

| | USA (`MCROWN_full_FF004000036F1F82`) | Canada (`MCROWN_full_6D000000CB053702`) |
|---|---|---|
| Subkey 0 | ID `SA351301` (the game ID); data: per-game settings | ID `SA351133` |
| Subkey 1 | ID `10/13/05` (made); data includes `USA-STD` | ID `10/21/05`; `CAN-VER2` |
| Subkey 2 | ID `R00`; data 0-19: the 80 option states | same |
| Option 20 | **0** | **0** |

MegaPPBox now bundles two other dumps, both with option 20 at 1 (on):

- USA, `MCROWN_full_FF004000036F1F82`: the same key (ROM ID, IDs, passwords),
  with option 20 at 1; nothing else differs.
- Canada, `MCROWN_full_0700400003C4D082`: another part (family 82, like the
  USA key), subkeys as the old Canada dump (`SA351133`, `10/21/05`,
  `CAN-VER2`), options 20 and 61 at 1. It replaces `6D000000CB053702`,
  which is outside Crown V16's key range.

USA options 0-79:

    33233203233132313133 03221333303320320333 22223020200332100000 30010300003013201323

The game changes only options 32-35 itself (byte +0x18 of `DallasKeyP3`,
when byte +0x3e of `DallasKeyP2` is 3). No checksum or signature over the
decrypted data was found; the last 8 bytes of subkey 0's data are the same
on both keys.

With MegaPPBox's Crown V16.10 image, the old Canada key stops at a key
mismatch error (probably its game ID, `SA351133`, against the image's
`SA351301`; not traced); use the USA key.

### A key with TournaMAXX

    python tools/meritkey.py MCROWN_full_FF004000036F1F82 --set 20=3 -o MCROWN_full_FF004000036F1F82_TMAXX

turns option 20 from 0 to 3 (the operator's choice, which the operator setup
then reads from NVRAM 0xa2). The cipher works byte by byte, so one byte of
the dump changes (149, subkey 2's data byte 5: 0x47 → 0x87); IDs, passwords
and ROM ID stay as they were. In MegaPPBox: **Key → Other key file...**, or
`key` and `key_file` in `MegaPPBox.cfg`, then reset the machine.

Once a game has written to a key, MegaPPBox keeps the subkeys in
`nvr/megatouch_ds1991_<ROM ID>.bin` and loads them over the dump, so a
changed dump of a key that has been written to needs that file removed (or
changed the same way).

## The server's features and timebomb packets

From Sapphire (V12) on, the TournaMAXX client handles server packets that
the DOS releases do not have, among them:

- `Process_V12_Features_Request`: sets operator options 19 (from the
  packet's byte +0x29 unless 0xff), 51, 57, 58, 61, 62, 66 and 69, more of
  NVRAM (0x15688, 0x1568c, 0x156e0, ...), per-game MegaNet records and the
  Fantasy Sports settings;
- `Process_Setup_Options_Request`, `Process_Game_Pricing_Request`,
  `Process_Menu_Layout`: operator settings, prices and the menu layout;
- `Process_Timebomb_Request`: a licence expiry (lease mode).

`modem-server.py` sends none of these. Since Jade 2's options 50-69 were
set where Crown's were not, Merit's server probably set them; the features
packet's layout is not worked out yet.

## Still open

- Whether the key with option 20 at 3 brings TournaMAXX up on Crown (fitted,
  not confirmed).
- `MegaNetOptionsAvalible`: which options it reads (MegaNet is hidden on
  Crown too).
- Where `ReadKeyData` gets the starting states `c2 28 e7`; how Jade 2's game
  derives its key.
- The V12 features packet's layout, and whether the game menu needs more
  than option 20 for TournaMAXX.
