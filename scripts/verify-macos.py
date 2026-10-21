#!/usr/bin/env python3
"""Check architecture, OS deployment targets, signatures and relocated app startup."""
import argparse
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MACH_MAGICS = {bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def version(value):
    return tuple(map(int, value.split("."))) + (0,) * (3 - len(value.split(".")))


def deployment_versions(output):
    found = []
    for block in output.split("Load command")[1:]:
        if re.search(r"\bcmd LC_BUILD_VERSION\b", block):
            match = re.search(r"\bminos\s+([\d.]+)", block)
        elif re.search(r"\bcmd LC_VERSION_MIN_MACOSX\b", block):
            match = re.search(r"\bversion\s+([\d.]+)", block)
        else:
            continue
        if not match:
            raise RuntimeError("Could not parse a Mach-O deployment target")
        found.append(match.group(1))
    return found


def output(command, **kwargs):
    return subprocess.check_output(list(map(str, command)), text=True, **kwargs)


def required_architectures(architecture):
    return ("arm64", "x86_64") if architecture == "universal2" else (architecture,)


def architectures_match(actual, architecture):
    return sorted(actual) == sorted(required_architectures(architecture))


def runtime_architectures(architectures):
    host = platform.machine()
    available = [host] if host in ("arm64", "x86_64") else []
    if host == "arm64" and "x86_64" in architectures:
        if subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode == 0:
            available.append("x86_64")
    return [architecture for architecture in architectures if architecture in available]


def verify(app, architecture, minimum="15.8"):
    app = Path(app).resolve(strict=True)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("LSMinimumSystemVersion") != minimum:
        raise RuntimeError("The bundle minimum OS does not match " + minimum)
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    files = []
    versions = set()
    for file in app.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        with file.open("rb") as handle:
            magic = handle.read(4)
        if magic not in MACH_MAGICS:
            continue
        architectures = output(["lipo", "-archs", file]).split()
        if not architectures_match(architectures, architecture):
            raise RuntimeError("Wrong architecture {} in {}".format(architectures, file))
        load_commands = output(["otool", "-l", file])
        targets = deployment_versions(load_commands)
        if not targets:
            raise RuntimeError("No macOS deployment target in " + str(file))
        if any(version(target) > version(minimum) for target in targets):
            raise RuntimeError("{} requires macOS {}, newer than {}".format(file, targets, minimum))
        # No dependencies or runtime search paths may refer to the build machine.
        dependencies = output(["otool", "-L", file]).splitlines()
        for line in dependencies:
            line = line.strip()
            if not line or line == str(file) + ":" or re.fullmatch(re.escape(str(file)) + r" \(architecture [^)]+\):", line):
                continue
            dependency = line.split(" (compatibility version", 1)[0]
            if not dependency.startswith(("/usr/lib/", "/System/Library/", "@rpath/", "@loader_path/", "@executable_path/")):
                raise RuntimeError("Nonportable dependency {} in {}".format(dependency, file))
        for block in load_commands.split("Load command")[1:]:
            if re.search(r"\bcmd LC_RPATH\b", block):
                match = re.search(r"\bpath\s+(.+?) \(offset", block)
                if match and not match.group(1).startswith(("@loader_path", "@executable_path", "/usr/lib", "/System/Library")):
                    raise RuntimeError("Nonportable runtime search path in " + str(file))
        files.append(str(file.relative_to(app)))
        versions.update(targets)
    if not files:
        raise RuntimeError("The application contains no native executable")
    required = required_architectures(architecture)
    runtime = runtime_architectures(required)
    if not runtime:
        raise RuntimeError("No requested architecture can be runtime tested on this host")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", "--all-architectures", str(app)], check=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "DYLD_", "XMD_", "QT_", "QML_"))}
    env.update(QT_QPA_PLATFORM="offscreen", PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    # Relocation plus a clean environment/cwd proves that neither a source checkout,
    # Python install, Homebrew nor build-only loader variables are required.
    with tempfile.TemporaryDirectory(prefix="xmd-independent-") as temp:
        destination = Path(temp) / "Independent location with spaces" / app.name
        destination.parent.mkdir()
        shutil.copytree(app, destination, symlinks=True)
        moved_executable = destination / executable.relative_to(app)
        for slice_architecture in runtime:
            prefix = ["/usr/bin/arch", "-" + slice_architecture, str(moved_executable)]
            doctor_process = subprocess.run(prefix + ["--cli", "--json", "doctor"], env=env, cwd=temp,
                                            capture_output=True, text=True, timeout=45, check=True)
            doctor = json.loads(doctor_process.stdout.strip())
            if destination.resolve() not in Path(doctor["library"]).resolve().parents:
                raise RuntimeError("Native library was loaded from outside the relocated app")
            for qt_platform in ("offscreen", "cocoa"):
                smoke_process = subprocess.run(prefix + ["--self-test"], env=dict(env, QT_QPA_PLATFORM=qt_platform), cwd=temp,
                                               capture_output=True, text=True, timeout=45)
                if smoke_process.returncode:
                    raise RuntimeError("Packaged {} startup failed: {}".format(qt_platform, smoke_process.stderr))
                smoke = json.loads(smoke_process.stdout.strip())
                if smoke["architecture"] != slice_architecture or smoke["gui"] is not True:
                    raise RuntimeError("The packaged GUI did not run as the requested architecture")
    return {"architecture": architecture, "architectures": list(required), "runtime_tested_architectures": runtime,
            "minimum_macos": minimum, "mach_o_files_checked": len(files),
            "native_deployment_targets": sorted(versions, key=version), "code_signature_verified": True,
            "relocated_cli_test": True, "relocated_qt_render_test": True, "relocated_cocoa_render_test": True,
            "tested_on_macos": platform.mac_ver()[0], "host_architecture": platform.machine(),
            "physical_intel_hardware_tested": platform.machine() == "x86_64",
            "runtime_on_macos_15_8_tested": platform.mac_ver()[0].startswith("15.8"),
            "physical_drive_operations_tested": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app")
    parser.add_argument("--arch", choices=("arm64", "x86_64", "universal2"), required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = verify(args.app, args.arch)
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
