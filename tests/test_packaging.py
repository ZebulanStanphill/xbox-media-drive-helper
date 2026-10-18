import importlib.util
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
