# PyInstaller spec for mkupdate.exe: tools/mkupdate.py as one windowed
# Windows exe (it carries the PKSFX stub itself; nothing else is needed).
# Build:
#
#   python -m PyInstaller --distpath build\mkupdate --workpath build\mkupdate-work tools\mkupdate.spec
import os

a = Analysis([os.path.join(SPECPATH, "mkupdate.py")],
             excludes=["unittest", "pydoc", "email", "http", "xml", "sqlite3"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name="mkupdate", console=False, upx=False)
