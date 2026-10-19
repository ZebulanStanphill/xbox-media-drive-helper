import json
import os
import shlex
import unittest
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QListWidgetItem, QMessageBox
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

    def test_previously_prepared_inspection_explains_later_copies(self):
        self.window.action = "inspect"
        result = {"ready": True, "objects": 10, "changes_needed": 2, "previously_prepared": True,
                  "skipped_reparse_points": [], "blockers": []}
        self.window.completed(result)
        for message in (self.window.status.text(), self.window.activity.toPlainText()):
            self.assertIn("2 objects don't have app access yet", message)
            self.assertIn("Mac or Linux", message)
            self.assertIn("Prepare again", message)
        self.window.completed(dict(result, changes_needed=0))
        self.assertIn("App permissions are already present.", self.window.status.text())

    def test_previously_prepared_journal_choices(self):
        for choice in ("Add to Existing Journal…", "New Journal…", "Cancel"):
            with self.subTest(choice=choice):
                self.window.target.setText("/tmp/volume.img")
                self.window.inspection = {"ready": True, "changes_needed": 2, "previously_prepared": True,
                                          "identity": {"boot_sha256": "volume-id"}}
                chosen = []

                def choose(box):
                    self.assertEqual(box.defaultButton().text(), "Add to Existing Journal…")
                    self.assertIn("single journal that restores everything", box.informativeText())
                    chosen.append(next(button for button in box.buttons() if button.text() == choice))
                    return 0

                journal = "/tmp/existing.sqlite" if choice.startswith("Add") else "/tmp/new.sqlite"
                with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                        patch.object(QMessageBox, "exec", choose), \
                        patch.object(QMessageBox, "clickedButton", side_effect=lambda: chosen[-1]), \
                        patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question, \
                        patch.object(QFileDialog, "getOpenFileName", return_value=(journal, "")) as open_file, \
                        patch.object(QFileDialog, "getSaveFileName", return_value=(journal, "")) as save_file, \
                        patch.object(self.window, "start") as start:
                    self.window.prepare_volume()
                if choice == "Cancel":
                    start.assert_not_called()
                    open_file.assert_not_called()
                    save_file.assert_not_called()
                    continue
                arguments = start.call_args.args[1]
                self.assertEqual(arguments[arguments.index("--backup") + 1], journal)
                self.assertEqual("--append" in arguments, choice.startswith("Add"))
                if choice.startswith("Add"):
                    open_file.assert_called_once()
                    save_file.assert_not_called()
                    self.assertIn("Add 2 objects to journal: " + journal, question.call_args.args[2])
                else:
                    save_file.assert_called_once()
                    open_file.assert_not_called()

    def test_append_completion_explains_existing_journal_and_future_files(self):
        self.window.action = "prepare"
        with self.dialogs() as dialog:
            self.window.completed({"changed": 2, "journal": "/tmp/existing.sqlite", "appended": True})
            self.window.followups.pop(0)()
        self.assertEqual(dialog[0][0], "Objects added to existing journal")
        self.assertIn("to the existing journal. They now allow Xbox app access", dialog[0][1])
        self.assertIn("Mac or Linux PC won't have app access", dialog[0][1])
        self.assertIn("Files copied in Windows inherit access automatically", dialog[0][1])
        self.assertIn("Added 2 objects", self.window.status.text())

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

    @contextmanager
    def dialogs(self, on_show=None):
        """Record result dialogs as (headline, details) instead of showing them."""
        shown = []

        def show(box):
            shown.append((box.text(), box.informativeText()))
            if on_show:
                on_show(box)
            return 0

        with patch.object(QMessageBox, "exec", show):
            yield shown

    def wait_for_idle(self):
        deadline = time.monotonic() + 5
        while (self.window.worker is not None or self.window.followups) and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertIsNone(self.window.worker)

    def add_device(self, mounted, path="/dev/disk9s1"):
        device = {"path": path, "label": "Media", "size": 10**9, "filesystem": "NTFS", "mounted": mounted, "external": True}
        item = QListWidgetItem("Media")
        item.setData(Qt.UserRole, device)
        self.window.drives.addItem(item)
        self.window.drives.setCurrentItem(item)
        return device

    def test_unmount_button_follows_the_selected_device(self):
        self.assertIn(self.window.unmount_button, self.window.controls)
        self.assertFalse(self.window.unmount_button.isEnabled())
        self.add_device(mounted=True)
        self.assertTrue(self.window.unmount_button.isEnabled())
        self.window.worker = SimpleNamespace()
        self.window.update_unmount_button()
        self.assertFalse(self.window.unmount_button.isEnabled())
        self.window.worker = None
        self.window.drives.clear()
        self.add_device(mounted=False)
        self.assertFalse(self.window.unmount_button.isEnabled())

    def test_successful_unmount_refresh_preserves_selection_and_inspection(self):
        device = self.add_device(mounted=True)
        inspection = {"ready": True, "changes_needed": 1, "identity": {"boot_sha256": "volume-id"}}
        self.window.inspection = inspection
        other = dict(device, path="/dev/disk8s1")
        with patch("xbox_media_drive.gui.unmount", return_value=device["path"]) as unmount, \
                patch("xbox_media_drive.gui.discover", return_value=[SimpleNamespace(to_dict=lambda: other), SimpleNamespace(to_dict=lambda: dict(device, mounted=False))]), \
                self.dialogs() as dialog:
            self.window.unmount_selected()
            self.assertIsNotNone(self.window.worker)
            self.assertFalse(self.window.unmount_button.isEnabled())
            self.assertTrue(self.window.spinner.isVisible())
            self.assertIn("Unmounting", self.window.busy.text())
            self.wait_for_idle()
        self.assertFalse(self.window.spinner.isVisible())
        self.assertEqual(self.window.busy.text(), "")
        unmount.assert_called_once_with(device["path"])
        self.assertEqual(dialog, [])
        self.assertEqual(self.window.selected_device()["path"], device["path"])
        self.assertFalse(self.window.selected_device()["mounted"])
        self.assertEqual(self.window.target.text(), device["path"])
        self.assertIs(self.window.inspection, inspection)
        self.assertFalse(self.window.unmount_button.isEnabled())

    def test_mounted_target_is_unmounted_then_preparation_continues(self):
        device = self.add_device(mounted=True)
        self.window.inspection = {"ready": True, "changes_needed": 2, "identity": {"boot_sha256": "volume-id"}}
        journal = "/tmp/media-original.sqlite"
        result = json.dumps({"changed": 2, "journal": journal})
        with patch("xbox_media_drive.gui.is_mounted", side_effect=[True, False]), \
                patch("xbox_media_drive.gui.unmount", return_value=device["path"]) as unmount, \
                patch("xbox_media_drive.gui.discover", return_value=[SimpleNamespace(to_dict=lambda: dict(device, mounted=False))]) as discover, \
                patch("xbox_media_drive.gui.helper_command", side_effect=lambda arguments, elevate: ["helper"] + arguments), \
                patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(stdout=result, stderr="", returncode=0)) as helper, \
                patch.object(QFileDialog, "getSaveFileName", return_value=(journal, "")) as save, \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question, \
                self.dialogs() as dialog:
            self.window.prepare_volume()
            self.wait_for_idle()
        unmount.assert_called_once_with(device["path"])
        discover.assert_called_once()
        self.assertEqual([title for title, text in dialog], ["Preparation complete"])
        self.assertIn(journal, dialog[0][1])
        self.assertIn("inspect and prepare it again", dialog[0][1])
        save.assert_called_once()
        self.assertEqual([call.args[1] for call in question.call_args_list], ["Unmount volume", "Prepare for Xbox"])
        helper.assert_called_once()
        command = helper.call_args.args[0]
        self.assertEqual(command[1:3], ["prepare", device["path"]])
        self.assertEqual(command[command.index("--backup") + 1], journal)
        self.assertEqual(command[command.index("--expect-volume") + 1], "volume-id")
        self.assertIn("Permissions prepared", self.window.status.text())

    def test_mounted_previously_prepared_target_resumes_journal_choice_after_unmount(self):
        device = self.add_device(mounted=True)
        self.window.inspection = {"ready": True, "changes_needed": 1, "previously_prepared": True,
                                  "identity": {"boot_sha256": "volume-id"}}
        journal = "/tmp/existing.sqlite"
        result = json.dumps({"changed": 1, "journal": journal, "appended": True})
        selected = []

        def choose(box):
            if box.text() == "Choose a recovery journal":
                selected.append(box.defaultButton())
            return 0

        with patch("xbox_media_drive.gui.is_mounted", side_effect=[True, False]), \
                patch("xbox_media_drive.gui.unmount", return_value=device["path"]) as unmount, \
                patch("xbox_media_drive.gui.discover", return_value=[SimpleNamespace(to_dict=lambda: dict(device, mounted=False))]), \
                patch("xbox_media_drive.gui.helper_command", side_effect=lambda arguments, elevate: ["helper"] + arguments), \
                patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(stdout=result, stderr="", returncode=0)) as helper, \
                patch.object(QMessageBox, "exec", choose), \
                patch.object(QMessageBox, "clickedButton", side_effect=lambda: selected[-1]), \
                patch.object(QFileDialog, "getOpenFileName", return_value=(journal, "")) as open_file, \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.window.prepare_volume()
            self.wait_for_idle()
        unmount.assert_called_once_with(device["path"])
        open_file.assert_called_once()
        self.assertEqual(len(selected), 1)
        self.assertIn("--append", helper.call_args.args[0])
        self.assertIn(journal, helper.call_args.args[0])

    def test_recovery_journal_advice_only_follows_prepare_and_restore_errors(self):
        for action, expected in (("prepare", True), ("restore", True), ("inspect", False), ("list", False), ("unmount", False)):
            with self.subTest(action=action), self.dialogs() as dialog:
                self.window.action = action
                self.window.failed("It broke")
                self.assertEqual(dialog, [])
                self.window.followups.pop(0)()
                self.assertEqual("recovery journal" in dialog[0][1], expected)

    def test_mounted_target_is_unmounted_then_the_inspection_continues(self):
        self.add_device(mounted=True)
        self.window.target.setText("/dev/disk9s1")
        commands = []
        result = '{"ready": true, "objects": 1, "changes_needed": 1, "skipped_reparse_points": [], "blockers": []}'

        def helper(*args, **kwargs):
            commands.append(args[0])
            return SimpleNamespace(stdout=result, stderr="", returncode=0)

        with patch("xbox_media_drive.gui.is_mounted", side_effect=[True, False]), \
                patch("xbox_media_drive.gui.unmount", return_value="/dev/disk9s1") as unmount, \
                patch("xbox_media_drive.gui.discover", return_value=[]), \
                patch("xbox_media_drive.gui.helper_command", side_effect=lambda arguments, elevate: ["helper"] + arguments), \
                patch("xbox_media_drive.gui.subprocess.run", side_effect=helper), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question:
            self.window.inspect_volume()
            self.wait_for_idle()
        self.assertIn("Media is mounted. Unmount it now?", question.call_args.args[2])
        unmount.assert_called_once_with("/dev/disk9s1")
        self.assertEqual(len(commands), 1)
        self.assertIn("inspect", commands[0])
        self.assertTrue(self.window.inspection["ready"])
        self.assertIn("Unmounted /dev/disk9s1. The drive stays connected.", self.window.activity.toPlainText())

    def test_declining_the_unmount_prompt_does_nothing(self):
        self.window.target.setText("/dev/disk9s1")
        with patch("xbox_media_drive.gui.is_mounted", return_value=True), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.No), \
                patch.object(self.window, "start") as start:
            self.window.inspect_volume()
        start.assert_not_called()

    def test_unmounted_targets_and_images_skip_the_prompt(self):
        self.window.target.setText("/tmp/volume.img")
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch.object(QMessageBox, "question") as question, patch.object(self.window, "start") as start:
            self.window.inspect_volume()
        question.assert_not_called()
        self.assertEqual(start.call_args.args[0], "inspect")

    def test_disconnected_drive_error_refreshes_the_list_when_the_worker_finishes(self):
        self.window.target.setText("/dev/disk9s1")
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch("xbox_media_drive.gui.subprocess.run", side_effect=DriveError("/dev/disk9s1 is no longer connected.")), \
                patch("xbox_media_drive.gui.helper_command", return_value=["helper"]), \
                patch("xbox_media_drive.gui.discover", return_value=[]) as discover, \
                self.dialogs() as dialog:
            self.window.inspect_volume()
            self.wait_for_idle()
        discover.assert_called_once()
        self.assertEqual([title for title, text in dialog], ["Operation could not complete"])

    def test_successful_restore_reports_completion_after_the_window_is_idle(self):
        self.window.target.setText("/tmp/volume.img")
        result = json.dumps({"state": "restored", "restored": 3, "already_original": 1, "conflicts": [], "missing": []})
        busy = []

        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch("xbox_media_drive.gui.helper_command", return_value=["helper"]), \
                patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(stdout=result, stderr="", returncode=0)), \
                patch.object(QFileDialog, "getOpenFileName", return_value=("/tmp/journal.sqlite", "")), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                self.dialogs(lambda box: busy.append(self.window.spinner.isVisible() or not self.window.refresh.isEnabled())) as dialog:
            self.window.restore_volume()
            self.wait_for_idle()
        self.assertEqual([title for title, text in dialog], ["Restoration complete"])
        self.assertIn("3 objects restored, 1 were already original", dialog[0][1])
        self.assertEqual(busy, [False])
