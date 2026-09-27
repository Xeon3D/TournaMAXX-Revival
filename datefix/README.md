# The tournament date fix

Emerald V8.04, Double Diamond V7.01 and Diamond V6.03 keep a tournament's
START, END and SHOWDATE in `D:\Database\Tourney.dbf` as `%8d` of (time −
930,000,000) seconds. From 2002-08-21 that needs nine digits, the ninth is
cut off, and the time reads back as early 2002: every tournament shows as
ended. Emerald 2 stores minutes. `tmfix.py` rewrites the six places that
store and read these times so they store minutes too (good until 2038), and
changes the version shown to x.20.

Needs Python 3 and `pip install capstone`.

    python tmfix.py check MEGACDLL.EXE              # unpatched / patched / unknown
    python tmfix.py patch MEGACDLL.EXE MEGACDLL.NEW  # prints both SHA-256s

Take `MEGACDLL.EXE` from the cabinet's `C:\MERIT2\EXEC`.

## Update packages

**Emerald V8.04**: its `TEST.BAT` also runs `C:\NetUpdt.bat` after the
self-extractor, so the package installs with a check. Make a folder:

    tmfix-804\NETUPDT.BAT              (NETUPDT-V804.BAT from here)
    tmfix-804\MERIT2\EXEC\MEGACDLL.NEW (the patched exe)

    python ..\mkupdate.py tmfix-804 --protocol 7 --result C:\TMFIX.TXT

The batch file installs `MEGACDLL.NEW` only if both it and the old exe are
1,713,893 bytes, clears the old-format `Tourney.dbf`, and writes the outcome
to `C:\TMFIX.TXT`, which the server fetches on the next call.

**Double Diamond V7.01 and Diamond V6.03**: their `TEST.BAT` runs only the
self-extractor, so the package holds the fixed exe itself:

    tmfix-dd701\MERIT2\EXEC\MEGACDLL.EXE

    python ..\mkupdate.py tmfix-dd701 --protocol 6

Their logins carry the game's version, so limit the update to it and let the
server see it arrive. In the state file, under `"updates"`:

    "tmfix-dd701":  {"protocol": 6, "version": "7.01", "becomes": "7.20",
                     "send": [["...\\NETUPDT.EXE", "C:\\NETUPDT.EXE"]]},
    "tmfix-dia603": {"protocol": 3, "version": "6.03", "becomes": "6.20",
                     "send": [["...\\NETUPDT.EXE", "C:\\NETUPDT.EXE"]]}

The running tournaments are sent again on the next call and stored in the
new form.
