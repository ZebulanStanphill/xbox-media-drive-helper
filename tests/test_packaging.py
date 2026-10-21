import importlib.util
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xbox_media_drive.entry import main

spec = importlib.util.spec_from_file_location("verify_macos", Path(__file__).resolve().parent.parent / "scripts/verify-macos.py")
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class PackagingTests(unittest.TestCase):
    def test_packaged_cli_forwards_arguments_without_launching_gui(self):
        with patch("xbox_media_drive.cli.main", return_value=3) as cli:
            self.assertEqual(main(["--cli", "--json", "inspect", "/dev/test"]), 3)
        cli.assert_called_once_with(["--json", "inspect", "/dev/test"])

    def test_macho_versions_are_distinct_from_sdk_version(self):
        commands = """Load command 0
          cmd LC_BUILD_VERSION
          platform 1
          minos 12.0
          sdk 27.0
        Load command 1
          cmd LC_VERSION_MIN_MACOSX
          version 10.13
          sdk 15.0
        """
        self.assertEqual(verification.deployment_versions(commands), ["12.0", "10.13"])
        self.assertLess(verification.version("15.0"), verification.version("15.8"))
        self.assertGreater(verification.version("26.0"), verification.version("15.8"))

    def test_malformed_macho_target_is_refused(self):
        with self.assertRaises(RuntimeError):
            verification.deployment_versions("Load command 0\n cmd LC_BUILD_VERSION\n sdk 27.0")

    def test_universal_architectures_match_in_either_order(self):
        self.assertTrue(verification.architectures_match(["arm64", "x86_64"], "universal2"))
        self.assertTrue(verification.architectures_match(["x86_64", "arm64"], "universal2"))

    def test_universal_requires_exactly_both_slices(self):
        for architectures in ([], ["arm64"], ["x86_64"], ["arm64", "x86_64", "i386"],
                              ["arm64", "x86_64", "arm64"]):
            with self.subTest(architectures=architectures):
                self.assertFalse(verification.architectures_match(architectures, "universal2"))

    def test_thin_requires_exactly_its_own_slice(self):
        for architecture, other in (("arm64", "x86_64"), ("x86_64", "arm64")):
            with self.subTest(architecture=architecture):
                self.assertTrue(verification.architectures_match([architecture], architecture))
                for architectures in ([], [other], [architecture, other], [other, architecture],
                                      [architecture, architecture]):
                    self.assertFalse(verification.architectures_match(architectures, architecture))

    def test_runtime_slices_follow_host_and_rosetta_availability(self):
        for host, rosetta, expected in (("arm64", 0, ["arm64", "x86_64"]), ("arm64", 1, ["arm64"]),
                                       ("x86_64", 0, ["x86_64"]), ("unknown", 0, [])):
            with self.subTest(host=host, rosetta=rosetta), \
                    patch.object(verification.platform, "machine", return_value=host), \
                    patch.object(verification.subprocess, "run", return_value=subprocess.CompletedProcess([], rosetta)) as run:
                self.assertEqual(verification.runtime_architectures(("arm64", "x86_64")), expected)
                if host == "arm64":
                    run.assert_called_once_with(["/usr/bin/arch", "-x86_64", "/usr/bin/true"], capture_output=True)
                else:
                    run.assert_not_called()

    def test_runtime_slices_are_limited_to_requested_architectures(self):
        with patch.object(verification.platform, "machine", return_value="x86_64"), \
                patch.object(verification.subprocess, "run") as run:
            self.assertEqual(verification.runtime_architectures(("arm64",)), [])
            self.assertEqual(verification.runtime_architectures(("x86_64",)), ["x86_64"])
            run.assert_not_called()
        with patch.object(verification.platform, "machine", return_value="arm64"), \
                patch.object(verification.subprocess, "run") as run:
            self.assertEqual(verification.runtime_architectures(("arm64",)), ["arm64"])
            run.assert_not_called()

    def test_universal_verification_checks_relocated_startup_per_slice(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp).resolve() / "Example.app"
            executable = app / "Contents/MacOS/Example"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(bytes.fromhex("cafebabe"))
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "LSMinimumSystemVersion": "15.8", "CFBundleExecutable": "Example"}))

            def tool_output(command):
                if command[:2] == ["lipo", "-archs"]:
                    return "x86_64 arm64\n"
                if command[:2] == ["otool", "-l"]:
                    return "Load command 0\n cmd LC_BUILD_VERSION\n minos 15.0\n" * 2
                return "\n".join("{} (architecture {}):\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)"
                                 .format(executable, architecture) for architecture in ("x86_64", "arm64"))

            def run(command, **kwargs):
                data = {}
                if "--cli" in command:
                    data = {"library": str(Path(command[2]).parents[1] / "Frameworks/libntfs-3g.dylib")}
                elif "--self-test" in command:
                    data = {"architecture": command[1][1:], "gui": True}
                return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

            with patch.object(verification, "output", side_effect=tool_output), \
                    patch.object(verification.platform, "machine", return_value="arm64"), \
                    patch.object(verification.subprocess, "run", side_effect=run) as process:
                report = verification.verify(app, "universal2")
                calls = [call for call in process.call_args_list if "--cli" in call.args[0] or "--self-test" in call.args[0]]
                self.assertEqual(len(calls), 6)
                for architecture in ("arm64", "x86_64"):
                    slice_calls = [call for call in calls if call.args[0][1] == "-" + architecture]
                    self.assertEqual(len(slice_calls), 3)
                    self.assertTrue(all(call.args[0][0] == "/usr/bin/arch" for call in slice_calls))
                    self.assertEqual([call.kwargs["env"]["QT_QPA_PLATFORM"] for call in slice_calls],
                                     ["offscreen", "offscreen", "cocoa"])
                self.assertEqual(report["architecture"], "universal2")
                self.assertEqual(report["architectures"], ["arm64", "x86_64"])
                self.assertEqual(report["runtime_tested_architectures"], ["arm64", "x86_64"])
                self.assertFalse(report["physical_intel_hardware_tested"])
            with patch.object(verification, "output", side_effect=tool_output), \
                    patch.object(verification.platform, "machine", return_value="unknown"), \
                    patch.object(verification.subprocess, "run") as process:
                with self.assertRaisesRegex(RuntimeError, "No requested architecture"):
                    verification.verify(app, "universal2")
                process.assert_not_called()
