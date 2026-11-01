"""KDE Plasma integration: an action in the Disks & Devices (device notifier) menu.

Plasma reads Solid actions from `solid/actions/*.desktop` under the user's data directory. This module writes and
removes such a file for the AppImage, which has no installer. It has no Qt imports and takes the environment as a
parameter, so it can be tested without touching the real home directory.
"""
import os
import shlex
import sys
import tempfile
from pathlib import Path

MARKER = "X-XboxMediaDrive-Managed=true"
ACTION_NAME = "xbox-media-drive-helper-appimage.desktop"
PREDICATE = ("[ IS_NOT OpticalDisc AND [ StorageVolume.ignored == false AND "
             "[ StorageVolume.usage == 'FileSystem' AND StorageVolume.fsType == 'ntfs' ] ] ]")
# Keep the predicate in step with packaging/linux/xbox-media-drive-helper-solid.desktop.
TEMPLATE = """[Desktop Entry]
Type=Service
X-KDE-Solid-Predicate={predicate}
Actions=open;
# Without this, Plasma mounts the volume before launching the action. The app needs it unmounted.
X-KDE-SkipMount=true
X-XboxMediaDrive-Managed=true

[Desktop Action open]
Exec={exec}
Icon={icon}
Name=Open in Xbox Media Drive Helper
"""


def data_home(environ=None):
    environ = os.environ if environ is None else environ
    configured = environ.get("XDG_DATA_HOME")
    if configured and os.path.isabs(configured):
        return Path(configured)
    home = environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".local/share"


def action_path(environ=None):
    return data_home(environ) / "solid/actions" / ACTION_NAME


def icon_path(environ=None):
    return data_home(environ) / "xbox-media-drive-helper/icon.svg"


def appimage_path(environ=None):
    """Return the running .AppImage file, or None outside an AppImage on Linux."""
    environ = os.environ if environ is None else environ
    if not sys.platform.startswith("linux") or not getattr(sys, "frozen", False):
        return None
    path = environ.get("APPIMAGE")
    if path and os.path.isabs(path) and os.path.isfile(path):
        return path
    return None


def _escape(value):
    """Escape a value for a KConfig .desktop file (backslash is its escape character)."""
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("The path contains a control character: {!r}".format(value))
    return value.replace("\\", "\\\\")


def exec_line(executable):
    # shlex.quote because Plasma runs Exec as a shell command line; "%" is doubled because it starts a macro.
    return _escape(shlex.quote(executable).replace("%", "%%") + ' "--device=%d"')


def action_text(executable, icon):
    return TEMPLATE.format(predicate=PREDICATE, exec=exec_line(executable),
                           icon=_escape(str(icon)))


def _write_atomically(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def install(executable, environ=None):
    """Write the icon and the Solid action for `executable`. Returns the action file's path."""
    icon = icon_path(environ)
    text = action_text(executable, icon)  # Validates before anything is written.
    _write_atomically(icon, Path(__file__).with_name("icon.svg").read_bytes())
    path = action_path(environ)
    _write_atomically(path, text.encode("utf-8"))
    return path


def installed(environ=None):
    text = _read(action_path(environ))
    return text is not None and MARKER in (line.strip() for line in text.splitlines())


def remove(environ=None):
    """Delete only the file this module wrote. Returns whether anything was removed."""
    if not installed(environ):
        return False
    action_path(environ).unlink()
    icon = icon_path(environ)
    try:
        icon.unlink()
    except FileNotFoundError:
        pass
    try:
        icon.parent.rmdir()
    except OSError:
        pass
    return True


def refresh(executable, environ=None):
    """Point an installed action at `executable` if it points elsewhere. Returns whether it was rewritten."""
    if not installed(environ):
        return False
    icon = icon_path(environ)
    if _read(action_path(environ)) == action_text(executable, icon) and icon.is_file():
        return False
    install(executable, environ)
    return True
