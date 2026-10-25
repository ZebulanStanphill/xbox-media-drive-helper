"""Qt desktop front end. Privileged work runs in a separate CLI process."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QThread, QTimer, QSignalBlocker, Signal
from PySide6.QtGui import QColor, QAction, QGuiApplication, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame,
                              QHBoxLayout, QLabel, QLineEdit, QListWidget,
                              QListWidgetItem, QMainWindow, QMessageBox,
                              QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                              QSizePolicy, QLayout, QProxyStyle, QStyle,
                              QTabBar, QTabWidget, QVBoxLayout, QWidget)

from . import DriveError, __version__
from .devices import discover, is_disk_mounted, is_mounted, mac_full_disk_access, unmount, whole_disk
from .fsck import CHECK_PROBLEMS_NOTE, find_ntfsck
from .operations import CLEAR_DIRTY_CONFIRMATION, DIRTY_WARNING

STYLE = """
QWidget { background: #101613; color: #e7eee8; font-size: 13px; }
QMainWindow { background: #101613; }
QLabel#muted { color: #a4b1a8; }
QLabel#section { font-size: 17px; font-weight: 650; }
QLabel#warning { color: #f3c969; font-weight: 600; }
QLabel#busy { color: #a6e7b8; font-weight: 600; }
QFrame#panel { border: 1px solid #2c3b31; border-radius: 12px; background: #151e18; }
QFrame#panel QLabel, QFrame#panel QWidget { background: transparent; }
QLabel#notice { color: #f3c969; }
QFrame#panel QFrame#divider { background: #2c3b31; }
QPushButton { background: #233027; border: 1px solid #3a4c40; border-radius: 7px; padding: 10px 15px; font-weight: 600; }
QPushButton:hover { background: #304336; }
QPushButton:disabled { color: #4f5c53; background: #141b16; border-color: #1f2922; }
QPushButton#primary { background: #98e5ad; border-color: #98e5ad; color: #102617; }
QPushButton#primary:hover { background: #b2f3c4; }
QPushButton#primary:disabled { background: #1e2f24; border-color: #1e2f24; color: #5b7462; }
QLineEdit, QComboBox, QListWidget, QPlainTextEdit { background: #0e1510; border: 1px solid #35473b; border-radius: 7px; padding: 9px; selection-background-color: #325b3e; }
QListWidget::item { padding: 12px 5px; border-bottom: 1px solid #223127; }
QListWidget::item:selected { background: #243d2c; border-radius: 5px; }
QLineEdit:disabled, QComboBox:disabled, QListWidget:disabled { color: #4f5c53; border-color: #1f2922; }
QListWidget::item:disabled { color: #4f5c53; }
QListWidget::item:selected:disabled { background: #18241c; }
QTabWidget::pane { border: 0; }
QTabBar::tab { padding: 11px 18px; color: #a4b1a8; border-bottom: 2px solid #2b3c30; }
QTabBar::tab:selected { color: #a6e7b8; border-bottom: 2px solid #98e5ad; }
QTabBar::tab:disabled { color: #4f5c53; border-bottom-color: #1f2922; }
QLabel:disabled, QLabel#muted:disabled { color: #4f5c53; }
QProgressBar { border: none; background: #23372a; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #98e5ad; border-radius: 3px; }
QMenuBar { background: #101613; color: #e7eee8; }
QMenuBar::item:selected { background: #233027; }
QMenu { background: #151e18; color: #e7eee8; border: 1px solid #2c3b31; }
QMenu::item:selected { background: #304336; }
QToolTip { background: #273d2e; color: white; border: 1px solid #55705d; }
"""


# As on macOS, scrolling over these controls scrolls the page instead of changing their value.
class ScrollSafeComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class ScrollSafeTabBar(QTabBar):
    def __init__(self):
        super().__init__()
        # Like the tab bar QTabWidget creates itself.
        self.setDrawBase(False)

    def wheelEvent(self, event):
        event.ignore()


class AppStyle(QProxyStyle):
    """Fusion, with the app icon on questions: macOS's question icon is a black glyph that vanishes on the dark theme."""

    def __init__(self):
        super().__init__("Fusion")

    def standardIcon(self, icon, option=None, widget=None):
        if icon == QStyle.SP_MessageBoxQuestion:
            question = app_icon()
            if not question.isNull():
                return question
        return super().standardIcon(icon, option, widget)


def configure(app):
    app.setStyle(AppStyle())
    app.setStyleSheet(STYLE)
    # Lets Wayland/X11 desktops associate the window with the .desktop launcher.
    app.setDesktopFileName("xbox-media-drive-helper")
    app.setApplicationName("Xbox Media Drive Helper")
    app.setApplicationVersion(__version__)


SOURCE_URL = "https://github.com/ZebulanStanphill/xbox-media-drive-helper"
LICENSE_URL = "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"
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

    def __init__(self, arguments=None, listing=False, unmount=None, whole=False):
        super().__init__()
        self.arguments = arguments
        self.listing = listing
        self.unmount = unmount
        self.whole = whole

    def run(self):
        try:
            if self.listing:
                self.done.emit({"devices": [device.to_dict() for device in discover()]})
            elif self.unmount:
                # Unmounting needs no privileges, so it never goes through the elevated helper.
                target = unmount(self.unmount, whole=True) if self.whole else unmount(self.unmount)
                self.done.emit({"unmounted": target})
            else:
                # NTFS-3G's security API requires root for images as well as disks.
                command = helper_command(self.arguments, elevate=True)
                process = subprocess.run(command, capture_output=True, text=True)
                self.done.emit(parse_helper_output(process.stdout, process.stderr, process.returncode))
        except Exception as exc:
            self.failed.emit(str(exc))


@lru_cache(maxsize=1)
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


class Spinner(QWidget):
    """A rotating arc shown while an operation runs. It keeps its space when hidden so the layout stays put."""

    def __init__(self, size=22):
        super().__init__()
        self.setFixedSize(size, size)
        policy = self.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.step)
        self.hide()

    def start(self):
        self.show()
        self.timer.start()

    def stop(self):
        self.timer.stop()
        self.hide()

    def step(self):
        self.angle = (self.angle + 15) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        pen = QPen(QColor("#29362d"), 3)
        painter.setPen(pen)
        painter.drawEllipse(rect)
        pen.setColor(QColor("#98e5ad"))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self.angle * 16, 100 * 16)


class MainWindow(QMainWindow):
    def __init__(self, auto_refresh=True):
        super().__init__()
        self.setWindowTitle("Xbox Media Drive Helper")
        self.setWindowIcon(app_icon())
        self.resize(1040, 940)
        self.setMinimumSize(900, 780)
        self.worker = None
        self.inspection = None
        self.target_path = ""
        self.image_path = ""
        self.disk_layout = None
        self.mode_backup = None
        self.filesystem_check = None
        self.action = None
        self.followups = []
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(14)
        header = QLabel(
            '<span style="font-size: 29px; font-weight: 700;">Xbox Media Drive Helper</span>'
            '&nbsp;<span style="font-size: 15px; color: #a4b1a8;">{}</span>'
            '&nbsp;&nbsp;&nbsp;'
            '<span style="font-size: 13px; color: #a4b1a8;">'
            'Prepare NTFS storage for Xbox apps, with a way back.</span>'.format(__version__)
        )
        header.setTextFormat(Qt.RichText)
        header.setWordWrap(False)
        self.header = header
        header_row = QHBoxLayout()
        header_row.addWidget(header)
        header_row.addStretch(1)
        self.about_button = QPushButton("About")
        self.about_button.setAccessibleName("About Xbox Media Drive Helper")
        self.about_button.clicked.connect(self.show_about)
        header_row.addWidget(self.about_button)
        layout.addLayout(header_row)
        # Qt moves this item into the macOS application menu.
        self.about_action = QAction("About Xbox Media Drive Helper", self)
        self.about_action.setMenuRole(QAction.AboutRole)
        self.about_action.triggered.connect(self.show_about)
        self.menuBar().addMenu("Help").addAction(self.about_action)

        intro = QFrame()
        intro.setObjectName("panel")
        intro_layout = QVBoxLayout(intro)
        intro_layout.setContentsMargins(17, 15, 17, 15)
        # Inside the intro panel rather than a panel of its own, so the drive list still fits at the minimum size.
        self.disk_access_notice = QWidget()
        notice_layout = QVBoxLayout(self.disk_access_notice)
        notice_layout.setContentsMargins(0, 0, 0, 0)
        notice_row = QHBoxLayout()
        notice_row.setSpacing(14)
        notice_row.addWidget(label(
            "macOS is blocking this app from reading drives. Turn on Xbox Media Drive Helper in Full Disk Access "
            "(add it with + if needed), then quit and reopen the app. Already on? Turn it off and on again: "
            "macOS treats each new build as a new app.", "notice"), 1)
        self.privacy_settings = QPushButton("Open Privacy Settings")
        self.privacy_settings.clicked.connect(self.open_privacy_settings)
        notice_row.addWidget(self.privacy_settings)
        notice_layout.addLayout(notice_row)
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        notice_layout.addWidget(divider)
        self.disk_access_notice.hide()
        intro_layout.addWidget(self.disk_access_notice)
        intro_layout.addWidget(label("Xbox media uses a normal NTFS volume. This tool adjusts app permissions and saves the "
                                     "originals. File contents and partitions stay intact; game-drive mode changes only the disk's two-byte signature.", "muted"))
        self.backup_warning = label("Back up the drive before using this app on it. Every change is recorded and reversible, "
                                    "but a recovery file is not a backup of your files, and a drive can fail or be unplugged "
                                    "at the wrong moment.", "warning")
        intro_layout.addWidget(self.backup_warning)
        layout.addWidget(intro)

        body = QHBoxLayout()
        body.setSpacing(24)
        left = QVBoxLayout()
        left.addWidget(label("1. Choose a volume", "section"))
        self.drives = QListWidget()
        self.drives.setAccessibleName("Volumes and images")
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
        right.addWidget(label("2. Prepare or restore", "section"))
        self.tabs = QTabWidget()
        self.tabs.setTabBar(ScrollSafeTabBar())
        prepare_tab = QWidget()
        prep = QVBoxLayout(prepare_tab)
        prep.setSizeConstraint(QLayout.SetMinimumSize)
        prep.setContentsMargins(0, 16, 0, 0)
        prep.setSpacing(12)
        access_row = QHBoxLayout()
        access_label = QLabel("App access")
        access_label.setWordWrap(False)
        access_row.addWidget(access_label)
        self.access = ScrollSafeComboBox()
        self.access.addItem("Read, write & delete  ·  Modify", "modify")
        self.access.addItem("Full control  ·  Compatibility option", "full")
        self.access.currentIndexChanged.connect(self.invalidate)
        access_row.addWidget(self.access, 1)
        prep.addLayout(access_row)
        prep.addWidget(label("Grants ALL APPLICATION PACKAGES access to existing files and folders, and Authenticated "
                             "Users the same access where the drive lacks it: Windows lets an app use only access granted "
                             "to both the app and its user account. This includes other packaged apps and users. Folders "
                             "pass it on to new content created in Windows.", "muted"))
        self.check = QPushButton("Inspect permissions")
        self.check.clicked.connect(self.inspect_volume)
        inspect_row = QHBoxLayout()
        inspect_row.addWidget(self.check, 1)
        # ntfsck is a Linux program; without it there is nothing to offer.
        self.check_filesystem_button = QPushButton("Check Filesystem…")
        self.check_filesystem_button.setVisible(bool(find_ntfsck()))
        self.check_filesystem_button.clicked.connect(self.check_filesystem_volume)
        inspect_row.addWidget(self.check_filesystem_button)
        prep.addLayout(inspect_row)
        self.status = label("Select a volume, then inspect its permissions.", "muted")
        self.status.setTextFormat(Qt.PlainText)
        prep.addWidget(self.status)
        self.dirty_warning = label(DIRTY_WARNING, "warning")
        self.dirty_warning.setTextFormat(Qt.PlainText)
        self.dirty_warning.hide()
        prep.addWidget(self.dirty_warning)
        self.clear_dirty_button = QPushButton("Clear Dirty Flag…")
        self.clear_dirty_button.hide()
        self.clear_dirty_button.clicked.connect(self.clear_dirty_volume)
        prep.addWidget(self.clear_dirty_button)
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
                             "are reported for review. Files not recorded in the journal keep their current permissions.", "muted"))
        self.restore_button = QPushButton("Choose journal & restore…")
        self.restore_button.clicked.connect(self.restore_volume)
        rest.addWidget(self.restore_button)
        rest.addStretch()
        restore_scroll = QScrollArea()
        restore_scroll.setFrameShape(QFrame.NoFrame)
        restore_scroll.setWidgetResizable(True)
        restore_scroll.setWidget(restore_tab)
        self.tabs.addTab(restore_scroll, "Restore permissions")

        mode_tab = QWidget()
        mode = QVBoxLayout(mode_tab)
        mode.setSizeConstraint(QLayout.SetMinimumSize)
        mode.setContentsMargins(0, 16, 0, 0)
        mode.setSpacing(12)
        mode.addWidget(label("An Xbox formats game drives as a normal GPT + NTFS disk but marks it with its own boot "
                             "signature (99 CC instead of 55 AA), so Macs and PCs treat it as unreadable. Switching "
                             "the signature is reversible and changes two bytes; files are untouched. Game packages "
                             "stay encrypted; media mode only lets you see and copy them.", "muted"))
        self.check_mode_button = QPushButton("Check drive mode")
        self.check_mode_button.clicked.connect(self.check_drive_mode)
        mode.addWidget(self.check_mode_button)
        self.mode_status = label("Select a drive, then check its mode.", "muted")
        self.mode_status.setTextFormat(Qt.PlainText)
        mode.addWidget(self.mode_status)
        self.switch_mode_button = QPushButton("Check drive mode first")
        self.switch_mode_button.setObjectName("primary")
        self.switch_mode_button.setEnabled(False)
        self.switch_mode_button.clicked.connect(self.switch_drive_mode)
        mode.addWidget(self.switch_mode_button)
        self.restore_sector_button = QPushButton("Restore boot sector from backup…")
        self.restore_sector_button.clicked.connect(self.restore_boot_sector)
        mode.addWidget(self.restore_sector_button)
        mode.addStretch()
        mode_scroll = QScrollArea()
        mode_scroll.setFrameShape(QFrame.NoFrame)
        mode_scroll.setWidgetResizable(True)
        mode_scroll.setWidget(mode_tab)
        self.tabs.addTab(mode_scroll, "Game drive mode")
        self.tabs.currentChanged.connect(self.update_unmount_button)
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

        busy_row = QHBoxLayout()
        busy_row.setSpacing(10)
        self.spinner = Spinner()
        busy_row.addWidget(self.spinner)
        self.busy = label("", "busy")
        self.busy.setTextFormat(Qt.PlainText)
        busy_row.addWidget(self.busy, 1)
        layout.addLayout(busy_row)
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
        layout.addWidget(label("For media apps on your Xbox, choose “Use for Media” when prompted.", "muted"))
        self.controls = [self.drives, self.refresh, self.unmount_button, self.image,
                         self.tabs, self.library, self.check_filesystem_button, self.clear_dirty_button,
                         self.check_mode_button, self.switch_mode_button, self.restore_sector_button]
        self.check_disk_access()
        QGuiApplication.instance().applicationStateChanged.connect(self.application_state_changed)
        if auto_refresh:
            self.refresh_devices()

    def check_disk_access(self):
        self.disk_access_notice.setVisible(mac_full_disk_access() is False)

    def application_state_changed(self, state):
        if state == Qt.ApplicationActive:
            self.check_disk_access()

    def open_privacy_settings(self):
        subprocess.run(["open", PRIVACY_SETTINGS])

    def log(self, text):
        self.activity.appendPlainText(datetime.now().strftime("%H:%M") + "  " + text)

    def set_target(self, path):
        if path != self.target_path:
            self.target_path = path
            self.filesystem_check = None
            self.invalidate()

    def invalidate(self, *_):
        self.inspection = None
        self.dirty_warning.hide()
        self.clear_dirty_button.hide()
        self.prepare_button.setEnabled(False)
        self.status.setText("Inspect this volume before preparing it." if self.target_path else
                            "Select a volume, then inspect its permissions.")
        self.disk_layout = None
        self.switch_mode_button.setEnabled(False)
        self.switch_mode_button.setText("Check drive mode first")
        self.mode_status.setText("Check this drive's mode before switching it.")

    def selected_device(self):
        item = self.drives.currentItem()
        return item.data(Qt.UserRole) if item else None

    def listed_devices(self):
        return [device for device in (self.drives.item(row).data(Qt.UserRole) for row in range(self.drives.count()))
                if device]

    def update_unmount_button(self):
        device = self.selected_device()
        mounted = bool(device and device["mounted"])
        if device and self.tabs.currentIndex() == 2:
            # Game-drive mode works on the whole disk: any mounted volume on it counts.
            mounted = any(other["mounted"] for other in self.listed_devices()
                          if other.get("disk") and other.get("disk") == device.get("disk"))
        self.unmount_button.setEnabled(self.worker is None and mounted)

    def select_drive(self, item, previous=None):
        if item and item.data(Qt.UserRole):
            self.set_target(item.data(Qt.UserRole)["path"])
        self.update_unmount_button()

    def select_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open raw NTFS volume image", "", "All files (*)")
        if path:
            with QSignalBlocker(self.drives):
                for row in reversed(range(self.drives.count())):
                    data = self.drives.item(row).data(Qt.UserRole)
                    if data and data.get("image"):
                        self.drives.takeItem(row)
                self.image_path = path
                item = self.add_image_item()
                if item:
                    self.drives.setCurrentItem(item)
            self.select_drive(self.drives.currentItem())

    def add_image_item(self):
        if not self.image_path:
            return None
        path = Path(self.image_path)
        if not path.is_file():
            self.image_path = ""
            return None
        size = path.stat().st_size
        item = QListWidgetItem("{}\nNTFS image  ·  {:.1f} GB\n{}".format(path.name, size / 10**9, path))
        item.setData(Qt.UserRole, {"path": str(path), "label": path.name, "size": size, "filesystem": "NTFS",
                                   "mounted": False, "external": False, "disk": "", "image": True})
        self.drives.insertItem(0, item)
        return item

    def refresh_devices(self):
        self.start("list", listing=True)

    def unmount_selected(self):
        device = self.selected_device()
        if device and self.tabs.currentIndex() == 2:
            try:
                self.start("unmount", unmount=self.selected_disk(), whole=True)
            except DriveError as exc:
                QMessageBox.warning(self, "Cannot select disk", str(exc))
        elif device and device["mounted"]:
            self.start("unmount", unmount=device["path"])

    def offer_unmount(self, resume, whole=False, path=None):
        """If the target is mounted, offer to unmount it and then call resume. Returns True when it took over."""
        path = path or self.target_path
        try:
            mounted = is_disk_mounted(path) if whole else is_mounted(path)
        except DriveError:
            return False
        if not mounted:
            return False
        name = next((device["label"] or path for device in self.listed_devices() if device["path"] == path), path)
        question = ("{} is mounted. Unmount it now? The drive stays connected. "
                    "Don't use Eject in Finder — that disconnects it.".format(name))
        if QMessageBox.question(self, "Unmount disk" if whole else "Unmount volume", question) == QMessageBox.Yes:
            self.followups = [resume]
            if whole:
                self.start("unmount", unmount=path, whole=True)
            else:
                self.start("unmount", unmount=path)
        else:
            self.log("{} is still mounted. Unmount it before continuing.".format(path))
        return True

    def arguments(self, command):
        args = []
        if self.library.text().strip():
            args.extend(["--library", self.library.text().strip()])
        args.extend([command, self.target_path])
        return args

    def inspect_volume(self):
        if self.target_path:
            if self.offer_unmount(self.inspect_volume):
                return
            self.start("inspect", self.arguments("inspect") + ["--access", self.access.currentData()])
        else:
            self.log("Choose a volume first.")

    def check_filesystem_volume(self):
        if not self.target_path:
            self.log("Choose a volume first.")
            return
        message = ("Check {} for problems with ntfsck?\n\nThe check is read-only: it never changes the drive. "
                   "It can take a while on a large drive.").format(self.target_path)
        if QMessageBox.question(self, "Check Filesystem", message) == QMessageBox.Yes:
            self.run_filesystem_check()

    def run_filesystem_check(self):
        if self.offer_unmount(self.run_filesystem_check):
            return
        self.start("check", self.arguments("check"))

    def remembered_check(self):
        """The latest ntfsck result for the selected drive, unless the inspected volume has since changed."""
        check = self.filesystem_check
        if not check or check["target"] != self.target_path:
            return None
        identity = (self.inspection or {}).get("identity")
        if check["identity"] and identity and check["identity"] != identity:
            return None
        return check

    def clear_dirty_volume(self):
        if not self.inspection or not self.inspection.get("dirty"):
            return
        if self.offer_unmount(self.clear_dirty_volume):
            return
        message = "Clear the NTFS dirty flag on {}?\n\n{}".format(self.target_path, CLEAR_DIRTY_CONFIRMATION)
        check = self.remembered_check()
        if check:
            message += "\n\n" + ("ntfsck found no problems on this drive at {}." if check["clean"] else
                                  "ntfsck found problems on this drive at {}; don't clear the flag, "
                                  "repair it in Windows instead.").format(check["time"])
        default = QMessageBox.No if check and not check["clean"] else QMessageBox.NoButton
        if QMessageBox.question(self, "Clear Dirty Flag", message, QMessageBox.Yes | QMessageBox.No, default) == QMessageBox.Yes:
            self.start("clear-dirty", self.arguments("clear-dirty") + ["--yes"])

    def prepare_volume(self):
        if not self.inspection or not self.inspection["ready"]:
            return
        if self.offer_unmount(self.prepare_volume):
            return
        append = False
        if self.inspection.get("previously_prepared"):
            box = QMessageBox(QMessageBox.Question, "Prepare again", "Choose a recovery journal", parent=self)
            box.setInformativeText("Adding to the journal from the earlier preparation keeps a single journal that restores everything.")
            existing = box.addButton("Add to Existing Journal…", QMessageBox.AcceptRole)
            new = box.addButton("New Journal…", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(existing)
            box.exec()
            if box.clickedButton() is existing:
                append = True
            elif box.clickedButton() is not new:
                return
        if append:
            backup, _ = QFileDialog.getOpenFileName(self, "Choose recovery journal from earlier preparation", str(Path.home()),
                                                   "SQLite journal (*.sqlite);;All files (*)")
        else:
            default = str(Path.home() / ("xbox-media-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite"))
            backup, _ = QFileDialog.getSaveFileName(self, "Save recovery journal on your computer", default,
                                                   "SQLite journal (*.sqlite)", options=QFileDialog.DontConfirmOverwrite)
            if backup and Path(backup).exists():
                QMessageBox.warning(self, "Journal already exists",
                                    "{} already exists. Existing journals are never overwritten.\n\n"
                                    "Choose a new file name, or use Add to Existing Journal to add this drive's new "
                                    "files to its earlier journal.".format(backup))
                return
        if not backup:
            return
        journal_message = ("Add {} objects to journal: {}" if append else "{} objects need changes.\nRecovery journal: {}").format(
            self.inspection["changes_needed"], backup)
        message = "Grant app access on {}?\n\nBack up the drive first if you haven't.\n\n{}\n\nKeep this journal: it is the only record used to restore original permissions automatically. If it is lost, the ALL APPLICATION PACKAGES and Authenticated Users entries added by this app must be removed manually in Windows.".format(
            self.target_path, journal_message)
        if self.inspection.get("dirty"):
            message += "\n\n" + DIRTY_WARNING
        if QMessageBox.question(self, "Prepare for Xbox", message) == QMessageBox.Yes:
            self.start("prepare", self.arguments("prepare") + ["--access", self.access.currentData(), "--backup", backup,
                       "--expect-volume", self.inspection["identity"]["boot_sha256"], "--yes"] + (["--append"] if append else []))

    def restore_volume(self):
        if not self.target_path:
            self.log("Choose the original NTFS volume first.")
            return
        if self.offer_unmount(self.restore_volume):
            return
        backup, _ = QFileDialog.getOpenFileName(self, "Choose recovery journal", str(Path.home()), "SQLite journal (*.sqlite);;All files (*)")
        if backup and QMessageBox.question(self, "Restore permissions", "Restore original permissions on {} using {}?".format(
                self.target_path, backup)) == QMessageBox.Yes:
            self.start("restore", self.arguments("restore") + ["--backup", backup, "--yes"])

    def selected_disk(self):
        """Use discovery's parent disk when it matches the selected target."""
        path = self.target_path
        device = self.selected_device()
        if device and device["path"] == path and device.get("disk"):
            return device["disk"]
        return whole_disk(path)

    def check_drive_mode(self):
        if not self.target_path:
            self.log("Choose a drive first.")
            return
        try:
            disk = self.selected_disk()
        except DriveError as exc:
            QMessageBox.warning(self, "Cannot select disk", str(exc))
            return
        self.disk_layout = None
        self.switch_mode_button.setEnabled(False)
        self.start("check-mode", ["mode", disk])

    def switch_drive_mode(self):
        if not self.disk_layout or self.disk_layout.get("mode") not in ("game", "media"):
            return
        disk = self.disk_layout["disk"]
        if self.offer_unmount(self.switch_drive_mode, whole=True, path=disk):
            return
        target = "media" if self.disk_layout["mode"] == "game" else "game"
        default = str(Path.home() / ("xbox-disk-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".mbr"))
        backup, _ = QFileDialog.getSaveFileName(self, "Save boot sector backup on your computer", default,
                                               "Boot sector backup (*.mbr)", options=QFileDialog.DontConfirmOverwrite)
        if backup and Path(backup).exists():
            QMessageBox.warning(self, "Backup already exists",
                                "{} already exists. Existing backups are never overwritten. Choose a new file name.".format(backup))
            return
        if not backup:
            return
        if target == "game":
            message = ("This Mac/PC will no longer be able to read {} until you switch it back. "
                       "If the drive was not formatted by an Xbox, the console may ask to format it, "
                       "which erases everything on it.").format(disk)
        else:
            message = ("The Xbox will not recognise {} until you switch it back. "
                       "Don't let the Xbox format it.").format(disk)
        hint = "formatted by an Xbox" if self.disk_layout["xbox_formatted"] else "not formatted by an Xbox"
        message += "\n\nBack up the drive first if you haven't.\n\nDetected layout: {}.\nBoot sector backup: {}".format(hint, backup)
        if QMessageBox.question(self, "Switch to {} mode".format(target), message) == QMessageBox.Yes:
            self.mode_backup = backup
            self.start("switch-mode", ["mode", disk, "--set", target, "--backup", backup, "--yes"])

    def restore_boot_sector(self):
        if not self.target_path:
            self.log("Choose the original whole disk first.")
            return
        try:
            disk = self.selected_disk()
        except DriveError as exc:
            QMessageBox.warning(self, "Cannot select disk", str(exc))
            return
        if self.offer_unmount(self.restore_boot_sector, whole=True, path=disk):
            return
        backup, _ = QFileDialog.getOpenFileName(self, "Choose boot sector backup", str(Path.home()),
                                               "Boot sector backup (*.mbr);;All files (*)")
        if backup and QMessageBox.question(self, "Restore boot sector",
                                          "Restore the boot sector on {} from {}? This restores the mode recorded "
                                          "in the backup. Unplug and reconnect the drive afterward.".format(disk, backup)) == QMessageBox.Yes:
            self.mode_backup = backup
            self.start("restore-sector", ["mode", disk, "--restore", backup, "--yes"])

    def start(self, action, arguments=None, listing=False, unmount=None, keep_inspection=False, whole=False):
        if self.worker is not None:
            return
        if action == "inspect":
            self.invalidate()
        if listing and not keep_inspection:
            self.invalidate()
        self.action = action
        self.worker = Worker(arguments, listing, unmount, whole)
        self.worker.done.connect(self.completed)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.finished)
        for control in self.controls:
            control.setEnabled(False)
        self.progress.setRange(0, 0)
        if listing:
            text = "Finding external volumes…"
        elif unmount:
            text = "Unmounting {}…".format(unmount)
        elif action == "switch-mode":
            text = "Switching {} to {} mode… Enter your administrator password if asked.".format(
                arguments[1], arguments[arguments.index("--set") + 1])
        elif action in ("check-mode", "restore-sector"):
            text = "{} {}… Enter your administrator password if asked.".format(
                "Checking drive mode on" if action == "check-mode" else "Restoring boot sector on", arguments[1])
        else:
            text = "{} {}… Enter your administrator password if asked.".format(
                {"inspect": "Inspecting permissions on", "prepare": "Preparing", "restore": "Restoring permissions on",
                 "clear-dirty": "Clearing the NTFS dirty flag on", "check": "Checking the filesystem on"}[action],
                self.target_path)
        self.log(text)
        self.busy.setText(text)
        self.spinner.start()
        self.worker.start()

    def completed(self, result):
        if self.action == "list":
            previous = self.selected_device()
            self.drives.clear()
            self.add_image_item()
            for device in result["devices"]:
                size = "{:.1f} GB".format(device["size"] / 10**9)
                item = QListWidgetItem("{}\n{}  ·  {}\n{}".format(
                    device["label"], device["filesystem"], size,
                    device["path"] + "  ·  Mounted — click Unmount" if device["mounted"] else device["path"]))
                item.setData(Qt.UserRole, device)
                self.drives.addItem(item)
            for row in range(self.drives.count()):
                item = self.drives.item(row)
                if previous and item.data(Qt.UserRole)["path"] == previous["path"]:
                    self.drives.setCurrentItem(item)
                    break
            if self.drives.currentItem() is None:
                self.set_target("")
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
            self.dirty_warning.setVisible(bool(result.get("dirty")))
            self.clear_dirty_button.setVisible(bool(result.get("dirty")))
            self.prepare_button.setEnabled(result["ready"] and result["changes_needed"] > 0)
            message = "{} objects inspected · {} need changes · {} reparse points skipped".format(
                result["objects"], result["changes_needed"], len(result["skipped_reparse_points"]))
            if result["blockers"]:
                message += "\nNeeds review: " + result["blockers"][0]["path"] + ": " + result["blockers"][0]["reason"]
            elif not result["changes_needed"]:
                message += "\nApp permissions are already present."
            if result.get("previously_prepared") and result["changes_needed"]:
                message += ("\nThis drive was prepared before, but {} objects don't have app access yet — usually files "
                            "copied from a Mac or Linux PC, which don't inherit it. Prepare again to add it.").format(result["changes_needed"])
            self.status.setText(message)
            self.log(message)
            QApplication.alert(self)
        elif self.action == "check":
            clean = result["verdict"] == "clean"
            self.filesystem_check = {"target": self.target_path, "identity": (self.inspection or {}).get("identity"),
                                     "clean": clean, "time": datetime.now().strftime("%H:%M")}
            message = ("ntfsck found no problems." if clean else
                       "ntfsck found problems. Repair the drive in Windows with chkdsk X: /f; don't just clear its dirty flag.")
            self.log(message)
            if not self.inspection:
                self.status.setText(message + " Inspect this volume before preparing it.")
            if clean:
                self.report(QMessageBox.Information, "No problems found", message)
            else:
                self.report(QMessageBox.Warning, "Problems found", CHECK_PROBLEMS_NOTE, details=result["output"])
        elif self.action == "clear-dirty":
            self.filesystem_check = None
            self.invalidate()
            self.log("NTFS dirty flag cleared." if result["cleared"] else "NTFS dirty flag was already clear.")
            if result["cleared"]:
                self.report(QMessageBox.Information, "Dirty flag cleared",
                            "The NTFS dirty flag on {} is cleared, and reading it back from the drive confirmed it. "
                            "Linux will no longer refuse to mount the drive because of this flag. Nothing else on the drive changed.\n\n"
                            "Click OK to inspect the drive again.".format(self.target_path))
            else:
                self.report(QMessageBox.Information, "Dirty flag already clear",
                            "The NTFS dirty flag on {} was already clear, so nothing changed.\n\n"
                            "Click OK to inspect the drive again.".format(self.target_path))
            self.followups.append(self.inspect_volume)
        elif self.action == "check-mode":
            self.disk_layout = result
            description = {"game": "Game mode (Xbox signature)", "media": "Media / PC mode (standard signature)"}.get(
                result["mode"], "Unknown boot signature")
            hint = "formatted by an Xbox" if result["xbox_formatted"] else "not formatted by an Xbox"
            message = "{} · {} · {}".format(description, result["scheme"].upper(), hint)
            self.mode_status.setText(message)
            self.log(message)
            self.update_mode_button()
        elif self.action in ("switch-mode", "restore-sector"):
            self.invalidate()
            restored = self.action == "restore-sector"
            title = "Boot sector restored" if restored else "Drive mode switched"
            message = ("Restored the boot sector on {} to {} mode." if restored else
                       "Changed the two boot signature bytes on {} to {} mode.").format(result["target"], result["mode"])
            backup = result.get("backup", self.mode_backup)
            if backup:
                message += "\n\nBoot sector backup: {}".format(backup)
            message += "\n\nUnplug and reconnect the drive so this computer re-reads it."
            if sys.platform == "darwin":
                message += "\nIf macOS says the disk is not readable, click Ignore."
            self.log(message)
            self.mode_status.setText(message)
            self.report(QMessageBox.Information, title, message)
            self.followups.append(self.refresh_devices)
        else:
            self.filesystem_check = None
            self.invalidate()
            if self.action == "prepare":
                appended = result.get("appended", False)
                headline = "Objects added to existing journal" if appended else "Preparation complete"
                summary = ("Added {} objects to the existing journal." if appended else "Prepared {} objects.").format(result["changed"])
                self.log(summary + " Keep journal: " + result["journal"])
                self.status.setText((summary if appended else "Permissions prepared.") + " Connect the drive to Xbox and choose “Use for Media”.")
                done = ("Added {1} objects on {0} to the existing journal. They now allow Xbox app access." if appended
                        else "Finished preparing {0}: {1} objects now allow Xbox app access.")
                self.report(QMessageBox.Information, headline,
                            done.format(self.target_path, result["changed"]) + "\n\n"
                            "Keep the recovery journal: {}\n\n"
                            "You can unplug the drive, connect it to your Xbox and choose “Use for Media”.\n\n"
                            "Files you copy onto the drive later from a Mac or Linux PC won't have app access until you "
                            "inspect and prepare it again (add them to this journal). Files copied in Windows inherit "
                            "access automatically.".format(result["journal"]))
            else:
                self.log("Restored {} objects; {} already original; {} conflicts; {} missing.".format(
                    result["restored"], result["already_original"], len(result["conflicts"]), len(result["missing"])))
                if result["state"] != "restored":
                    self.log(json.dumps({"conflicts": result["conflicts"], "missing": result["missing"]}, ensure_ascii=False))
                    self.report(QMessageBox.Warning, "Restoration needs review", "Some objects are missing or their permissions "
                                "changed. See the activity log. The journal is retained and restoration can be retried.")
                else:
                    self.status.setText("Original recorded permissions restored. Files not recorded in the journal retain their current permissions.")
                    self.report(QMessageBox.Information, "Restoration complete",
                                "Finished restoring {}: {} objects restored, {} were already original.\n\n"
                                "Files not recorded in the journal keep their current permissions.".format(
                                    self.target_path, result["restored"], result["already_original"]))

    def about_box(self):
        title = "About Xbox Media Drive Helper"
        # macOS hides dialog titles, so the bold headline is the app name.
        box = QMessageBox(QMessageBox.NoIcon, title, "", QMessageBox.Ok, self)
        box.setIconPixmap(app_icon().pixmap(64, 64))
        box.setTextFormat(Qt.RichText)
        link = '<a href="{0}"><span style="color: #98e5ad;">{1}</span></a>'
        # Only the packaged macOS app bundles these; source installs use the system's copies.
        bundled = ("<p>The app also bundles Python, Qt and PySide6 (LGPL) and NTFS-3G (GPL-2.0-or-later); "
                   "THIRD_PARTY_NOTICES.md on the disk image lists their licenses and sources.</p>"
                   if getattr(sys, "frozen", False) else "")
        box.setText(
            "<p style='font-size: 16px;'><b>Xbox Media Drive Helper</b></p>"
            "<p>Version {}</p>"
            "<p>Prepares NTFS drives for Xbox apps, with a way back.</p>"
            "<p>Licensed under the {}, version 2 or (at your option) any later version, with no warranty.</p>"
            "{}"
            "<p>Source code: {}</p>".format(
                __version__, link.format(LICENSE_URL, "GNU General Public License"), bundled,
                link.format(SOURCE_URL, SOURCE_URL)))
        text = box.findChild(QLabel, "qt_msgbox_label")
        if text is not None:
            text.setTextInteractionFlags(Qt.TextBrowserInteraction)
            text.setOpenExternalLinks(True)
        return box

    def show_about(self):
        self.about_box().exec()

    def report(self, icon, title, message, settings=False, details=None):
        """Show a result once the worker has finished, so the window is no longer busy behind the dialog."""
        QApplication.alert(self)

        def show():
            # macOS hides dialog titles, so the title is also the bold headline.
            box = QMessageBox(icon, title, title, QMessageBox.Close if settings else QMessageBox.Ok, self)
            box.setInformativeText(message)
            if details:
                box.setDetailedText(details)
            button = box.addButton("Open Privacy Settings", QMessageBox.AcceptRole) if settings else None
            box.exec()
            if button is not None and box.clickedButton() is button:
                self.open_privacy_settings()
        self.followups.append(show)

    def failed(self, message):
        self.invalidate()
        self.followups = []
        self.log("Error: " + message)
        disconnected = "is no longer connected" in message
        if self.action in ("prepare", "restore") and "No permissions changed" not in message:
            message += "\n\nIf preparation started, keep its recovery journal and use Restore permissions."
        if sys.platform == "darwin" and "Full Disk Access" in message:
            self.disk_access_notice.show()
            self.report(QMessageBox.Warning, "Disk access needed", message, settings=True)
        else:
            self.report(QMessageBox.Warning, "Operation could not complete", message)
        if disconnected:
            self.followups.append(self.refresh_devices)

    def finished(self):
        self.worker.deleteLater()
        self.worker = None
        for control in self.controls:
            control.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.spinner.stop()
        self.busy.setText("")
        self.update_unmount_button()
        self.update_mode_button()
        while self.followups and self.worker is None:
            self.followups.pop(0)()

    def update_mode_button(self):
        mode = self.disk_layout.get("mode") if self.disk_layout else None
        allowed = mode in ("game", "media") and self.disk_layout.get("scheme") in ("gpt", "mbr")
        self.switch_mode_button.setEnabled(self.worker is None and allowed)
        if mode in ("game", "media"):
            self.switch_mode_button.setText("Switch to {} mode…".format("media" if mode == "game" else "game"))

    def closeEvent(self, event):
        if self.worker is not None:
            self.log("Wait for the active operation to finish before closing.")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    configure(app)
    window = MainWindow()
    window.show()
    return app.exec()
