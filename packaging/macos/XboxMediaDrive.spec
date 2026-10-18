# Run through scripts/build-macos.py so architecture and native paths are explicit.
import os
from pathlib import Path

root = Path(SPECPATH).resolve().parent.parent
architecture = os.environ["XMD_MACOS_ARCH"]
native = Path(os.environ["XMD_MACOS_NATIVE"])
identity = os.environ.get("XMD_CODESIGN_IDENTITY") or None

analysis = Analysis(
    [str(root / "packaging/macos/launcher.py")],
    pathex=[str(root)],
    binaries=[(str(native), ".")],
    datas=[(str(root / "xbox_media_drive/icon.svg"), "xbox_media_drive"),
           (str(root / "LICENSE"), "licenses"),
           (str(root / "packaging/macos/THIRD_PARTY_NOTICES.md"), "licenses"),
           (str(root / "build/macos-tools/licenses"), "licenses/third-party"),
           (str(root / "packaging/macos/INSTALL.txt"), "documentation")],
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
    console=False,
    argv_emulation=False,
    target_arch=architecture,
    codesign_identity=identity,
    entitlements_file=None,
)
collection = COLLECT(executable, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name="XboxMediaDrive")
application = BUNDLE(
    collection,
    name="Xbox Media Drive Helper.app",
    icon=str(root / "build/macos-tools/AppIcon.icns"),
    bundle_identifier="org.xboxmediadrive.helper",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "15.8",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHighResolutionCapable": True,
        "NSRemovableVolumesUsageDescription": "Inspect and update permissions on the external NTFS drive you select.",
        "NSAppleEventsUsageDescription": "Request administrator authorization for the disk-permission helper.",
        "NSHumanReadableCopyright": "GPL-2.0-or-later. Unaffiliated with Microsoft or Xbox.",
        "XMDArchitecture": architecture,
    },
)
