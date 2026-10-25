#!/usr/bin/env python3
"""Build a self-contained Linux AppImage.

Everything downloaded is verified against a pinned SHA-256 digest and extracted into build/.
Requires Linux, a C compiler, make, pkg-config and curl. The Python that runs this script
(3.9+, 3.13 in CI) is bundled; build on the oldest glibc you want to support.
"""
import argparse
import hashlib
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "build/linux-tools"
NATIVE = ROOT / "build/linux-native"
# The app version is defined only in the package; the file names follow it.
APP_VERSION = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "xbox_media_drive/__init__.py").read_text(),
                        re.MULTILINE).group(1)
NTFSPROGS_VERSION = "1.0.0"
NTFSPROGS_URL = "https://github.com/ntfsprogs-plus/ntfsprogs-plus/archive/refs/tags/{}.tar.gz".format(NTFSPROGS_VERSION)
NTFSPROGS_SHA256 = "3e3d9d2f3620cdebb128fb8dbc116f2c2a035f18000e3f01af204694ef8c7778"
# The only shared libraries the bundled ntfsck may need: all come with glibc on every Linux system.
ALLOWED_LIBRARIES = re.compile(r"libc\.so\.6|ld-linux[\w.-]*\.so\.\d+|linux-vdso\.so\.1|libm\.so\.6"
                               r"|libpthread\.so\.0|libdl\.so\.2|librt\.so\.1")
APPIMAGETOOL_VERSION = "1.9.1"
RUNTIME_VERSION = "20251108"
# Release assets by architecture: (file name, SHA-256).
APPIMAGETOOL = {
    "x86_64": ("appimagetool-x86_64.AppImage", "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"),
    "aarch64": ("appimagetool-aarch64.AppImage", "f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158"),
}
RUNTIME = {
    "x86_64": ("runtime-x86_64", "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"),
    "aarch64": ("runtime-aarch64", "00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444"),
}


def run(command, **kwargs):
    print("+ " + " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def download(urls, destination, digest):
    for url in ([urls] if isinstance(urls, str) else urls):
        if not destination.exists():
            if subprocess.run(["curl", "--fail", "--location", "--retry", "2", "--connect-timeout", "20",
                               "--max-time", "180", url, "--output", str(destination)]).returncode != 0:
                print("Download failed; trying the next source.", flush=True)
                destination.unlink(missing_ok=True)
                continue
        break
    else:
        raise RuntimeError("Could not download " + destination.name)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        destination.unlink()
        raise RuntimeError("Checksum mismatch: " + destination.name)


def native_script():
    # scripts/build-native.py pins NTFS-3G; reuse its version, digest and mirrors.
    spec = importlib.util.spec_from_file_location("build_native", ROOT / "scripts/build-native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bootstrap():
    """Create the build virtualenv with the pinned PyInstaller and Qt wheels."""
    environment = TOOLS / "venv"
    if not environment.exists():
        run([sys.executable, "-m", "venv", environment])
    python = environment / "bin/python"
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r",
         ROOT / "packaging/linux/requirements.txt"])
    return python


def icon(python, appdir):
    script = """
import sys
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QGuiApplication([])
svg = QSvgRenderer(sys.argv[1])
if not svg.isValid():
    raise RuntimeError('Invalid app icon SVG')
image = QImage(256, 256, QImage.Format_ARGB32)
image.fill(0)
painter = QPainter(image)
svg.render(painter)
painter.end()
if not image.save(sys.argv[2]):
    raise RuntimeError('Could not write icon image')
"""
    run([python, "-c", script, ROOT / "xbox_media_drive/icon.svg", appdir / "xbox-media-drive-helper.png"],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))


def licenses(python):
    output = TOOLS / "licenses"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir()
    # The Qt license texts are shared with the macOS build.
    shutil.copytree(ROOT / "packaging/macos/licenses", output / "Qt-license-texts")
    sites = Path(subprocess.check_output([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                         text=True).strip())
    for distribution in sites.glob("*.dist-info"):
        for file in distribution.rglob("*"):
            if file.is_file() and ("licenses" in [part.lower() for part in file.relative_to(distribution).parts]
                                   or "license" in file.name.lower() or "copying" in file.name.lower()):
                destination = output / distribution.name / file.relative_to(distribution)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, destination)
    qt_licenses = sites / "PySide6/Qt/licenses"
    if qt_licenses.exists():
        shutil.copytree(qt_licenses, output / "Qt")
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if python_license.exists():
        shutil.copyfile(python_license, output / "Python-LICENSE.txt")
    else:
        print("Warning: no Python license text found at " + str(python_license), flush=True)
    return output


def extract(archive, destination):
    """Extract a pinned source archive, rejecting links and paths that escape the folder."""
    destination = destination.resolve()
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            path = (destination / member.name).resolve()
            if destination not in path.parents or member.issym() or member.islnk():
                raise RuntimeError("Unsafe path in source archive: " + member.name)
        if sys.version_info >= (3, 12):
            package.extractall(destination, filter="data")
        else:
            package.extractall(destination)


def check_dependencies(program):
    """Fail if the program needs any shared library beyond the C library."""
    result = subprocess.run(["ldd", str(program)], capture_output=True, text=True)
    print(result.stdout + result.stderr, flush=True)
    if result.returncode != 0:
        # Fully static programs make ldd fail with "not a dynamic executable"; anything else is an error.
        if "not a dynamic executable" in result.stdout + result.stderr:
            return
        raise RuntimeError("ldd failed for " + str(program))
    unexpected = [line.strip() for line in result.stdout.splitlines()
                  if line.strip() and not ALLOWED_LIBRARIES.fullmatch(os.path.basename(line.split()[0]))]
    if unexpected:
        raise RuntimeError("{} needs more than the C library: {}".format(program.name, "; ".join(unexpected)))


def build_ntfsck(archive):
    """Build ntfsck from ntfsprogs-plus with libntfs linked in, and return the installed program."""
    prefix = ROOT / "build/linux-ntfsck"
    if prefix.exists():
        shutil.rmtree(prefix)
    with tempfile.TemporaryDirectory(prefix="xmd-ntfsck-") as temp:
        extract(archive, Path(temp))
        source = Path(temp).resolve() / ("ntfsprogs-plus-" + NTFSPROGS_VERSION)
        (source / "m4").mkdir(exist_ok=True)
        run(["./autogen.sh"], cwd=source)
        # --disable-library links libntfs into the tools instead of installing it as a shared library.
        # ntfsprogs-plus has no --disable-crypto option; its configure treats enable_crypto=no from the
        # environment as that choice, so a libgcrypt on the build machine can't leak into the program.
        run(["./configure", "--prefix=" + str(prefix), "--without-uuid", "--disable-library", "--disable-ldconfig"],
            cwd=source, env=dict(os.environ, enable_crypto="no"))
        run(["make", "-j" + str(min(os.cpu_count() or 2, 8))], cwd=source)
        run(["make", "install"], cwd=source)
        licenses = ROOT / "build/linux-tools/licenses/ntfsprogs-plus"
        licenses.mkdir(parents=True, exist_ok=True)
        for name in ("COPYING", "COPYING.LIB"):
            shutil.copyfile(source / name, licenses / name)
    program = next((path for path in (prefix / "sbin/ntfsck", prefix / "bin/ntfsck") if path.is_file()), None)
    if program is None:
        raise RuntimeError("ntfsck was not installed")
    if program.read_bytes()[:4] != b"\x7fELF":
        raise RuntimeError("ntfsck is not a plain executable (libtool wrapper?)")
    check_dependencies(program)
    return program


def bundled_library():
    """The real libntfs-3g.so.N file, copied under its SONAME so symlinks are not needed in the bundle."""
    library = NATIVE / "lib"
    links = sorted(path for path in library.glob("libntfs-3g.so.*") if re.fullmatch(r"libntfs-3g\.so\.\d+", path.name))
    if len(links) != 1:
        raise RuntimeError("Expected exactly one libntfs-3g.so.N in " + str(library))
    real = links[0].resolve(strict=True)
    header = subprocess.check_output(["readelf", "-d", real], text=True)
    if "[" + links[0].name + "]" not in header:
        raise RuntimeError("libntfs-3g SONAME does not match " + links[0].name)
    destination = ROOT / "build/linux-bundle" / links[0].name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(real, destination)
    destination.chmod(0o755)
    return destination


def source_archive(destination, *third_party):
    # Include tracked and untracked project source, respecting .gitignore.
    listing = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    with tarfile.open(destination, "w:gz") as archive:
        for name in sorted(set(os.fsdecode(item) for item in listing.split(b"\0") if item)):
            path = ROOT / name
            if path.is_file():
                archive.add(path, arcname="xbox-media-drive-helper/" + name, recursive=False)
        for source in third_party:
            archive.add(source, arcname="third-party/" + source.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-native-build", action="store_true", help="Reuse build/linux-native")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Linux AppImages must be built on Linux")
    architecture = platform.machine()
    if architecture not in APPIMAGETOOL:
        parser.error("Unsupported architecture: " + architecture)
    for command in ("cc", "make", "pkg-config", "curl", "readelf"):
        if not shutil.which(command):
            parser.error("Missing build tool: " + command + ". On Debian/Ubuntu: build-essential pkg-config curl.")
    TOOLS.mkdir(parents=True, exist_ok=True)
    python = bootstrap()
    licenses(python)
    ntfsprogs_source = TOOLS / ("ntfsprogs-plus-" + NTFSPROGS_VERSION + ".tar.gz")
    download(NTFSPROGS_URL, ntfsprogs_source, NTFSPROGS_SHA256)
    ntfsck = build_ntfsck(ntfsprogs_source)

    native = native_script()
    ntfs_source = TOOLS / ("ntfs-3g_ntfsprogs-" + native.VERSION + ".tgz")
    download(native.URLS, ntfs_source, native.SHA256)
    if not args.skip_native_build:
        if NATIVE.exists():
            shutil.rmtree(NATIVE)
        run([sys.executable, ROOT / "scripts/build-native.py", "--prefix", NATIVE, "--archive", ntfs_source])
    run([sys.executable, ROOT / "scripts/test-native.py", NATIVE])
    library = bundled_library()

    tool_name, tool_digest = APPIMAGETOOL[architecture]
    appimagetool = TOOLS / tool_name
    download("https://github.com/AppImage/appimagetool/releases/download/{}/{}".format(APPIMAGETOOL_VERSION, tool_name),
             appimagetool, tool_digest)
    appimagetool.chmod(0o755)
    runtime_name, runtime_digest = RUNTIME[architecture]
    runtime = TOOLS / runtime_name
    download("https://github.com/AppImage/type2-runtime/releases/download/{}/{}".format(RUNTIME_VERSION, runtime_name),
             runtime, runtime_digest)

    frozen = ROOT / "build/linux-dist"
    if frozen.exists():
        shutil.rmtree(frozen)
    run([python, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", frozen,
         "--workpath", ROOT / "build/pyinstaller/linux", ROOT / "packaging/linux/XboxMediaDrive.spec"],
        env=dict(os.environ, XMD_LINUX_NATIVE=str(library), XMD_LINUX_NTFSCK=str(ntfsck)))

    appdir = ROOT / "build/linux-appdir"
    if appdir.exists():
        shutil.rmtree(appdir)
    (appdir / "usr/lib").mkdir(parents=True)
    shutil.copytree(frozen / "XboxMediaDrive", appdir / "usr/lib/XboxMediaDrive", symlinks=True)
    apprun = appdir / "AppRun"
    apprun.write_text('#!/bin/sh\n'
                      'here=$(dirname "$(readlink -f "$0")")\n'
                      'exec "$here/usr/lib/XboxMediaDrive/XboxMediaDrive" "$@"\n')
    apprun.chmod(0o755)
    desktop = (ROOT / "packaging/linux/xbox-media-drive-helper.desktop").read_text()
    desktop = desktop.replace("Exec=xbox-media-drive-gui", "Exec=XboxMediaDrive")
    (appdir / "xbox-media-drive-helper.desktop").write_text(desktop + "X-AppImage-Version={}\n".format(APP_VERSION))
    shutil.copyfile(ROOT / "xbox_media_drive/icon.svg", appdir / "xbox-media-drive-helper.svg")
    icon(python, appdir)
    (appdir / ".DirIcon").symlink_to("xbox-media-drive-helper.png")

    dist = ROOT / "dist/linux"
    dist.mkdir(parents=True, exist_ok=True)
    image = dist / "Xbox-Media-Drive-Helper-{}-{}.AppImage".format(APP_VERSION, architecture)
    image.unlink(missing_ok=True)
    # appimagetool is itself an AppImage; extract-and-run avoids needing FUSE on the build machine.
    run([appimagetool, "--runtime-file", runtime, "--no-appstream", appdir, image],
        env=dict(os.environ, ARCH=architecture, APPIMAGE_EXTRACT_AND_RUN="1"))
    image.chmod(0o755)

    sources = dist / "Xbox-Media-Drive-Helper-{}-source.tar.gz".format(APP_VERSION)
    source_archive(sources, ntfs_source, ntfsprogs_source)
    checksums = [hashlib.sha256(file.read_bytes()).hexdigest() + "  " + file.name for file in (image, sources)]
    (dist / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n")
    print("AppImage and archives: " + str(dist))


if __name__ == "__main__":
    main()
