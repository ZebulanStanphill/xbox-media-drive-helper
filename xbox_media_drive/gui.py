"""Qt desktop front end. Privileged work runs in a separate CLI process."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame,
                              QHBoxLayout, QLabel, QLineEdit, QListWidget,
                              QListWidgetItem, QMainWindow, QMessageBox,
                              QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                              QSizePolicy, QLayout,
                              QTabWidget, QVBoxLayout, QWidget)

from . import DriveError
from .devices import discover, is_mounted, unmount

STYLE = """
QWidget { background: #101613; color: #e7eee8; font-size: 13px; }
QMainWindow { background: #101613; }
QLabel#title { font-size: 29px; font-weight: 700; }
QLabel#muted { color: #a4b1a8; }
QLabel#section { font-size: 17px; font-weight: 650; }
QFrame#panel { border: 1px solid #2c3b31; border-radius: 12px; background: #151e18; }
QFrame#panel QLabel, QFrame#panel QWidget { background: transparent; }
QPushButton { background: #233027; border: 1px solid #3a4c40; border-radius: 7px; padding: 10px 15px; font-weight: 600; }
QPushButton:hover { background: #304336; }
QPushButton:disabled { color: #76847a; background: #1c251f; border-color: #29362d; }
QPushButton#primary { background: #98e5ad; border-color: #98e5ad; color: #102617; }
QPushButton#primary:hover { background: #b2f3c4; }
QPushButton#primary:disabled { background: #304b39; border-color: #304b39; color: #8eab96; }
QLineEdit, QComboBox, QListWidget, QPlainTextEdit { background: #0e1510; border: 1px solid #35473b; border-radius: 7px; padding: 9px; selection-background-color: #325b3e; }
QListWidget::item { padding: 12px 5px; border-bottom: 1px solid #223127; }
QListWidget::item:selected { background: #243d2c; border-radius: 5px; }
QTabWidget::pane { border: 0; }
QTabBar::tab { padding: 11px 18px; color: #a4b1a8; border-bottom: 2px solid #2b3c30; }
QTabBar::tab:selected { color: #a6e7b8; border-bottom: 2px solid #98e5ad; }
QProgressBar { border: none; background: #23372a; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #98e5ad; border-radius: 3px; }
QToolTip { background: #273d2e; color: white; border: 1px solid #55705d; }
"""


PRIVACY_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def label(text, name=None):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    if name:
        widget.setObjectName(name)
    return widget


def parse_helper_output(stdout, stderr, code):
    result = None
    for line in (stdout + "\n" + stderr).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "progress" not in value:
            result = value
    if result and "error" in result:
        raise DriveError(result["error"])
    if code not in (0, 3) or result is None:
        raise DriveError((stderr or stdout or "The helper did not return a result.").strip()[-5000:])
    return result


def helper_command(arguments, elevate):
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--cli", "--json"] + arguments
    else:
        command = [sys.executable, "-m", "xbox_media_drive", "--json"] + arguments
    if not elevate or os.getuid() == 0:
        return command
    if sys.platform == "darwin":
        # Explicit shell quoting for every argument; no user text becomes shell code.
        shell = shlex.join(command) + " 2>&1; exit 0"
        literal = '"' + shell.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return ["osascript", "-e", "do shell script " + literal + " with administrator privileges"]
    if sys.platform.startswith("linux") and shutil.which("pkexec"):
        return ["pkexec"] + command
    raise DriveError("Install policykit's pkexec to authorize disk access, or use the CLI with sudo.")


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, arguments=None, listing=False, unmount=None):
        super().__init__()
        self.arguments = arguments
        self.listing = listing
        self.unmount = unmount

    def run(self):
        try:
            if self.listing:
                self.done.emit({"devices": [device.to_dict() for device in discover()]})
            elif self.unmount:
                # Unmounting needs no privileges, so it never goes through the elevated helper.
                self.done.emit({"unmounted": unmount(self.unmount)})
            else:
                # NTFS-3G's security API requires root for images as well as disks.
                command = helper_command(self.arguments, elevate=True)
                process = subprocess.run(command, capture_output=True, text=True)
                self.done.emit(parse_helper_output(process.stdout, process.stderr, process.returncode))
        except Exception as exc:
            self.failed.emit(str(exc))


def app_icon():
    """Render the bundled SVG at several sizes; fall back to no icon if QtSvg is unavailable."""
    icon = QIcon()
    try:
        from PySide6.QtSvg import QSvgRenderer
        svg = QSvgRenderer(str(Path(__file__).with_name("icon.svg")))
        if not svg.isValid():
            return icon
        for size in (16, 32, 48, 64, 128, 256, 512):
            image = QImage(size, size, QImage.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            svg.render(painter)
            painter.end()
            icon.addPixmap(QPixmap.fromImage(image))
    except ImportError:
        pass
    return icon


class MainWindow(QMainWindow):
    def __init__(self, auto_refresh=True):
        super().__init__()
        self.setWindowTitle("Xbox Media Drive Helper")
        self.setWindowIcon(app_icon())
        self.resize(1040, 940)
        self.setMinimumSize(900, 780)
        self.worker = None
        self.inspection = None
        self.action = None
        self.followups = []
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(14)
        header = QVBoxLayout()
        header.addWidget(label("Xbox Media Drive Helper", "title"))
        header.addWidget(label("Prepare NTFS storage for Xbox apps, with a way back.", "muted"))
        layout.addLayout(header)

        intro = QFrame()
        intro.setObjectName("panel")
        intro_layout = QVBoxLayout(intro)
        intro_layout.setContentsMargins(17, 15, 17, 15)
        intro_layout.addWidget(label("NTFS stays NTFS", "section"))
        intro_layout.addWidget(label("Xbox media uses a normal NTFS volume. This tool adjusts app permissions and saves the "
                                     "originals. File contents and the partition table stay intact.", "muted"))
        layout.addWidget(intro)

        body = QHBoxLayout()
        body.setSpacing(24)
        left = QVBoxLayout()
        left.addWidget(label("01  Choose a volume", "section"))
        self.drives = QListWidget()
        self.drives.setAccessibleName("External volumes")
        self.drives.setMinimumWidth(285)
        self.drives.currentItemChanged.connect(self.select_drive)
        left.addWidget(self.drives, 1)
        self.refresh = QPushButton("Refresh drives")
        self.refresh.clicked.connect(self.refresh_devices)
        self.image = QPushButton("Open NTFS image…")
        self.image.clicked.connect(self.select_image)
        self.unmount_button = QPushButton("Unmount")
        self.unmount_button.setEnabled(False)
        self.unmount_button.clicked.connect(self.unmount_selected)
        drive_buttons = QHBoxLayout()
        drive_buttons.addWidget(self.refresh, 1)
        drive_buttons.addWidget(self.unmount_button, 1)
        left.addLayout(drive_buttons)
        left.addWidget(self.image)
        left.addWidget(label("Click Unmount before inspecting a volume; do not use Eject, which disconnects the drive.", "muted"))
        body.addLayout(left, 2)

        right = QVBoxLayout()
        right.addWidget(label("02  Prepare or restore", "section"))
        self.target = QLineEdit()
        self.target.setPlaceholderText("/dev/disk4s1 or /dev/sdb1")
        self.target.setAccessibleName("NTFS volume path")
        self.target.textChanged.connect(self.invalidate)
        right.addWidget(self.target)
        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(290)
        prepare_tab = QWidget()
        prep = QVBoxLayout(prepare_tab)
        prep.setSizeConstraint(QLayout.SetMinimumSize)
        prep.setContentsMargins(0, 16, 0, 0)
        prep.setSpacing(12)
        prep.addWidget(label("App access"))
        self.access = QComboBox()
        self.access.addItem("Read, write & delete  ·  Modify", "modify")
        self.access.addItem("Full control  ·  Compatibility option", "full")
        self.access.currentIndexChanged.connect(self.invalidate)
        prep.addWidget(self.access)
        prep.addWidget(label("Grants ALL APPLICATION PACKAGES access to existing files and folders, "
                             "with inheritance for new content. This includes other packaged apps.", "muted"))
        self.check = QPushButton("Inspect permissions")
        self.check.clicked.connect(self.inspect_volume)
        prep.addWidget(self.check)
        self.status = label("Select a volume, then inspect its permissions.", "muted")
        self.status.setTextFormat(Qt.PlainText)
        prep.addWidget(self.status)
        self.prepare_button = QPushButton("Prepare for Xbox…")
        self.prepare_button.setObjectName("primary")
        self.prepare_button.setEnabled(False)
        self.prepare_button.clicked.connect(self.prepare_volume)
        prep.addWidget(self.prepare_button)
        prep.addStretch()
        prepare_scroll = QScrollArea()
        prepare_scroll.setFrameShape(QFrame.NoFrame)
        prepare_scroll.setWidgetResizable(True)
        prepare_scroll.setWidget(prepare_tab)
        self.tabs.addTab(prepare_scroll, "Prepare for Xbox")

        restore_tab = QWidget()
        rest = QVBoxLayout(restore_tab)
        rest.setSizeConstraint(QLayout.SetMinimumSize)
        rest.setContentsMargins(0, 16, 0, 0)
        rest.setSpacing(12)
        rest.addWidget(label("Return to original permissions", "section"))
        rest.addWidget(label("Choose the recovery journal saved when you prepared this volume. "
                             "It also works after an interrupted preparation.", "muted"))
        rest.addWidget(label("Files are matched by NTFS record ID, even after a rename. Changed permissions "
                             "are reported for review. New files keep their current permissions.", "muted"))
        self.restore_button = QPushButton("Choose journal & restore…")
        self.restore_button.clicked.connect(self.restore_volume)
        rest.addWidget(self.restore_button)
        rest.addStretch()
        restore_scroll = QScrollArea()
        restore_scroll.setFrameShape(QFrame.NoFrame)
        restore_scroll.setWidgetResizable(True)
        restore_scroll.setWidget(restore_tab)
        self.tabs.addTab(restore_scroll, "Restore permissions")
        right.addWidget(self.tabs, 1)
        library_label = label("Optional: trusted NTFS-3G library path", "muted")
        right.addWidget(library_label)
        self.library = QLineEdit()
        self.library.setPlaceholderText("Auto-detect installed libntfs-3g")
        self.library.setAccessibleName("NTFS-3G library path")
        self.library.textChanged.connect(self.invalidate)
        right.addWidget(self.library)
        if getattr(sys, "frozen", False):
            library_label.hide()
            self.library.hide()
        body.addLayout(right, 3)
        layout.addLayout(body, 1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setFixedHeight(90)
        self.activity.setAccessibleName("Activity log")
        self.activity.setPlaceholderText("Operation results will appear here.")
        layout.addWidget(self.activity)
        layout.addWidget(label("On your Xbox, choose “Use for Media” when prompted.", "muted"))
        self.controls = [self.drives, self.refresh, self.image, self.target, self.tabs, self.library]
        if auto_refresh:
            self.refresh_devices()

    def log(self, text):
        self.activity.appendPlainText(datetime.now().strftime("%H:%M") + "  " + text)

    def invalidate(self, *_):
        self.inspection = None
        self.prepare_button.setEnabled(False)
        self.status.setText("Inspect this volume before preparing it.")

    def selected_device(self):
        item = self.drives.currentItem()
        return item.data(Qt.UserRole) if item else None

    def update_unmount_button(self):
        device = self.selected_device()
        self.unmount_button.setEnabled(self.worker is None and bool(device and device["mounted"]))

    def select_drive(self, item, previous=None):
        if item:
            self.target.setText(item.data(Qt.UserRole)["path"])
        self.update_unmount_button()

    def select_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open raw NTFS volume image", "", "All files (*)")
        if path:
            self.target.setText(path)

    def refresh_devices(self):
        self.start("list", listing=True)

    def unmount_selected(self):
        device = self.selected_device()
        if device and device["mounted"]:
            self.start("unmount", unmount=device["path"])

    def offer_unmount(self, resume):
        """If the target is mounted, offer to unmount it and then call resume. Returns True when it took over."""
        path = self.target.text().strip()
        try:
            mounted = is_mounted(path)
        except DriveError:
            return False
        if not mounted:
            return False
        devices = [self.drives.item(row).data(Qt.UserRole) for row in range(self.drives.count())]
        name = next((device["label"] for device in devices if device and device["path"] == path), path)
        question = ("{} is mounted. Unmount it now? The drive stays connected. "
                    "Don't use Eject in Finder — that disconnects it.".format(name))
        if QMessageBox.question(self, "Unmount volume", question) == QMessageBox.Yes:
            self.followups = [resume]
            self.start("unmount", unmount=path)
        else:
            self.log("{} is still mounted. Unmount it before continuing.".format(path))
        return True

    def arguments(self, command):
        args = []
        if self.library.text().strip():
            args.extend(["--library", self.library.text().strip()])
        args.extend([command, self.target.text().strip()])
        return args

    def inspect_volume(self):
        if self.target.text().strip():
            if self.offer_unmount(self.inspect_volume):
                return
            self.start("inspect", self.arguments("inspect") + ["--access", self.access.currentData()])
        else:
            self.log("Choose a volume or enter its device path first.")

    def prepare_volume(self):
        if not self.inspection or not self.inspection["ready"]:
            return
        if self.offer_unmount(self.prepare_volume):
            return
        default = str(Path.home() / ("xbox-media-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite"))
        backup, _ = QFileDialog.getSaveFileName(self, "Save recovery journal on your computer", default, "SQLite journal (*.sqlite)")
        if not backup:
            return
        message = "Grant app access on {}?\n\n{} objects need changes.\nRecovery journal: {}\n\nKeep this journal: it is the only record used to restore original permissions automatically. If it is lost, the app grant ALL APPLICATION PACKAGES must be removed manually in Windows.".format(
            self.target.text(), self.inspection["changes_needed"], backup)
        if QMessageBox.question(self, "Prepare for Xbox", message) == QMessageBox.Yes:
            self.start("prepare", self.arguments("prepare") + ["--access", self.access.currentData(), "--backup", backup,
                       "--expect-volume", self.inspection["identity"]["boot_sha256"], "--yes"])

    def restore_volume(self):
        if not self.target.text().strip():
            self.log("Choose the original NTFS volume first.")
            return
        if self.offer_unmount(self.restore_volume):
            return
        backup, _ = QFileDialog.getOpenFileName(self, "Choose recovery journal", str(Path.home()), "SQLite journal (*.sqlite);;All files (*)")
        if backup and QMessageBox.question(self, "Restore permissions", "Restore original permissions on {} using {}?".format(
                self.target.text(), backup)) == QMessageBox.Yes:
            self.start("restore", self.arguments("restore") + ["--backup", backup, "--yes"])

    def start(self, action, arguments=None, listing=False, unmount=None, keep_inspection=False):
        if self.worker is not None:
            return
        if listing and not keep_inspection:
            self.invalidate()
        self.action = action
        self.worker = Worker(arguments, listing, unmount)
        self.worker.done.connect(self.completed)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.finished)
        for control in self.controls:
            control.setEnabled(False)
        self.unmount_button.setEnabled(False)
        self.progress.setRange(0, 0)
        if listing:
            self.log("Finding external volumes…")
        elif unmount:
            self.log("Unmounting {}…".format(unmount))
        else:
            self.log("{}: waiting for authorization or processing the volume…".format(action.capitalize()))
        self.worker.start()

    def completed(self, result):
        if self.action == "list":
            previous = self.selected_device()
            self.drives.clear()
            for device in result["devices"]:
                size = "{:.1f} GB".format(device["size"] / 10**9)
                item = QListWidgetItem("{}\n{}  ·  {}\n{}".format(
                    device["label"], device["filesystem"], size,
                    device["path"] + "  ·  Mounted — click Unmount" if device["mounted"] else device["path"]))
                item.setData(Qt.UserRole, device)
                self.drives.addItem(item)
                if previous and device["path"] == previous["path"]:
                    self.drives.setCurrentItem(item)
            self.log("Found {} external volumes.".format(len(result["devices"])))
            if not result["devices"]:
                item = QListWidgetItem("No external volumes\nConnect a drive, then refresh.")
                item.setFlags(Qt.NoItemFlags)
                self.drives.addItem(item)
                self.status.setText("No external volumes found. Connect a drive and refresh, or open an NTFS image.")
        elif self.action == "unmount":
            self.log("Unmounted {}. The drive stays connected.".format(result["unmounted"]))
            # Refresh the list without discarding an inspection, then resume any action that asked for the unmount.
            self.followups.insert(0, lambda: self.start("list", listing=True, keep_inspection=True))
        elif self.action == "inspect":
            self.inspection = result
            self.prepare_button.setEnabled(result["ready"] and result["changes_needed"] > 0)
            message = "{} objects inspected · {} need changes · {} reparse points skipped".format(
                result["objects"], result["changes_needed"], len(result["skipped_reparse_points"]))
            if result["blockers"]:
                message += "\nNeeds review: " + result["blockers"][0]["path"] + ": " + result["blockers"][0]["reason"]
            elif not result["changes_needed"]:
                message += "\nApp permissions are already present."
            self.status.setText(message)
            self.log(message)
        else:
            self.invalidate()
            if self.action == "prepare":
                self.log("Prepared {} objects. Keep journal: {}".format(result["changed"], result["journal"]))
                self.status.setText("Permissions prepared. Connect the drive to Xbox and choose “Use for Media”.")
            else:
                self.log("Restored {} objects; {} already original; {} conflicts; {} missing.".format(
                    result["restored"], result["already_original"], len(result["conflicts"]), len(result["missing"])))
                if result["state"] != "restored":
                    self.log(json.dumps({"conflicts": result["conflicts"], "missing": result["missing"]}, ensure_ascii=False))
                    QMessageBox.warning(self, "Restoration needs review", "Some objects are missing or their permissions changed. "
                                        "See the activity log. The journal is retained and restoration can be retried.")
                else:
                    self.status.setText("Original recorded permissions restored. New files retain their current permissions.")

    def failed(self, message):
        self.invalidate()
        self.followups = []
        if "is no longer connected" in message:
            self.followups.append(self.refresh_devices)
        self.log("Error: " + message)
        if self.action in ("prepare", "restore"):
            message += "\n\nIf preparation started, keep its recovery journal and use Restore permissions."
        if sys.platform == "darwin" and "Full Disk Access" in message:
            box = QMessageBox(QMessageBox.Warning, "Disk access needed", message, QMessageBox.Close, self)
            settings = box.addButton("Open Privacy Settings", QMessageBox.AcceptRole)
            box.exec()
            if box.clickedButton() is settings:
                subprocess.run(["open", PRIVACY_SETTINGS])
            return
        QMessageBox.warning(self, "Operation could not complete", message)

    def finished(self):
        self.worker.deleteLater()
        self.worker = None
        for control in self.controls:
            control.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.update_unmount_button()
        while self.followups and self.worker is None:
            self.followups.pop(0)()

    def closeEvent(self, event):
        if self.worker is not None:
            self.log("Wait for the active operation to finish before closing.")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    # Lets Wayland/X11 desktops associate the window with the .desktop launcher.
    app.setDesktopFileName("xbox-media-drive-helper")
    window = MainWindow()
    window.show()
    return app.exec()
