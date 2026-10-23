# PyInstaller spec for the engine the desktop shell starts.   python scripts/build_engine.py
# One folder (not one file): a one-file build unpacks ~300 MB to a temp folder on every start.
from PyInstaller.utils.hooks import collect_data_files

datas = [("web/dist", "web/dist")] + collect_data_files("pycountry")

a = Analysis(
    ["engine_main.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=["api.server", "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                   "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "multipart", "openpyxl"],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="engine", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="engine", upx=False)
