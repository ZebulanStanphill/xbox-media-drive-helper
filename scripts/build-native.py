#!/usr/bin/env python3
"""Build a pinned NTFS-3G library and image tools locally, without FUSE or sudo."""
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

VERSION = "2026.9.28"
SHA256 = "350d9415c59f7c3fa74e23985ad2d56423c6a168337368f672e2e362d8f295c8"
URLS = ("https://tuxera.com/opensource/ntfs-3g_ntfsprogs-{}.tgz".format(VERSION),
        # Linux From Scratch's mirror at the OSU Open Source Lab: tuxera.com refuses some hosts,
        # such as GitHub's CI runners, with 403. The digest check applies to either copy.
        "https://ftp.osuosl.org/pub/blfs/conglomeration/ntfs-3g/ntfs-3g_ntfsprogs-{}.tgz".format(VERSION))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default=str(Path(__file__).resolve().parent.parent / ".native"))
    parser.add_argument("--arch", choices=("arm64", "x86_64"), help="macOS target architecture")
    parser.add_argument("--min-macos", default="12.0", help="Deployment target when using --arch")
    parser.add_argument("--archive", help="Use an existing source archive; its pinned digest is still checked")
    args = parser.parse_args()
    if args.arch and sys.platform != "darwin":
        parser.error("--arch is supported only for macOS builds")
    for tool in ("cc", "make", "pkg-config", "curl"):
        if not shutil.which(tool):
            parser.error("{} is required. On macOS: Xcode Command Line Tools and brew install pkgconf. "
                         "On Debian/Ubuntu: build-essential pkg-config.".format(tool))
    prefix = Path(args.prefix).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="xmd-build-") as temp:
        build_root = Path(temp).resolve()
        archive = build_root / "ntfs.tgz"
        print("Downloading NTFS-3G {}…".format(VERSION), flush=True)
        if args.archive:
            shutil.copyfile(args.archive, archive)
        else:
            for url in URLS:
                if subprocess.run(["curl", "--fail", "--location", "--retry", "2", "--connect-timeout", "20",
                                   "--max-time", "120", url, "--output", str(archive)]).returncode == 0:
                    break
                print("Download failed; trying the next source.", flush=True)
            else:
                raise RuntimeError("NTFS-3G could not be downloaded from any source.")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError("NTFS-3G archive checksum mismatch; refusing to build.")
        # The archive is pinned by digest; also reject paths escaping the build folder.
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                path = (build_root / member.name).resolve()
                if build_root not in path.parents or member.issym() or member.islnk():
                    raise RuntimeError("Unsafe path in source archive: " + member.name)
            if sys.version_info >= (3, 12):
                package.extractall(build_root, filter="data")
            else:
                package.extractall(build_root)
        source = build_root / ("ntfs-3g-" + VERSION)
        flags = ["./configure", "--prefix=" + str(prefix), "--exec-prefix=" + str(prefix),
                 "--disable-ntfs-3g", "--disable-crypto", "--without-uuid", "--enable-extras",
                 "--disable-plugins"]
        env = os.environ.copy()
        if args.arch:
            architecture_flags = "-arch {} -mmacosx-version-min={}".format(args.arch, args.min_macos)
            for key in ("CFLAGS", "LDFLAGS"):
                env[key] = (env.get(key, "") + " " + architecture_flags).strip()
            env["MACOSX_DEPLOYMENT_TARGET"] = args.min_macos
            if args.arch != platform.machine():
                flags.append("--host=" + args.arch + "-apple-darwin")
        subprocess.run(flags, cwd=source, check=True, env=env)
        subprocess.run(["make", "-j" + str(min(os.cpu_count() or 2, 8))], cwd=source, check=True, env=env)
        subprocess.run(["make", "install"], cwd=source, check=True, env=env)
    print("Installed offline NTFS library and tools in {}".format(prefix))
    print("Run: python3 scripts/test-native.py " + str(prefix))


if __name__ == "__main__":
    main()
