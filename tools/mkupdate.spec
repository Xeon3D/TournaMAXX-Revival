# PyInstaller spec for mkupdate.exe: tools/mkupdate.py as one Windows exe,
# with PKZIP 2.04g's PKZIP.EXE and ZIP2EXE.EXE inside (DOSBox-X stays
# outside).  Build:
#
#   set MEGAPPBOX_PKZIP=<folder with PKZIP.EXE and ZIP2EXE.EXE>
#   python -m PyInstaller --distpath build\mkupdate --workpath build\mkupdate-work tools\mkupdate.spec
import os

pk = os.environ.get("MEGAPPBOX_PKZIP")
if not pk:
    raise SystemExit("set MEGAPPBOX_PKZIP to the folder with PKZIP.EXE and ZIP2EXE.EXE")


def pkfile(name):
    for f in os.listdir(pk):
        if f.upper() == name:
            return (os.path.join(pk, f), "pkzip")
    raise SystemExit("%s has no %s" % (pk, name))


a = Analysis([os.path.join(SPECPATH, "mkupdate.py")],
             datas=[pkfile("PKZIP.EXE"), pkfile("ZIP2EXE.EXE")],
             excludes=["tkinter", "unittest", "pydoc", "email", "http", "xml"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name="mkupdate", console=True, upx=False)
