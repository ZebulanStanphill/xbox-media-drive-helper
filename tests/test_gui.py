import json
import os
import shlex
import tempfile
import unittest
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QAction, QWheelEvent
    from PySide6.QtWidgets import QApplication, QFileDialog, QListWidgetItem, QMessageBox, QStyle
    from xbox_media_drive.gui import MainWindow, PRIVACY_SETTINGS, SOURCE_URL, app_icon, configure, helper_command, parse_helper_output
    HAS_QT = True
except ImportError:
    HAS_QT = False

from xbox_media_drive import DriveError, __version__
from xbox_media_drive.fsck import CHECK_PROBLEMS_NOTE
from xbox_media_drive.operations import CLEAR_DIRTY_CONFIRMATION, DIRTY_WARNING


@unittest.skipUnless(HAS_QT, "Install the gui extra to test the desktop interface")
class GUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        configure(cls.app)

    def setUp(self):
        self.probe = patch("xbox_media_drive.gui.mac_full_disk_access", return_value=None).start()
        self.addCleanup(patch.stopall)
        self.window = MainWindow(auto_refresh=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_question_dialog_uses_app_icon_through_stylesheet(self):
        self.assertFalse(app_icon().isNull())
        for kind in (QMessageBox.Question, QMessageBox.Information, QMessageBox.Warning):
            with self.subTest(kind=kind):
                box = QMessageBox(kind, "Unmount volume", "Unmount it now?",
                                  QMessageBox.Yes | QMessageBox.No, self.window)
                pixmap = box.iconPixmap()
                expected = app_icon().pixmap(pixmap.size()).toImage()
                self.assertEqual(pixmap.toImage() == expected, kind == QMessageBox.Question)
                box.deleteLater()
        # The static question convenience function creates a box with this same icon enum.
        self.assertEqual(self.app.style().standardIcon(QStyle.SP_MessageBoxQuestion).cacheKey(),
                         app_icon().cacheKey())

    def test_disk_access_notice_at_construction(self):
        for result in (True, None, False):
            with self.subTest(result=result):
                self.probe.return_value = result
                window = MainWindow(auto_refresh=False)
                self.assertEqual(window.disk_access_notice.isHidden(), result is not False)
                window.close()
                window.deleteLater()

    def test_disk_access_notice_rechecks_on_activation(self):
        self.probe.return_value = False
        self.window.application_state_changed(Qt.ApplicationActive)
        self.assertTrue(self.window.disk_access_notice.isVisible())
        self.probe.return_value = True
        self.probe.reset_mock()
        self.window.application_state_changed(Qt.ApplicationInactive)
        self.probe.assert_not_called()
        self.window.application_state_changed(Qt.ApplicationActive)
        self.probe.assert_called_once_with()
        self.assertTrue(self.window.disk_access_notice.isHidden())

    def test_disk_access_notice_opens_settings(self):
        with patch("xbox_media_drive.gui.subprocess.run") as run:
            self.window.privacy_settings.click()
            run.assert_called_once_with(["open", PRIVACY_SETTINGS])

    def test_recovery_suffix_only_when_permissions_might_have_changed(self):
        for action in ("prepare", "restore", "inspect"):
            for unchanged in (False, True):
                with self.subTest(action=action, unchanged=unchanged):
                    self.window.action = action
                    message = "Could not open. No permissions changed." if unchanged else "Write failed."
                    with patch.object(self.window, "report") as report:
                        self.window.failed(message)
                    shown = report.call_args.args[2]
                    self.assertEqual("If preparation started" in shown,
                                     action in ("prepare", "restore") and not unchanged)

    def test_disk_access_failure_shows_notice_and_keeps_dialog(self):
        with patch("sys.platform", "darwin"), patch.object(self.window, "report") as report:
            self.window.failed("Enable Full Disk Access")
            self.assertTrue(self.window.disk_access_notice.isVisible())
            report.assert_called_once_with(QMessageBox.Warning, "Disk access needed",
                                           "Enable Full Disk Access", settings=True)

    def wheel(self, widget, delta):
        position = widget.rect().center()
        event = QWheelEvent(QPointF(position), QPointF(widget.mapToGlobal(position)),
                            QPoint(), QPoint(0, delta), Qt.NoButton, Qt.NoModifier,
                            Qt.NoScrollPhase, False)
        QApplication.sendEvent(widget, event)
        return event

    def test_scrolling_never_changes_access_or_tabs(self):
        for widget in (self.window.access, self.window.tabs.tabBar()):
            for index, delta in ((0, -120), (1, 120)):
                with self.subTest(widget=type(widget).__name__, delta=delta):
                    widget.setCurrentIndex(index)
                    event = self.wheel(widget, delta)
                    self.assertEqual(widget.currentIndex(), index)
                    self.assertFalse(event.isAccepted())
        # The replacement tab bar must look like the one QTabWidget creates itself.
        self.assertFalse(self.window.tabs.tabBar().drawBase())

    def test_real_inspection_result_enables_action_and_target_changes_invalidate(self):
        self.window.action = "inspect"
        self.window.completed({"ready": True, "objects": 10, "changes_needed": 5,
                               "skipped_reparse_points": [], "blockers": []})
        self.assertTrue(self.window.prepare_button.isEnabled())
        self.window.set_target("/dev/different")
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
                self.window.set_target("/tmp/volume.img")
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

    def test_new_journal_dialog_refuses_an_existing_file_before_starting(self):
        self.window.set_target("/tmp/volume.img")
        self.window.inspection = {"ready": True, "changes_needed": 2, "identity": {"boot_sha256": "volume-id"}}
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as existing, \
                patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch.object(QFileDialog, "getSaveFileName", return_value=(existing.name, "")) as save, \
                patch.object(QMessageBox, "warning") as warning, \
                patch.object(QMessageBox, "question") as question, \
                patch.object(self.window, "start") as start:
            self.window.prepare_volume()
        self.assertEqual(save.call_args.kwargs["options"], QFileDialog.DontConfirmOverwrite)
        self.assertIn("never overwritten", warning.call_args.args[2])
        question.assert_not_called()
        start.assert_not_called()

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

    def test_dirty_warning_display_confirmation_and_clear(self):
        self.window.set_target("/tmp/volume.img")
        result = {"ready": True, "changes_needed": 1, "objects": 1,
                  "skipped_reparse_points": [], "blockers": [], "dirty": True,
                  "identity": {"boot_sha256": "volume-id"}}
        self.window.action = "inspect"
        self.window.completed(result)
        self.assertFalse(self.window.dirty_warning.isHidden())
        self.assertFalse(self.window.clear_dirty_button.isHidden())
        self.assertEqual(self.window.dirty_warning.text(), DIRTY_WARNING)
        self.assertTrue(self.window.prepare_button.isEnabled())
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch.object(QFileDialog, "getSaveFileName", return_value=("/tmp/new-journal.sqlite", "")), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.No) as question:
            self.window.prepare_volume()
        self.assertIn(DIRTY_WARNING, question.call_args.args[2])
        self.window.completed(dict(result, dirty=False))
        self.assertTrue(self.window.dirty_warning.isHidden())
        self.assertTrue(self.window.clear_dirty_button.isHidden())
        self.window.completed(result)
        self.window.set_target("/tmp/another.img")
        self.assertTrue(self.window.dirty_warning.isHidden())
        self.assertTrue(self.window.clear_dirty_button.isHidden())
        self.window.completed(result)
        self.window.invalidate()
        self.assertTrue(self.window.dirty_warning.isHidden())
        self.assertTrue(self.window.clear_dirty_button.isHidden())

    def test_clear_dirty_requires_confirmation_and_runs_cli(self):
        self.window.set_target("/tmp/volume.img")
        self.window.library.setText("/tmp/libntfs.dylib")
        self.window.inspection = {"dirty": True}
        for answer in (QMessageBox.No, QMessageBox.Yes):
            with self.subTest(answer=answer), patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                    patch.object(QMessageBox, "question", return_value=answer) as question, \
                    patch.object(self.window, "start") as start:
                self.window.clear_dirty_volume()
                self.assertIn(CLEAR_DIRTY_CONFIRMATION, question.call_args.args[2])
                self.assertIn("/tmp/volume.img", question.call_args.args[2])
                if answer == QMessageBox.Yes:
                    start.assert_called_once_with("clear-dirty", ["--library", "/tmp/libntfs.dylib",
                                                               "clear-dirty", "/tmp/volume.img", "--yes"])
                else:
                    start.assert_not_called()
        self.window.invalidate()
        with patch.object(QMessageBox, "question") as question:
            self.window.clear_dirty_volume()
        question.assert_not_called()

    def test_clear_dirty_success_reinspects_after_worker_finishes(self):
        self.window.set_target("/tmp/volume.img")
        self.window.action = "clear-dirty"
        self.window.completed({"target": "/tmp/volume.img", "dirty": False, "cleared": True})
        self.assertTrue(self.window.clear_dirty_button.isHidden())
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), patch.object(self.window, "start") as start:
            self.window.followups.pop(0)()
        start.assert_called_once_with("inspect", ["inspect", "/tmp/volume.img", "--access", "modify"])

    def test_clear_dirty_button_disabled_while_worker_runs(self):
        self.window.set_target("/tmp/volume.img")
        self.window.clear_dirty_button.show()
        with patch("xbox_media_drive.gui.Worker") as worker:
            self.window.start("clear-dirty", ["clear-dirty", "/tmp/volume.img", "--yes"])
            self.assertFalse(self.window.clear_dirty_button.isEnabled())
            worker.return_value.start.assert_called_once_with()
            self.window.finished()
        self.assertTrue(self.window.clear_dirty_button.isEnabled())

    def test_check_filesystem_button_only_where_ntfsck_is_installed(self):
        self.assertTrue(self.window.check_filesystem_button.isHidden())
        for found in ("/usr/sbin/ntfsck", None):
            with self.subTest(found=found), patch("xbox_media_drive.gui.find_ntfsck", return_value=found):
                window = MainWindow(auto_refresh=False)
                window.show()
                self.assertEqual(window.check_filesystem_button.isVisible(), found is not None)
                if found:
                    self.assertEqual(window.check_filesystem_button.text(), "Check Filesystem…")
                    window.check_filesystem_button.click()
                    self.assertIn("Choose a volume first.", window.activity.toPlainText())
                window.close()
                window.deleteLater()

    def test_check_filesystem_requires_confirmation_and_runs_cli(self):
        self.window.set_target("/tmp/volume.img")
        self.window.library.setText("/tmp/libntfs.dylib")
        for answer in (QMessageBox.No, QMessageBox.Yes):
            with self.subTest(answer=answer), patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                    patch.object(QMessageBox, "question", return_value=answer) as question, \
                    patch.object(self.window, "start") as start:
                self.window.check_filesystem_volume()
                self.assertEqual(question.call_args.args[1], "Check Filesystem")
                for text in ("/tmp/volume.img", "ntfsck", "read-only", "never changes the drive", "take a while"):
                    self.assertIn(text, question.call_args.args[2])
                if answer == QMessageBox.Yes:
                    start.assert_called_once_with("check", ["--library", "/tmp/libntfs.dylib", "check", "/tmp/volume.img"])
                else:
                    start.assert_not_called()
        self.window.set_target("")
        with patch.object(QMessageBox, "question") as question, patch.object(self.window, "start") as start:
            self.window.check_filesystem_volume()
        question.assert_not_called()
        start.assert_not_called()

    def test_mounted_target_is_unmounted_then_the_filesystem_check_runs(self):
        device = self.add_device(mounted=True)
        self.window.set_target("/dev/disk9s1")
        commands = []
        result = json.dumps({"target": "/dev/disk9s1", "verdict": "clean", "output": ""})

        def helper(*args, **kwargs):
            commands.append(args[0])
            return SimpleNamespace(stdout=result, stderr="", returncode=0)

        with patch("xbox_media_drive.gui.is_mounted", side_effect=[True, False]), \
                patch("xbox_media_drive.gui.unmount", return_value="/dev/disk9s1") as unmount, \
                patch("xbox_media_drive.gui.discover", return_value=[SimpleNamespace(to_dict=lambda: dict(device, mounted=False))]), \
                patch("xbox_media_drive.gui.helper_command", side_effect=lambda arguments, elevate: ["helper"] + arguments), \
                patch("xbox_media_drive.gui.subprocess.run", side_effect=helper), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question, \
                self.dialogs() as dialog:
            self.window.check_filesystem_volume()
            self.wait_for_idle()
        self.assertEqual([call.args[1] for call in question.call_args_list], ["Check Filesystem", "Unmount volume"])
        unmount.assert_called_once_with("/dev/disk9s1")
        self.assertEqual(commands, [["helper", "check", "/dev/disk9s1"]])
        self.assertEqual([title for title, text in dialog], ["No problems found"])
        self.assertEqual(self.window.filesystem_check["clean"], True)

    def test_filesystem_check_results_are_logged_and_shown(self):
        self.window.set_target("/tmp/volume.img")
        self.window.action = "check"
        details = []
        with self.dialogs(lambda box: details.append(box.detailedText())) as dialog:
            self.window.completed({"verdict": "clean", "exit_code": 0, "output": "", "target": "/tmp/volume.img"})
            self.assertIn("ntfsck found no problems.", self.window.activity.toPlainText())
            self.assertIn("ntfsck found no problems.", self.window.status.text())
            self.window.followups.pop(0)()
            self.window.completed({"verdict": "problems", "exit_code": 4, "output": "Inode 5 is corrupt\nsecond line",
                                   "target": "/tmp/volume.img"})
            self.assertIn("ntfsck found problems.", self.window.activity.toPlainText())
            self.window.followups.pop(0)()
        self.assertEqual(dialog, [("No problems found", "ntfsck found no problems."),
                                  ("Problems found", CHECK_PROBLEMS_NOTE)])
        self.assertEqual(details, ["", "Inode 5 is corrupt\nsecond line"])
        # An existing inspection keeps its own status and the Prepare gate.
        self.window.inspection = {"ready": True, "changes_needed": 1}
        self.window.prepare_button.setEnabled(True)
        self.window.status.setText("1 objects inspected")
        self.window.completed({"verdict": "clean", "exit_code": 0, "output": ""})
        self.assertEqual(self.window.status.text(), "1 objects inspected")
        self.assertTrue(self.window.prepare_button.isEnabled())

    def test_filesystem_check_failure_is_reported_like_other_failures(self):
        self.window.set_target("/tmp/volume.img")
        message = "ntfsck couldn't check the volume (exit 1).\nntfsck: not an NTFS volume"
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch("xbox_media_drive.gui.helper_command", return_value=["helper"]), \
                patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(
                    stdout="", stderr=json.dumps({"error": message}), returncode=1)), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                self.dialogs() as dialog:
            self.window.check_filesystem_volume()
            self.wait_for_idle()
        self.assertEqual(dialog, [("Operation could not complete", message)])
        self.assertIsNone(self.window.filesystem_check)

    def test_filesystem_check_button_disabled_while_worker_runs(self):
        self.window.set_target("/tmp/volume.img")
        self.window.check_filesystem_button.show()
        with patch("xbox_media_drive.gui.Worker") as worker:
            self.window.start("check", ["check", "/tmp/volume.img"])
            self.assertFalse(self.window.check_filesystem_button.isEnabled())
            self.assertEqual(self.window.busy.text(),
                             "Checking the filesystem on /tmp/volume.img… Enter your administrator password if asked.")
            worker.return_value.start.assert_called_once_with()
            self.window.finished()
        self.assertTrue(self.window.check_filesystem_button.isEnabled())

    def clear_dirty_prompt(self):
        """Return the Clear Dirty Flag question's text and default button for the current state."""
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.No) as question:
            self.window.clear_dirty_volume()
        self.assertEqual(question.call_args.args[3], QMessageBox.Yes | QMessageBox.No)
        return question.call_args.args[2], question.call_args.args[4]

    def finish_filesystem_check(self, verdict, time="09:41"):
        self.window.action = "check"
        with patch("xbox_media_drive.gui.datetime") as clock:
            clock.now.return_value.strftime.return_value = time
            self.window.completed({"verdict": verdict, "exit_code": 0 if verdict == "clean" else 4, "output": ""})
        self.window.followups.clear()

    def test_remembered_check_result_shapes_the_clear_dirty_confirmation(self):
        self.window.set_target("/tmp/volume.img")
        self.window.inspection = {"dirty": True, "identity": {"boot_sha256": "volume-id"}}
        message, default = self.clear_dirty_prompt()
        self.assertTrue(message.endswith(CLEAR_DIRTY_CONFIRMATION))
        self.assertEqual(default, QMessageBox.NoButton)
        self.finish_filesystem_check("clean")
        message, default = self.clear_dirty_prompt()
        self.assertTrue(message.endswith(CLEAR_DIRTY_CONFIRMATION + "\n\nntfsck found no problems on this drive at 09:41."))
        self.assertEqual(default, QMessageBox.NoButton)
        self.finish_filesystem_check("problems", "10:02")
        message, default = self.clear_dirty_prompt()
        self.assertTrue(message.endswith(CLEAR_DIRTY_CONFIRMATION + "\n\nntfsck found problems on this drive at 10:02; "
                                         "don't clear the flag, repair it in Windows instead."))
        self.assertEqual(default, QMessageBox.No)

    def finish_inspection(self, volume="volume-id"):
        self.window.action = "inspect"
        self.window.completed({"ready": True, "changes_needed": 1, "objects": 1, "skipped_reparse_points": [],
                               "blockers": [], "dirty": True, "identity": {"boot_sha256": volume}})

    def remembered_text(self):
        return self.clear_dirty_prompt()[0].replace(CLEAR_DIRTY_CONFIRMATION, "")

    def test_remembered_check_survives_inspection_but_not_a_changed_target_or_volume(self):
        self.window.set_target("/tmp/volume.img")
        # A check made before inspecting applies to the volume inspected afterwards.
        self.finish_filesystem_check("problems")
        self.finish_inspection()
        self.assertIn("ntfsck found problems", self.remembered_text())
        self.window.invalidate()
        self.assertIsNotNone(self.window.filesystem_check)
        # Selecting the same target again is not a change.
        self.window.set_target("/tmp/volume.img")
        self.assertIsNotNone(self.window.filesystem_check)
        # A check made on an inspected volume is not used for a different volume at the same path.
        self.finish_inspection()
        self.finish_filesystem_check("clean")
        self.finish_inspection("other-volume")
        self.assertNotIn("ntfsck", self.remembered_text())
        self.finish_inspection()
        self.assertIn("ntfsck found no problems", self.remembered_text())
        # A different target forgets it, even when the original comes back.
        self.window.set_target("/tmp/another.img")
        self.assertIsNone(self.window.filesystem_check)
        self.window.set_target("/tmp/volume.img")
        self.finish_inspection()
        self.assertNotIn("ntfsck", self.remembered_text())

    def test_remembered_check_is_forgotten_after_changes_to_the_drive(self):
        for action, result in (("clear-dirty", {"cleared": True}),
                               ("prepare", {"changed": 1, "journal": "/tmp/j.sqlite"}),
                               ("restore", {"state": "restored", "restored": 1, "already_original": 0,
                                            "conflicts": [], "missing": []})):
            with self.subTest(action=action):
                self.window.set_target("/tmp/volume.img")
                self.finish_filesystem_check("clean")
                self.assertIsNotNone(self.window.filesystem_check)
                self.window.action = action
                self.window.completed(result)
                self.window.followups.clear()
                self.assertIsNone(self.window.filesystem_check)

    def test_backup_warning_is_always_visible_and_repeated_before_changes(self):
        self.assertTrue(self.window.backup_warning.isVisible())
        self.assertIn("Back up the drive before using this app", self.window.backup_warning.text())
        self.window.set_target("/tmp/volume.img")
        self.window.inspection = {"ready": True, "changes_needed": 1, "identity": {"boot_sha256": "volume-id"}}
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch.object(QFileDialog, "getSaveFileName", return_value=("/tmp/new-journal.sqlite", "")), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.No) as question:
            self.window.prepare_volume()
        self.assertIn("Back up the drive first", question.call_args.args[2])

    def test_controls_do_not_collapse_at_smallest_window_size(self):
        self.window.resize(900, 780)
        self.app.processEvents()
        for control in (self.window.access, self.window.check, self.window.prepare_button):
            self.assertGreaterEqual(control.height(), 30)
        self.assertLess(self.window.tabs.geometry().bottom(), self.window.library.geometry().top())

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

    def add_device(self, mounted, path="/dev/disk9s1", disk="/dev/disk9"):
        device = {"path": path, "label": "Media", "size": 10**9, "filesystem": "NTFS", "mounted": mounted, "external": True}
        device["disk"] = disk
        item = QListWidgetItem("Media")
        item.setData(Qt.UserRole, device)
        self.window.drives.addItem(item)
        self.window.drives.setCurrentItem(item)
        return device

    def open_image(self, path):
        with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")):
            self.window.select_image()

    def test_open_image_selects_list_item_and_invalidates_inspection(self):
        self.add_device(mounted=True)
        self.window.inspection = {"ready": True}
        self.window.prepare_button.setEnabled(True)
        with tempfile.NamedTemporaryFile(suffix=".img") as image:
            image.write(b"NTFS")
            image.flush()
            self.open_image(image.name)
            item = self.window.drives.item(0)
            self.assertIs(self.window.drives.currentItem(), item)
            self.assertEqual(item.text(), "{}\nNTFS image  ·  0.0 GB\n{}".format(os.path.basename(image.name), image.name))
            self.assertEqual(item.data(Qt.UserRole), {
                "path": image.name, "label": os.path.basename(image.name), "size": 4, "filesystem": "NTFS",
                "mounted": False, "external": False, "disk": "", "image": True})
            self.assertEqual(self.window.target_path, image.name)
            self.assertEqual(self.window.drives.count(), 2)
            self.assertIsNone(self.window.inspection)
            self.assertFalse(self.window.prepare_button.isEnabled())
            self.assertFalse(self.window.unmount_button.isEnabled())
            self.window.tabs.setCurrentIndex(2)
            self.assertFalse(self.window.unmount_button.isEnabled())

    def test_open_second_image_replaces_first_and_keeps_devices(self):
        device = self.add_device(mounted=False)
        with tempfile.NamedTemporaryFile() as first, tempfile.NamedTemporaryFile() as second:
            self.open_image(first.name)
            self.open_image(second.name)
            self.assertEqual(self.window.image_path, second.name)
            self.assertEqual(self.window.target_path, second.name)
            self.assertEqual([data["path"] for data in self.window.listed_devices()], [second.name, device["path"]])
            self.assertIs(self.window.drives.currentItem(), self.window.drives.item(0))

    def test_refresh_keeps_image_first_and_selected(self):
        device = self.add_device(mounted=False)
        with tempfile.NamedTemporaryFile() as image:
            self.open_image(image.name)
            self.window.action = "list"
            self.window.completed({"devices": [device]})
            self.assertEqual([data["path"] for data in self.window.listed_devices()], [image.name, device["path"]])
            self.assertIs(self.window.drives.currentItem(), self.window.drives.item(0))
            self.assertEqual(self.window.target_path, image.name)

    def test_refresh_forgets_missing_image_and_clears_target(self):
        device = self.add_device(mounted=False)
        with tempfile.NamedTemporaryFile() as image:
            self.open_image(image.name)
        self.window.action = "list"
        self.window.completed({"devices": [device]})
        self.assertEqual(self.window.image_path, "")
        self.assertEqual(self.window.target_path, "")
        self.assertEqual(self.window.listed_devices(), [device])
        self.assertIsNone(self.window.drives.currentItem())

    def test_refresh_clears_disappeared_device_and_disables_prepare(self):
        self.add_device(mounted=False)
        self.window.inspection = {"ready": True}
        self.window.prepare_button.setEnabled(True)
        self.window.action = "list"
        other = {"path": "/dev/disk8s1", "label": "Other", "size": 10**9, "filesystem": "NTFS",
                 "mounted": False, "external": True, "disk": "/dev/disk8"}
        self.window.completed({"devices": [other]})
        self.assertEqual(self.window.target_path, "")
        self.assertIsNone(self.window.inspection)
        self.assertFalse(self.window.prepare_button.isEnabled())
        self.assertEqual(self.window.status.text(), "Select a volume, then inspect its permissions.")

    def test_no_devices_placeholder_preserves_status_with_or_without_image(self):
        self.add_device(mounted=False)
        self.window.action = "list"
        for has_image in (False, True):
            with self.subTest(image=has_image), tempfile.NamedTemporaryFile() as image:
                if has_image:
                    self.open_image(image.name)
                self.window.completed({"devices": []})
                self.assertIn("No external volumes found.", self.window.status.text())
                self.assertEqual(self.window.drives.count(), 2 if has_image else 1)
                item = self.window.drives.item(self.window.drives.count() - 1)
                self.assertIn("No external volumes", item.text())
                self.assertEqual(item.flags(), Qt.NoItemFlags)
                self.assertEqual(self.window.target_path, image.name if has_image else "")

    def test_setting_unchanged_target_preserves_inspection(self):
        self.window.set_target("/tmp/volume.img")
        inspection = {"ready": True}
        self.window.inspection = inspection
        self.window.prepare_button.setEnabled(True)
        self.window.set_target("/tmp/volume.img")
        self.assertIs(self.window.inspection, inspection)
        self.assertTrue(self.window.prepare_button.isEnabled())

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
        self.window.prepare_button.setEnabled(True)
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
        self.assertEqual(self.window.target_path, device["path"])
        self.assertIs(self.window.inspection, inspection)
        self.assertTrue(self.window.prepare_button.isEnabled())
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
        device = self.add_device(mounted=True)
        self.window.set_target("/dev/disk9s1")
        commands = []
        result = '{"ready": true, "objects": 1, "changes_needed": 1, "skipped_reparse_points": [], "blockers": []}'

        def helper(*args, **kwargs):
            commands.append(args[0])
            return SimpleNamespace(stdout=result, stderr="", returncode=0)

        with patch("xbox_media_drive.gui.is_mounted", side_effect=[True, False]), \
                patch("xbox_media_drive.gui.unmount", return_value="/dev/disk9s1") as unmount, \
                patch("xbox_media_drive.gui.discover", return_value=[SimpleNamespace(to_dict=lambda: dict(device, mounted=False))]), \
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
        self.window.set_target("/dev/disk9s1")
        with patch("xbox_media_drive.gui.is_mounted", return_value=True), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.No), \
                patch.object(self.window, "start") as start:
            self.window.inspect_volume()
        start.assert_not_called()

    def test_unmounted_targets_and_images_skip_the_prompt(self):
        self.window.set_target("/tmp/volume.img")
        with patch("xbox_media_drive.gui.is_mounted", return_value=False), \
                patch.object(QMessageBox, "question") as question, patch.object(self.window, "start") as start:
            self.window.inspect_volume()
        question.assert_not_called()
        self.assertEqual(start.call_args.args[0], "inspect")

    def test_disconnected_drive_error_refreshes_the_list_when_the_worker_finishes(self):
        self.window.set_target("/dev/disk9s1")
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
        self.window.set_target("/tmp/volume.img")
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

    def test_check_mode_uses_selected_whole_disk_and_gates_switching(self):
        self.add_device(mounted=False)
        for mode, title, action in (("game", "Game mode (Xbox signature)", "Switch to media mode…"),
                                    ("media", "Media / PC mode (standard signature)", "Switch to game mode…")):
            result = {"disk": "/dev/disk9", "mode": mode, "scheme": "gpt", "xbox_formatted": mode == "game"}
            with self.subTest(mode=mode), \
                    patch("xbox_media_drive.gui.helper_command", side_effect=lambda arguments, elevate: ["helper"] + arguments) as command, \
                    patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(
                        stdout=json.dumps(result), stderr="", returncode=0)):
                self.window.check_drive_mode()
                self.assertFalse(self.window.switch_mode_button.isEnabled())
                self.wait_for_idle()
            command.assert_called_once_with(["mode", "/dev/disk9"], elevate=True)
            self.assertEqual(self.window.disk_layout, result)
            self.assertIn(title, self.window.mode_status.text())
            self.assertIn("GPT", self.window.mode_status.text())
            self.assertIn("formatted by an Xbox", self.window.mode_status.text())
            self.assertTrue(self.window.switch_mode_button.isEnabled())
            self.assertEqual(self.window.switch_mode_button.text(), action)
        self.window.set_target("/dev/other")
        self.assertIsNone(self.window.disk_layout)
        self.assertFalse(self.window.switch_mode_button.isEnabled())

    def test_check_mode_resolves_target_instead_of_old_selection(self):
        self.add_device(mounted=False)
        self.window.set_target("/dev/sdb2")
        with patch("xbox_media_drive.gui.whole_disk", return_value="/dev/sdb") as resolve, \
                patch.object(self.window, "start") as start:
            self.window.check_drive_mode()
        resolve.assert_called_once_with("/dev/sdb2")
        start.assert_called_once_with("check-mode", ["mode", "/dev/sdb"])

    def test_unknown_or_blank_layout_never_enables_switching(self):
        self.window.action = "check-mode"
        for mode, scheme in ((None, "gpt"), ("media", "none")):
            self.window.completed({"disk": "/dev/disk9", "mode": mode, "scheme": scheme, "xbox_formatted": False})
            self.assertFalse(self.window.switch_mode_button.isEnabled())

    def test_switch_mode_arguments_warnings_busy_controls_and_completion(self):
        self.add_device(mounted=False)
        for previous, target in (("media", "game"), ("game", "media")):
            with tempfile.TemporaryDirectory() as folder, self.subTest(target=target):
                backup = os.path.join(folder, "sector.mbr")
                self.window.disk_layout = {"disk": "/dev/disk9", "mode": previous, "scheme": "gpt", "xbox_formatted": False}
                result = {"target": "/dev/disk9", "mode": target, "backup": backup}
                busy = []
                with patch("xbox_media_drive.gui.is_disk_mounted", return_value=False), \
                        patch("xbox_media_drive.gui.helper_command", side_effect=lambda arguments, elevate: ["helper"] + arguments), \
                        patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(
                            stdout=json.dumps(result), stderr="", returncode=0)) as helper, \
                        patch("xbox_media_drive.gui.discover", return_value=[]) as discover, \
                        patch.object(QFileDialog, "getSaveFileName", return_value=(backup, "")) as save, \
                        patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question, \
                        self.dialogs(lambda box: busy.append(self.window.spinner.isVisible())) as dialog:
                    self.window.switch_drive_mode()
                    self.assertIn("Switching /dev/disk9 to {} mode…".format(target), self.window.busy.text())
                    for button in (self.window.check_mode_button, self.window.switch_mode_button, self.window.restore_sector_button):
                        self.assertIn(button, self.window.controls)
                        self.assertFalse(button.isEnabled())
                    self.wait_for_idle()
                self.assertEqual(helper.call_args.args[0], ["helper", "mode", "/dev/disk9", "--set", target,
                                                           "--backup", backup, "--yes"])
                self.assertEqual(save.call_args.kwargs["options"], QFileDialog.DontConfirmOverwrite)
                warning = question.call_args.args[2]
                self.assertIn("not formatted by an Xbox", warning)
                self.assertIn("erases everything" if target == "game" else "Don't let the Xbox format it", warning)
                self.assertEqual(busy, [False])
                self.assertEqual(dialog[0][0], "Drive mode switched")
                self.assertIn(backup, dialog[0][1])
                self.assertIn("Unplug and reconnect", dialog[0][1])
                if __import__("sys").platform == "darwin":
                    self.assertIn("click Ignore", dialog[0][1])
                discover.assert_called_once()
                self.assertIsNone(self.window.disk_layout)
                self.assertFalse(self.window.switch_mode_button.isEnabled())

    def test_switch_mode_refuses_existing_backup(self):
        self.window.disk_layout = {"disk": "/dev/disk9", "mode": "media", "xbox_formatted": False}
        with tempfile.NamedTemporaryFile(suffix=".mbr") as backup, \
                patch("xbox_media_drive.gui.is_disk_mounted", return_value=False), \
                patch.object(QFileDialog, "getSaveFileName", return_value=(backup.name, "")), \
                patch.object(QMessageBox, "warning") as warning, \
                patch.object(QMessageBox, "question") as question, patch.object(self.window, "start") as start:
            self.window.switch_drive_mode()
        self.assertIn("never overwritten", warning.call_args.args[2])
        question.assert_not_called()
        start.assert_not_called()

    def test_switch_mode_unmounts_whole_disk_before_continuing(self):
        device = self.add_device(mounted=True)
        self.window.disk_layout = {"disk": "/dev/disk9", "mode": "media", "scheme": "gpt", "xbox_formatted": False}
        backup = "/tmp/new-sector.mbr"
        with patch("xbox_media_drive.gui.is_disk_mounted", side_effect=[True, False]), \
                patch("xbox_media_drive.gui.unmount", return_value="/dev/disk9") as unmount, \
                patch("xbox_media_drive.gui.discover", return_value=[SimpleNamespace(to_dict=lambda: dict(device, mounted=False))]), \
                patch("xbox_media_drive.gui.helper_command", return_value=["helper"]), \
                patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(
                    stdout='{"target":"/dev/disk9","mode":"game"}', stderr="", returncode=0)), \
                patch.object(QFileDialog, "getSaveFileName", return_value=(backup, "")), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as question, self.dialogs():
            self.window.switch_drive_mode()
            self.wait_for_idle()
        unmount.assert_called_once_with("/dev/disk9", whole=True)
        self.assertEqual([call.args[1] for call in question.call_args_list], ["Unmount disk", "Switch to game mode"])

    def test_restore_sector_passes_backup_to_helper_and_reports_reconnect(self):
        self.add_device(mounted=False)
        with patch("xbox_media_drive.gui.is_disk_mounted", return_value=False), \
                patch("xbox_media_drive.gui.helper_command", side_effect=lambda arguments, elevate: ["helper"] + arguments), \
                patch("xbox_media_drive.gui.subprocess.run", return_value=SimpleNamespace(
                    stdout='{"target":"/dev/disk9","mode":"media"}', stderr="", returncode=0)) as helper, \
                patch("xbox_media_drive.gui.discover", return_value=[]), \
                patch.object(QFileDialog, "getOpenFileName", return_value=("/tmp/sector.mbr", "")), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), self.dialogs() as dialog:
            self.window.restore_boot_sector()
            self.wait_for_idle()
        self.assertEqual(helper.call_args.args[0], ["helper", "mode", "/dev/disk9", "--restore", "/tmp/sector.mbr", "--yes"])
        self.assertEqual(dialog[0][0], "Boot sector restored")
        self.assertIn("Unplug and reconnect", dialog[0][1])
        self.assertIn("/tmp/sector.mbr", dialog[0][1])

    def test_header_shows_version(self):
        self.assertIn(__version__, self.window.header.text())

    def test_about_box_shows_version_license_and_source(self):
        box = self.window.about_box()
        self.addCleanup(box.deleteLater)
        self.assertIn(__version__, box.text())
        self.assertIn("GNU General Public License", box.text())
        self.assertIn(SOURCE_URL, box.text())
        self.assertEqual(len(box.buttons()), 1)
        self.assertEqual(box.standardButtons(), QMessageBox.Ok)
        self.assertNotIn("THIRD_PARTY_NOTICES", box.text())

    def test_packaged_about_box_names_bundled_components(self):
        with patch("xbox_media_drive.gui.sys.frozen", True, create=True):
            box = self.window.about_box()
        self.addCleanup(box.deleteLater)
        self.assertIn("NTFS-3G", box.text())
        self.assertIn("THIRD_PARTY_NOTICES.md", box.text())

    def test_about_action_opens_about_box(self):
        self.assertEqual(self.window.about_action.menuRole(), QAction.AboutRole)
        box = MagicMock()
        with patch.object(self.window, "about_box", return_value=box):
            self.window.about_action.trigger()
            self.window.about_button.click()
        self.assertEqual(box.exec.call_count, 2)
