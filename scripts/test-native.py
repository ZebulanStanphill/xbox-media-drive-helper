#!/usr/bin/env python3
"""Compile the image-only test harness and run the real NTFS integration suite."""
import argparse
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("prefix", help="NTFS-3G install prefix (with include, lib, bin and sbin)")
parser.add_argument("--compiler-arch", choices=("arm64", "x86_64"),
                    help="macOS compiler execution architecture (can differ from the test runtime)")
args = parser.parse_args()
prefix = Path(args.prefix).resolve()
root = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix="xmd-harness-") as temp:
    output = Path(temp) / ("images.dylib" if sys.platform == "darwin" else "images.so")
    command = ["cc", "-dynamiclib" if sys.platform == "darwin" else "-shared", "-fPIC",
               "-I" + str(prefix / "include"), str(root / "tests/image_session.c"),
               "-L" + str(prefix / "lib"), "-Wl,-rpath," + str(prefix / "lib"), "-lntfs-3g", "-o", str(output)]
    if sys.platform == "darwin":
        command.extend(["-arch", platform.machine()])
        command = ["/usr/bin/arch", "-" + (args.compiler_arch or platform.machine())] + command
    subprocess.run(command, check=True)
    env = dict(os.environ, XMD_TEST_PREFIX=str(prefix), XMD_TEST_HARNESS=str(output))
    raise SystemExit(subprocess.call([sys.executable, "-m", "unittest", "tests.test_native_images", "-v"], cwd=root, env=env))
