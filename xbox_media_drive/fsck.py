"""Optional read-only filesystem check with ntfsck from ntfsprogs-plus (Linux only)."""
import os
import stat
import subprocess
import sys

from . import DriveError
from .devices import validate_target

# Searched instead of PATH: the check runs as root, so a caller-controlled directory must never supply the program.
SYSTEM_DIRECTORIES = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
ENVIRONMENT = {"PATH": ":".join(SYSTEM_DIRECTORIES), "LC_ALL": "C"}
# Where the AppImage build puts ntfsck, relative to the PyInstaller bundle directory.
BUNDLED_NTFSCK = "ntfsprogs-plus/ntfsck"
EXECUTABLE = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

# Exit codes of ntfsck -n: 0 means no problems and 4 means problems it left uncorrected.
# Anything else (usage, operational or system errors, a signal) gave no verdict.
CLEAN, PROBLEMS = 0, 4

NTFSCK_MISSING = ("The filesystem check needs ntfsck from ntfsprogs-plus, which runs only on Linux, and none was found "
                  "in /usr/sbin, /usr/bin, /sbin or /bin (owned by root and not writable by others). "
                  "On Debian and Ubuntu, install it with sudo apt install ntfsprogs-plus; other distributions "
                  "package it as ntfsprogs-plus. Otherwise run a read-only chkdsk X: on Windows.")

CHECK_PROBLEMS_NOTE = ("ntfsck found problems. Don't clear the dirty flag. Copy off anything you need, then repair the "
                       "drive with chkdsk X: /f on Windows and inspect it again here, since a repair can move files "
                       "and reset their permissions. ntfsck is young and can report problems that aren't real, "
                       "and chkdsk is authoritative for drives used by Windows or an Xbox.")


def trusted(info, owner):
    return info.st_uid == owner and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def bundled_ntfsck(path=None, statvfs=os.statvfs):
    """Return the real path of the ntfsck inside a running AppImage, or None.

    The AppImage runtime mounts its squashfs read-only, so nothing can swap the file after mounting; the trust
    anchor is the .AppImage file itself, the same file pkexec runs as root. That is why the file is accepted
    without being root-owned: ownership inside a squashfs mount isn't reliable. It must still be a regular
    executable that others can't write, on a read-only filesystem. An extracted copy fails the last check,
    because it sits on a writable one.
    """
    if not (sys.platform.startswith("linux") and getattr(sys, "frozen", False) and os.environ.get("APPIMAGE")):
        return None
    try:
        candidate = path or os.path.join(sys._MEIPASS, BUNDLED_NTFSCK)
        real = os.path.realpath(candidate)
        info = os.stat(real)
        read_only = statvfs(real).f_flag & os.ST_RDONLY
    except (OSError, AttributeError):
        return None
    if stat.S_ISREG(info.st_mode) and info.st_mode & EXECUTABLE and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) \
            and read_only:
        return real
    return None


def is_bundled(checker, bundled=None, statvfs=os.statvfs):
    return bool(checker) and checker == bundled_ntfsck(bundled, statvfs)


def find_ntfsck(directories=SYSTEM_DIRECTORIES, owner=0, bundled=None, statvfs=os.statvfs):
    """Return the real path of a root-owned ntfsck in a root-owned system directory, else the AppImage's, or None.

    A system copy comes first because it gets distribution updates.
    """
    if not sys.platform.startswith("linux"):
        return None
    for directory in directories:
        try:
            # Resolve symlinks first, so the file and directory that get checked are the ones that get run.
            path = os.path.realpath(os.path.join(directory, "ntfsck"))
            info = os.stat(path)
            parent = os.stat(os.path.dirname(path))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mode & EXECUTABLE and trusted(info, owner) and trusted(parent, owner):
            return path
    return bundled_ntfsck(bundled, statvfs)


def tail(text, lines=200):
    return "\n".join(text.splitlines()[-lines:])[-20000:]


def check_filesystem(target, find=find_ntfsck, run=subprocess.run):
    target = validate_target(target)
    checker = find()
    if checker is None:
        raise DriveError(NTFSCK_MISSING)
    try:
        # -n opens the device read-only and answers "no" to every repair; -q silences progress.
        # Never add an option that repairs, and not -C either: despite its name it opens the device read-write.
        process = run([checker, "-n", "-q", target], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, errors="replace", env=ENVIRONMENT)
    except OSError as exc:
        raise DriveError("Could not run ntfsck: {}".format(exc)) from exc
    output = process.stdout or ""
    if process.returncode not in (CLEAN, PROBLEMS):
        # A short tail: the message is shown in a dialog.
        raise DriveError("ntfsck couldn't check the volume (exit {}).\n{}".format(
            process.returncode, tail(output, 20)).strip())
    return {"target": target, "checker": checker, "bundled": is_bundled(checker), "exit_code": process.returncode,
            "verdict": "clean" if process.returncode == CLEAN else "problems", "output": tail(output)}
