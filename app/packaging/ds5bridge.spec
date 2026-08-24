# PyInstaller spec for ds5bridge. Build with app\packaging\build.ps1.
#
# Two executables from one analysis:
#
#   ds5bridge.exe        console; the CLI (run / devices / cleanup / doctor)
#   ds5bridge-tray.exe   windowed; the same BridgeService behind a tray icon
#
# ONE-DIR, not one-file, and that is a considered choice -- see build.ps1 and
# docs/USER-GUIDE.md. Set DS5_ONEFILE=1 to build the one-file variant for
# comparison; both work, and the measurements that decided it are in
# docs/STATUS.md section 18.
#
# The three source trees are siblings and none is installed as a package, so
# `pathex` is how `ds5app`, `ds5emu` and `ds5bridge` all become importable. At
# runtime `app/ds5app/_bootstrap.py` sees `sys.frozen` and adds nothing, because
# the packages are already top-level inside the bundle.

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

SPEC_DIR = Path(SPECPATH).resolve()
APP_DIR = SPEC_DIR.parent
REPO = APP_DIR.parent

ONEFILE = os.environ.get("DS5_ONEFILE") == "1"

hidden = [
    # Imported by name inside functions, so the analysis cannot see them:
    #   service._start_server -> ds5emu.bridge / ds5emu.server / ds5emu.timing
    #   ds5emu.backend.__getattr__ -> ds5emu.bridge
    "ds5emu.bridge", "ds5emu.server", "ds5emu.timing", "ds5emu.backend",
    "ds5emu.device", "ds5emu.translate", "ds5emu.descriptors", "ds5emu.uac",
    "ds5emu.wire", "ds5emu._bootstrap",
    "ds5bridge.protocol", "ds5bridge.device", "ds5bridge.audio",
    "ds5bridge.pacing", "ds5bridge.crc",
    "ds5app.tray", "ds5app.cli", "ds5app.service", "ds5app.controller",
    "ds5app.usbip",
    # PyAV loads its codecs through the extension modules; numpy is pulled in
    # by ds5bridge.audio.
    "av", "numpy", "hid",
    # pystray picks its backend at import time from the platform.
    "pystray._win32", "PIL.Image", "PIL.ImageDraw",
]

# PyAV ships libopus/swresample DLLs in av.libs; the bundled hook collects them,
# but ask explicitly so a hook regression turns into a build error rather than a
# runtime "codec not found" three weeks later.
binaries = collect_dynamic_libs("av") + collect_dynamic_libs("numpy")

excludes = [
    # Nothing here draws a plot, opens a notebook or serves a web page, and
    # each of these adds tens of megabytes.
    "tkinter", "matplotlib", "scipy", "pandas", "IPython", "notebook",
    "pytest", "sounddevice", "setuptools", "pip",
]

a = Analysis(
    [str(APP_DIR / "packaging" / "entry_cli.py")],
    pathex=[str(APP_DIR), str(REPO / "emulator"), str(REPO / "prototype")],
    binaries=binaries,
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

a_tray = Analysis(
    [str(APP_DIR / "packaging" / "entry_tray.py")],
    pathex=[str(APP_DIR), str(REPO / "emulator"), str(REPO / "prototype")],
    binaries=binaries,
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    excludes=excludes,
    noarchive=False,
)
pyz_tray = PYZ(a_tray.pure)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="ds5bridge",
              console=True, upx=False, strip=False)
    exe_tray = EXE(pyz_tray, a_tray.scripts, a_tray.binaries, a_tray.datas, [],
                   name="ds5bridge-tray", console=False, upx=False, strip=False)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ds5bridge",
              console=True, upx=False, strip=False)
    exe_tray = EXE(pyz_tray, a_tray.scripts, [], exclude_binaries=True,
                   name="ds5bridge-tray", console=False, upx=False, strip=False)
    COLLECT(exe, a.binaries, a.datas,
            exe_tray, a_tray.binaries, a_tray.datas,
            strip=False, upx=False, name="ds5bridge")
