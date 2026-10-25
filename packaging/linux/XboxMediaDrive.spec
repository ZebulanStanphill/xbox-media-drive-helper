# Run through scripts/build-linux.py so the native library and version are explicit.
import os
from pathlib import Path

root = Path(SPECPATH).resolve().parent.parent
native = Path(os.environ["XMD_LINUX_NATIVE"])
tools = root / "build/linux-tools"

analysis = Analysis(
    [str(root / "packaging/launcher.py")],
    pathex=[str(root)],
    binaries=[(str(native), ".")],
    datas=[(str(root / "xbox_media_drive/icon.svg"), "xbox_media_drive"),
           (str(root / "LICENSE"), "licenses"),
           (str(root / "packaging/linux/THIRD_PARTY_NOTICES.md"), "licenses"),
           (str(tools / "licenses"), "licenses/third-party")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "tkinter", "unittest"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [],
    exclude_binaries=True,
    name="XboxMediaDrive",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
collection = COLLECT(executable, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name="XboxMediaDrive")
