"""ctypes bindings to NTFS-3G's published security API. No FUSE mount needed."""
import ctypes as C
import ctypes.util
import errno
import glob
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import DriveError
from .security import Descriptor, ntfs3g_dacl_write

CALLBACK = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_int, C.c_int,
                      C.c_int64, C.c_uint64, C.c_uint)
MFT_MASK = (1 << 48) - 1
MAX_DESCRIPTOR = 262144
NTFS_MNT_EXCLUSIVE = 0x08000000
NTFS_MNT_RDONLY = 0x00000001
NTFS_MNT_FORENSIC = 0x04000000
READ_ONLY_FLAGS = NTFS_MNT_EXCLUSIVE | NTFS_MNT_RDONLY | NTFS_MNT_FORENSIC


WINDOWS_REPAIR_WARNING = ("If Windows says there's a problem with the drive, copy off anything you need before letting "
                          "it repair the drive, then inspect permissions again: a repair can move files and reset their "
                          "permissions.")
WINDOWS_CACHED_REMEDY = ("Connect the drive to the Windows PC it was last used on, then shut Windows down fully (hold Shift "
                         "while choosing Shut down) or eject the drive with Safely Remove Hardware. "
                         + WINDOWS_REPAIR_WARNING)
WINDOWS_CACHED = ("Windows kept this drive's metadata cached (hibernation or Fast Startup), or lost the drive before "
                  "closing it, so NTFS-3G won't change it. No permissions changed.\n\n" + WINDOWS_CACHED_REMEDY)
JOURNAL_CONSEQUENCES = ("Until it is closed, Linux refuses to mount the drive read-write, and trying (for example Mount "
                        "and Open in Dolphin or KDE's Disks & Devices) marks the drive dirty; and this app can't "
                        "prepare it, restore its permissions or clear its dirty flag.")


def close_journal_steps(target):
    if sys.platform.startswith("linux"):
        ntfsfix = "Run sudo ntfsfix -d {} (ntfsfix is in the ntfs-3g package).".format(target)
    else:
        ntfsfix = ("On a Linux computer, run sudo ntfsfix -d on the drive's NTFS partition, for example "
                   "sudo ntfsfix -d /dev/sdb1 (ntfsfix is in the ntfs-3g package).")
    return ("To close the journal, do one of these:\n"
            "- Connect the drive to a Windows PC, wait for it to appear, then eject it with Safely Remove Hardware. "
            + WINDOWS_REPAIR_WARNING + "\n"
            "- " + ntfsfix + "\n"
            "  This empties the journal, discarding any changes Windows hadn't finished writing. Keep the -d: "
            "without it, ntfsfix marks the drive for a Windows disk check, and Linux then refuses to mount it.")


def open_journal_message(target):
    return ("The drive's NTFS journal is still open: an Xbox or Windows PC was using the drive and didn't close it, "
            "so NTFS-3G won't change it. No permissions changed.\n\n" + close_journal_steps(target))


def journal_warning(state, target):
    """Explain a drive whose journal isn't clean, found by inspection rather than by a refused write."""
    if state == "open":
        return ("This drive wasn't closed properly: its NTFS journal is still open, which happens when an Xbox or "
                "Windows PC was using it and it was removed without being closed. " + JOURNAL_CONSEQUENCES + "\n\n"
                + close_journal_steps(target))
    if state == "windows-cached":
        return ("Windows kept this drive's metadata cached (hibernation or Fast Startup), or lost the drive before "
                "closing it. " + JOURNAL_CONSEQUENCES + "\n\n" + WINDOWS_CACHED_REMEDY)
    raise ValueError("Unknown journal state {!r}".format(state))


class SecurityAPIPrefix(C.Structure):
    # Published SECURITY_API begins with u32 magic, then SECURITY_CONTEXT,
    # whose first member is ntfs_volume*. ctypes applies native C alignment.
    _fields_ = [("magic", C.c_uint32), ("volume", C.c_void_p)]


def load_library(explicit=None):
    frozen = getattr(sys, "frozen", False)
    if explicit:
        candidates = [explicit]
    elif frozen:
        # Bundle dependencies take precedence and never fall back to a host
        # Homebrew installation or a development checkout.
        directory = Path(sys._MEIPASS)
        pattern = "libntfs-3g*.dylib" if sys.platform == "darwin" else "libntfs-3g.so*"
        candidates = sorted(glob.glob(str(directory / pattern)))
    else:
        candidates = [ctypes.util.find_library("ntfs-3g")]
    if not explicit and not frozen:
        for pattern in ("/opt/homebrew/lib/libntfs-3g*.dylib", "/usr/local/lib/libntfs-3g*.dylib",
                        "/usr/lib/*/libntfs-3g.so*", "/usr/lib64/libntfs-3g.so*",
                        "/lib/*/libntfs-3g.so*"):
            candidates.extend(sorted(glob.glob(pattern)))
        candidates.extend(sorted(glob.glob(str(Path(__file__).resolve().parent.parent / ".native/lib/libntfs-3g.*"))))
    errors = []
    for name in dict.fromkeys(filter(None, candidates)):
        try:
            lib = C.CDLL(name, use_errno=True)
            lib.ntfs_log_set_handler.argtypes = [C.c_void_p]
            lib.ntfs_log_set_handler.restype = None
            lib.ntfs_log_set_handler(C.cast(lib.ntfs_log_handler_stderr, C.c_void_p))
            lib.ntfs_initialize_file_security.argtypes = [C.c_char_p, C.c_ulong]
            lib.ntfs_initialize_file_security.restype = C.c_void_p
            lib.ntfs_leave_file_security.argtypes = [C.c_void_p]
            lib.ntfs_leave_file_security.restype = C.c_int
            lib.ntfs_get_file_security.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32,
                                                 C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32)]
            lib.ntfs_get_file_security.restype = C.c_int
            lib.ntfs_set_file_security.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32, C.c_void_p]
            lib.ntfs_set_file_security.restype = C.c_int
            lib.ntfs_get_file_attributes.argtypes = [C.c_void_p, C.c_char_p]
            lib.ntfs_get_file_attributes.restype = C.c_int
            lib.ntfs_pathname_to_inode.argtypes = [C.c_void_p, C.c_void_p, C.c_char_p]
            lib.ntfs_pathname_to_inode.restype = C.c_void_p
            lib.ntfs_readdir.argtypes = [C.c_void_p, C.POINTER(C.c_int64), C.c_void_p, CALLBACK]
            lib.ntfs_readdir.restype = C.c_int
            lib.ntfs_inode_close.argtypes = [C.c_void_p]
            lib.ntfs_inode_close.restype = C.c_int
            lib.ntfs_inode_open.argtypes = [C.c_void_p, C.c_uint64]
            lib.ntfs_inode_open.restype = C.c_void_p
            lib.ntfs_attr_open.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32]
            lib.ntfs_attr_open.restype = C.c_void_p
            lib.ntfs_attr_pread.argtypes = [C.c_void_p, C.c_int64, C.c_int64, C.c_void_p]
            lib.ntfs_attr_pread.restype = C.c_int64
            lib.ntfs_attr_pwrite.argtypes = [C.c_void_p, C.c_int64, C.c_int64, C.c_void_p]
            lib.ntfs_attr_pwrite.restype = C.c_int64
            lib.ntfs_attr_close.argtypes = [C.c_void_p]
            lib.ntfs_attr_close.restype = None
            lib.ntfs_check_logfile.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
            lib.ntfs_check_logfile.restype = C.c_int
            lib.ntfs_is_logfile_clean.argtypes = [C.c_void_p, C.c_void_p]
            lib.ntfs_is_logfile_clean.restype = C.c_int
            return lib
        except (OSError, AttributeError) as exc:
            errors.append(str(exc))
    raise DriveError("NTFS-3G security library not found. See README setup or pass --library /absolute/path. "
                     + "; ".join(errors))


@dataclass(frozen=True)
class Entry:
    path: str
    reference: int
    directory: bool


class Volume:
    def __init__(self, target, writable=False, library=None):
        self.lib = load_library(library)
        self.writable = writable
        # EXCLUSIVE, with neither RECOVER nor IGNORE_HIBERFILE nor MAY_RDONLY.
        flags = NTFS_MNT_EXCLUSIVE if writable else READ_ONLY_FLAGS
        self.handle = self._initialize(target, flags)
        if not self.handle:
            error = C.get_errno()
            # NTFS-3G checks the journal and hibernation only for writable sessions; it reports an open journal
            # as EOPNOTSUPP and Windows-cached metadata as EPERM, which otherwise means "not root".
            root = os.getuid() == 0
            if writable and (error == errno.EOPNOTSUPP or (error == errno.EPERM and root)):
                probe = self._initialize(target, READ_ONLY_FLAGS)
                if probe:
                    self.lib.ntfs_leave_file_security(probe)
                    raise DriveError(open_journal_message(target) if error == errno.EOPNOTSUPP else WINDOWS_CACHED)
            explanation = {errno.EBUSY: "The volume is mounted or in use. Unmount it first.",
                           errno.EACCES: "The device is not accessible. Run with sudo; check macOS disk-access permissions."}
            if not root:
                explanation[errno.EPERM] = "NTFS-3G requires root, including for image files. Run this command with sudo."
            raise DriveError(explanation.get(error, "NTFS-3G refused the volume ({}). It may be dirty, hibernated, "
                                             "damaged, or unsupported. Shut down Windows fully and check the filesystem there."
                                             .format(os.strerror(error))))

    def _initialize(self, target, flags):
        return self.lib.ntfs_initialize_file_security(os.fsencode(target), flags)

    def __enter__(self):
        return self

    def __exit__(self, typ, value, traceback):
        handle, self.handle = self.handle, None
        if handle and not self.lib.ntfs_leave_file_security(handle):
            if typ is None:
                raise DriveError("NTFS-3G could not close/flush the volume. Keep the recovery journal and check the drive.")

    def _volume_pointer(self):
        prefix = C.cast(self.handle, C.POINTER(SecurityAPIPrefix)).contents
        if prefix.magic != 0x09042009 or not prefix.volume:
            raise DriveError("Unsupported NTFS-3G security API layout.")
        return prefix.volume

    def dirty(self):
        """Read VOLUME_IS_DIRTY from $Volume's $VOLUME_INFORMATION."""
        return bool(self._volume_flags() & 1)

    def clear_dirty(self):
        """Clear only VOLUME_IS_DIRTY, preserving all other on-disk flags."""
        if not self.writable:
            raise DriveError("Refusing to write through a read-only session.")
        # One direct write of the flags field: NTFS-3G's ntfs_volume_write_flags() masks
        # them with 0xc03f, dropping bits Windows sets. Unmount does not rewrite them.
        self._volume_flags(write=self._volume_flags() & ~1)

    def journal_state(self):
        """Report the $LogFile state as NTFS-3G's ntfs_volume_check_logfile() sees it, but read-only.

        "clean", "open" (restart pages unclean or invalid; a writable session fails with EOPNOTSUPP) or
        "windows-cached" (restart pages version 2.0, Windows-cached metadata; a writable session fails with EPERM).
        """
        inode = self.lib.ntfs_inode_open(self._volume_pointer(), 2)
        if not inode:
            raise DriveError("Cannot read the NTFS journal state: cannot open $LogFile.")
        restart = C.c_void_p()
        try:
            attr = self.lib.ntfs_attr_open(inode, 0x80, None, 0)
            if not attr:
                raise DriveError("Cannot read the NTFS journal state: missing journal data.")
            try:
                clean = bool(self.lib.ntfs_check_logfile(attr, C.byref(restart))
                             and self.lib.ntfs_is_logfile_clean(attr, restart))
                state = "clean" if clean else "open"
                if restart:
                    # RESTART_PAGE_HEADER: minor_ver (s16) at 0x1a, major_ver (s16) at 0x1c.
                    header = C.string_at(restart, 0x1e)
                    if (int.from_bytes(header[0x1c:0x1e], "little", signed=True) == 2
                            and int.from_bytes(header[0x1a:0x1c], "little", signed=True) == 0):
                        state = "windows-cached"
            finally:
                self.lib.ntfs_attr_close(attr)
                if restart:
                    C.CDLL(None).free(restart)
        finally:
            closed = self.lib.ntfs_inode_close(inode)
        if closed:
            raise DriveError("Cannot close $LogFile after reading the NTFS journal state.")
        return state

    def _volume_flags(self, write=None):
        """Read (or preserve) the on-disk u16 flags without volume struct offsets."""
        if write is not None and not self.writable:
            raise DriveError("Refusing to write through a read-only session.")
        inode = self.lib.ntfs_inode_open(self._volume_pointer(), 3)
        if not inode:
            raise DriveError("Cannot read the NTFS dirty flag: cannot open $Volume.")
        try:
            # ntfs_attr_open accepts NULL with name_len=0 for an unnamed attribute.
            attr = self.lib.ntfs_attr_open(inode, 0x70, None, 0)
            if not attr:
                raise DriveError("Cannot read the NTFS dirty flag: missing volume information.")
            try:
                buffer = C.create_string_buffer(12)
                if self.lib.ntfs_attr_pread(attr, 0, 12, buffer) != 12:
                    raise DriveError("Cannot read the NTFS dirty flag: incomplete volume information.")
                flags = int.from_bytes(buffer.raw[10:12], "little")
                if write is not None:
                    value = C.create_string_buffer(write.to_bytes(2, "little"))
                    if self.lib.ntfs_attr_pwrite(attr, 10, 2, value) != 2:
                        raise DriveError("Cannot write the NTFS volume flags.")
            finally:
                self.lib.ntfs_attr_close(attr)
        finally:
            closed = self.lib.ntfs_inode_close(inode)
        if closed:
            raise DriveError("Cannot close $Volume after reading the NTFS dirty flag.")
        return flags

    def get(self, path):
        # NTFS-3G clears required size on failure, so use a bounded maximum buffer.
        buffer = C.create_string_buffer(MAX_DESCRIPTOR)
        size = C.c_uint32()
        result = self.lib.ntfs_get_file_security(self.handle, path.encode("utf-8"), 15,
                                                buffer, len(buffer), C.byref(size))
        if not result or not 20 <= size.value <= len(buffer):
            raise DriveError("Cannot read security for {!r}: {}".format(path, os.strerror(C.get_errno())))
        raw = buffer.raw[:size.value]
        Descriptor.parse(raw)
        return raw

    def set(self, path, raw):
        if not self.writable:
            raise DriveError("Refusing to write through a read-only session.")
        Descriptor.parse(raw)
        # Only DACL_SECURITY_INFORMATION: NTFS-3G retains owner, group and SACL.
        if not self.lib.ntfs_set_file_security(self.handle, path.encode("utf-8"), 4,
                                               C.create_string_buffer(ntfs3g_dacl_write(raw))):
            raise DriveError("Cannot write permissions for {!r}: {}".format(path, os.strerror(C.get_errno())))

    def listdir(self, path):
        entries, errors = [], []

        @CALLBACK
        def collect(context, name, length, namespace, position, reference, kind):
            try:
                if namespace == 2:  # Duplicate DOS 8.3 name.
                    return 0
                if not 0 < length <= 255:
                    raise DriveError("Invalid NTFS filename length.")
                text = C.string_at(name, length * 2).decode("utf-16-le", "strict")
                if "/" in text or "\0" in text:
                    raise DriveError("Unsupported NTFS filename.")
                entries.append((text, reference, kind))
                return 0
            except Exception as exc:
                errors.append(exc)
                return -1

        # ntfs_read_directory discards readdir's return code. errno can also be
        # nonzero after successful directory enumeration. Call the underlying
        # public API directly so neither truncated reads nor stale errno pass.
        prefix = C.cast(self.handle, C.POINTER(SecurityAPIPrefix)).contents
        if prefix.magic != 0x09042009 or not prefix.volume:
            raise DriveError("Unsupported NTFS-3G security API layout.")
        inode = self.lib.ntfs_pathname_to_inode(prefix.volume, None, path.encode("utf-8"))
        if not inode:
            raise DriveError("Cannot open directory {!r}: {}".format(path, os.strerror(C.get_errno())))
        position = C.c_int64(0)
        result = self.lib.ntfs_readdir(inode, C.byref(position), None, collect)
        error = C.get_errno()
        closed = self.lib.ntfs_inode_close(inode)
        if closed:
            error = C.get_errno()
        if errors or result or closed:
            raise DriveError("Cannot enumerate {!r}: {}".format(path, errors[0] if errors else os.strerror(error)))
        return entries

    def walk(self):
        root_entries = self.listdir("/")
        root_ref = next((ref for name, ref, _ in root_entries if name == "."), None)
        if root_ref is None:
            raise DriveError("Cannot identify the NTFS root record.")
        stack = [Entry("/", root_ref, True)]
        seen = set()
        self.skipped = []
        self.system_skipped = []
        while stack:
            entry = stack.pop()
            if entry.reference in seen:
                continue
            seen.add(entry.reference)
            attributes = self.lib.ntfs_get_file_attributes(self.handle, entry.path.encode("utf-8"))
            if attributes == -1:
                raise DriveError("Cannot inspect attributes for {!r}.".format(entry.path))
            if attributes & 0x400:  # Reparse point: never follow junctions or symlinks.
                self.skipped.append(entry.path)
                continue
            if attributes & 0x4000:
                raise DriveError("Encrypted file {!r} requires Windows EFS keys; ACL grants cannot decrypt it."
                                 .format(entry.path))
            entry = Entry(entry.path, entry.reference, bool(attributes & 0x10))
            yield entry
            if attributes & 0x10:
                for name, reference, kind in reversed(self.listdir(entry.path)):
                    if name in (".", "..") or reference & MFT_MASK < 16:
                        continue
                    path = entry.path.rstrip("/") + "/" + name
                    if entry.path == "/" and name.casefold() in ("$recycle.bin", "system volume information"):
                        self.system_skipped.append(path)
                        continue
                    stack.append(Entry(path, reference, bool(kind & 4)))
