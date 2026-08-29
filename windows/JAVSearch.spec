# Build from the repository root: pyinstaller windows/JAVSearch.spec --noconfirm
from pathlib import Path

root = Path(SPECPATH).parent
datas = [
    (str(root / "frontend"), "frontend"),
    (str(root / "backend" / "assets"), "backend/assets"),
    (str(root / "VERSION"), "."),
]
ffprobe = root / "tools" / "ffprobe.exe"
binaries = [(str(ffprobe), "tools")] if ffprobe.exists() else []

a = Analysis(
    [str(root / "windows" / "launcher.py")],
    pathex=[str(root / "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                   "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="JAV Search", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    icon=str(root / "windows" / "app.ico") if (root / "windows" / "app.ico").exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name="JAV Search")

