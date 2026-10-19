import contextlib
import io
import json
import unittest
from unittest.mock import patch

from xbox_media_drive import DriveError
from xbox_media_drive.cli import main


class CLITests(unittest.TestCase):
    def call(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_json_listing(self):
        with patch("xbox_media_drive.cli.discover", return_value=[]):
            status, output, error = self.call(["--json", "list"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), {"devices": []})
        self.assertEqual(error, "")

    def test_json_failures_stay_off_stdout(self):
        with patch("xbox_media_drive.cli.inspect", side_effect=DriveError("Unmount first")):
            status, output, error = self.call(["--json", "inspect", "/dev/test"])
        self.assertEqual(status, 1)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(error), {"error": "Unmount first"})

    def test_noninteractive_write_requires_yes(self):
        with patch("sys.stdin.isatty", return_value=False), patch("xbox_media_drive.cli.prepare") as prepare:
            status, output, error = self.call(["--json", "prepare", "/dev/test", "--backup", "host.sqlite"])
        prepare.assert_not_called()
        self.assertEqual(status, 1)
        self.assertIn("--yes", error)

    def test_inspection_blockers_and_partial_restore_have_exit_three(self):
        with patch("xbox_media_drive.cli.inspect", return_value={"ready": False}):
            self.assertEqual(self.call(["--json", "inspect", "/dev/test"])[0], 3)
        with patch("xbox_media_drive.cli.restore", return_value={"state": "restore-incomplete"}):
            self.assertEqual(self.call(["--json", "restore", "/dev/test", "--backup", "b.sqlite", "--yes"])[0], 3)

    def test_relative_library_path_is_refused(self):
        status, output, error = self.call(["--json", "--library", "untrusted.so", "doctor"])
        self.assertEqual(status, 1)
        self.assertIn("absolute", error)

    def test_append_passed_through(self):
        for append in (False, True):
            with self.subTest(append=append), patch("xbox_media_drive.cli.prepare", return_value={}) as prepare:
                arguments = ["prepare", "/dev/test", "--backup", "b.sqlite", "--yes"]
                status, _, _ = self.call(arguments + (["--append"] if append else []))
                self.assertEqual(status, 0)
                self.assertEqual(prepare.call_args.kwargs["append"], append)

    def test_append_confirmation_describes_existing_journal(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="/dev/test"), \
                patch("xbox_media_drive.cli.prepare", return_value={}):
            status, _, error = self.call(["prepare", "/dev/test", "--backup", "b.sqlite", "--append"])
        self.assertEqual(status, 0)
        self.assertIn("add to journal b.sqlite", error)
