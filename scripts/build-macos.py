#!/usr/bin/env python3
"""Build self-contained, architecture-specific .app bundles and drag-to-install DMGs.

All downloaded tools are extracted into build/; no system Python is installed.
Requires macOS, Command Line Tools, make, pkg-config and curl.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "build/macos-tools"
PYTHON_VERSION = "3.13.16"
PYTHON_SHA256 = "30666509020b4da0dd8bc2e773255f34d76b7bb80b66960a928d5f6daa0192d7"
NTFS_VERSION = "2026.9.28"
NTFS_SHA256 = "350d9415c59f7c3fa74e23985ad2d56423c6a168337368f672e2e362d8f295c8"


def run(command, **kwargs):
    print("+ " + " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def download(url, destination, digest):
    if not destination.exists():
        run(["curl", "--fail", "--location", "--retry", "2", "--connect-timeout", "20",
             "--max-time", "180", url, "--output", destination])
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise RuntimeError("Checksum mismatch: " + str(destination))


def relocate_python(version):
    # Apple's arch launcher strips DYLD_* variables. Make the extracted build
    # interpreter genuinely relocatable so PyInstaller's isolated subprocesses
    # can run without a system-wide Python installation.
    original = "/Library/Frameworks/Python.framework/Versions/3.13/"
    for file in version.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        with file.open("rb") as handle:
            if handle.read(4) not in (bytes.fromhex("cafebabe"), bytes.fromhex("cffaedfe"), bytes.fromhex("feedfacf")):
                continue
        dependencies = subprocess.check_output(["otool", "-L", str(file)], text=True)
        replacements = set()
        for line in dependencies.splitlines():
            name = line.strip().split(" (compatibility version", 1)[0]
            if name.startswith(original):
                target = version / name[len(original):]
                relative = os.path.relpath(target, file.parent)
                replacements.add((name, "@loader_path/" + relative))
        if replacements:
            command = ["install_name_tool"]
            for old, new in sorted(replacements):
                command.extend(["-change", old, new])
            command.append(str(file))
            run(command)
            run(["codesign", "--force", "--sign", "-", file])


def bootstrap():
    TOOLS.mkdir(parents=True, exist_ok=True)
    package = TOOLS / "python.pkg"
    download("https://www.python.org/ftp/python/{0}/python-{0}-macos11.pkg".format(PYTHON_VERSION),
             package, PYTHON_SHA256)
    run(["pkgutil", "--check-signature", package])
    expanded = TOOLS / "python-expanded"
    if not expanded.exists():
        run(["pkgutil", "--expand-full", package, expanded])
    framework = TOOLS / "Python.framework"
    if not framework.exists():
        framework.symlink_to("python-expanded/Python_Framework.pkg/Payload", target_is_directory=True)
    version = framework / "Versions/3.13"
    relocate_python(version)
    python = version / "bin/python3.13"
    env = os.environ.copy()
    env.update(DYLD_FRAMEWORK_PATH=str(TOOLS), DYLD_LIBRARY_PATH=str(version / "lib"),
               PYTHONHOME=str(version), PYTHONPATH=str(ROOT), SSL_CERT_FILE="/etc/ssl/cert.pem")
    run([python, "-m", "ensurepip", "--upgrade"], env=env)
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r",
         ROOT / "packaging/macos/requirements.txt"], env=env)
    return python, env, version


def icon(python, env):
    iconset = TOOLS / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    script = """
import sys
from pathlib import Path
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QGuiApplication([])
svg = QSvgRenderer(sys.argv[1])
if not svg.isValid():
    raise RuntimeError('Invalid app icon SVG')
for size in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        image = QImage(size * scale, size * scale, QImage.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        svg.render(painter)
        painter.end()
        name = 'icon_{0}x{0}{1}.png'.format(size, '@2x' if scale == 2 else '')
        if not image.save(str(Path(sys.argv[2]) / name)):
            raise RuntimeError('Could not write icon image')
"""
    run([python, "-c", script, ROOT / "xbox_media_drive/icon.svg", iconset], env=dict(env, QT_QPA_PLATFORM="offscreen"))
    run(["iconutil", "-c", "icns", iconset, "-o", TOOLS / "AppIcon.icns"])


def licenses(version):
    output = TOOLS / "licenses"
    output.mkdir(exist_ok=True)
    shutil.copytree(ROOT / "packaging/macos/licenses", output / "Qt-license-texts", dirs_exist_ok=True)
    sites = version / "lib/python3.13/site-packages"
    for distribution in sites.glob("*.dist-info"):
        for file in distribution.rglob("*"):
            if file.is_file() and ("licenses" in [part.lower() for part in file.relative_to(distribution).parts]
                                   or "license" in file.name.lower() or "copying" in file.name.lower()):
                destination = output / distribution.name / file.relative_to(distribution)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, destination)
    qt_licenses = sites / "PySide6/Qt/licenses"
    if qt_licenses.exists():
        shutil.copytree(qt_licenses, output / "Qt", dirs_exist_ok=True)
    python_license = version / "Resources/English.lproj/License.rtf"
    candidates = [version / "lib/python3.13/LICENSE.txt", python_license,
                  TOOLS / "python-expanded/Python_Framework.pkg/Resources/en.lproj/License.txt"]
    for file in candidates:
        if file.exists():
            shutil.copyfile(file, output / ("Python-" + file.name))


def source_archive(destination, ntfs_source):
    # Include tracked and untracked project source, respecting .gitignore.
    listing = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    with tarfile.open(destination, "w:gz") as archive:
        for name in sorted(set(os.fsdecode(item) for item in listing.split(b"\0") if item)):
            path = ROOT / name
            if path.is_file():
                archive.add(path, arcname="xbox-media-drive-helper/" + name, recursive=False)
        archive.add(ntfs_source, arcname="third-party/" + ntfs_source.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("arm64", "x86_64", "both"), default="both")
    parser.add_argument("--codesign-identity", help="Optional Developer ID Application signing identity")
    parser.add_argument("--skip-native-build", action="store_true", help="Reuse architecture-specific native libraries")
    parser.add_argument("--skip-dmg", action="store_true", help="Build and verify .app bundles without disk images")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("macOS app bundles must be built on macOS")
    for command in ("cc", "make", "pkg-config", "curl", "pkgutil", "codesign", "hdiutil", "lipo", "iconutil"):
        if not shutil.which(command):
            parser.error("Missing build tool: " + command)
    python, env, version = bootstrap()
    icon(python, env)
    licenses(version)
    ntfs_source = TOOLS / ("ntfs-3g_ntfsprogs-" + NTFS_VERSION + ".tgz")
    download("https://tuxera.com/opensource/" + ntfs_source.name, ntfs_source, NTFS_SHA256)
    dist = ROOT / "dist/macos"
    dist.mkdir(parents=True, exist_ok=True)
    architectures = ("arm64", "x86_64") if args.arch == "both" else (args.arch,)
    for architecture in architectures:
        prefix = ROOT / "build/macos-native" / architecture
        if not args.skip_native_build:
            run([sys.executable, ROOT / "scripts/build-native.py", "--prefix", prefix, "--arch", architecture,
                 "--min-macos", "15.0", "--archive", ntfs_source])
        run(["/usr/bin/arch", "-" + architecture, python, ROOT / "scripts/test-native.py", prefix,
             "--compiler-arch", platform.machine()], env=env)
        native = (prefix / "lib/libntfs-3g.dylib").resolve(strict=True)
        build_env = dict(env, XMD_MACOS_ARCH=architecture, XMD_MACOS_NATIVE=str(native))
        if args.codesign_identity:
            build_env["XMD_CODESIGN_IDENTITY"] = args.codesign_identity
        run([python, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", dist / architecture,
             "--workpath", ROOT / "build/pyinstaller" / architecture,
             ROOT / "packaging/macos/XboxMediaDrive.spec"], env=build_env)
        app = dist / architecture / "Xbox Media Drive Helper.app"
        report = dist / architecture / "verification.json"
        # Deliberately use the caller's ordinary Python/environment for verification,
        # so the app cannot accidentally rely on the extracted build interpreter.
        run([sys.executable, ROOT / "scripts/verify-macos.py", app, "--arch", architecture, "--output", report])
        if not args.skip_dmg:
            stage = ROOT / "build/dmg-stage" / architecture
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir(parents=True)
            run(["ditto", app, stage / app.name])
            (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
            shutil.copyfile(ROOT / "packaging/macos/INSTALL.txt", stage / "INSTALL.txt")
            shutil.copyfile(ROOT / "packaging/macos/THIRD_PARTY_NOTICES.md", stage / "THIRD_PARTY_NOTICES.md")
            shutil.copyfile(report, stage / "verification.json")
            image = dist / ("Xbox-Media-Drive-Helper-0.1.0-" + architecture + ".dmg")
            run(["hdiutil", "create", "-ov", "-format", "UDZO", "-volname", "Xbox Media Drive Helper",
                 "-srcfolder", stage, image])
            run(["hdiutil", "verify", image])
    sources = dist / "Xbox-Media-Drive-Helper-0.1.0-source.tar.gz"
    source_archive(sources, ntfs_source)
    checksums = []
    for file in sorted(dist.glob("*.dmg")) + [sources]:
        checksums.append(hashlib.sha256(file.read_bytes()).hexdigest() + "  " + file.name)
    (dist / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n")
    print("Installable apps and archives: " + str(dist))


if __name__ == "__main__":
    main()
