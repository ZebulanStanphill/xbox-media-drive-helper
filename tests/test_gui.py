import json
import os
import shlex
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
    from xbox_media_drive.gui import MainWindow, STYLE, helper_command, parse_helper_output
    HAS_QT = True
except ImportError:
    HAS_QT = False

from xbox_media_drive import DriveError


@unittest.skipUnless(HAS_QT, "Install the gui extra to test the desktop interface")
class GUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.window = MainWindow(auto_refresh=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_real_inspection_result_enables_action_and_target_changes_invalidate(self):
        self.window.action = "inspect"
        self.window.completed({"ready": True, "objects": 10, "changes_needed": 5,
                               "skipped_reparse_points": [], "blockers": []})
        self.assertTrue(self.window.prepare_button.isEnabled())
        self.window.target.setText("/dev/different")
        self.assertFalse(self.window.prepare_button.isEnabled())
        self.assertIsNone(self.window.inspection)

    def test_unsupported_acl_blocks_preparation(self):
        self.window.action = "inspect"
        self.window.completed({"ready": False, "objects": 10, "changes_needed": 2,
                               "skipped_reparse_points": [], "blockers": [{"path": "/Movies", "reason": "deny ACE"}]})
        self.assertFalse(self.window.prepare_button.isEnabled())
        self.assertIn("deny ACE", self.window.status.text())

    def test_controls_do_not_collapse_at_smallest_window_size(self):
        self.window.resize(900, 780)
        self.app.processEvents()
        for control in (self.window.access, self.window.check, self.window.prepare_button):
            self.assertGreaterEqual(control.height(), 30)

    def test_mac_authorization_quotes_shell_arguments(self):
        target = "/tmp/drive'; touch SHOULD_NOT_EXIST; echo '.img"
        with patch("xbox_media_drive.gui.os.getuid", return_value=501), patch("sys.platform", "darwin"):
            command = helper_command(["inspect", target], True)
        self.assertEqual(command[0], "osascript")
        # Decode the AppleScript string, then tokenize its shell command.
        shell = json.loads(command[2].removeprefix("do shell script ").removesuffix(" with administrator privileges"))
        self.assertEqual(shlex.split(shell.split(" 2>&1;")[0])[-1], target)

    def test_helper_handles_success_failures_and_partial_results(self):
        self.assertEqual(parse_helper_output('{"state":"restored"}', "", 0)["state"], "restored")
        self.assertEqual(parse_helper_output('{"state":"restore-incomplete"}', "", 3)["state"], "restore-incomplete")
        with self.assertRaisesRegex(DriveError, "Unmount"):
            parse_helper_output("", '{"error":"Unmount"}', 1)

    def test_frozen_helper_dispatches_to_bundled_cli(self):
        with patch.object(__import__("sys"), "frozen", True, create=True):
            command = helper_command(["inspect", "/dev/test"], elevate=False)
        self.assertEqual(command[1:], ["--cli", "--json", "inspect", "/dev/test"])
