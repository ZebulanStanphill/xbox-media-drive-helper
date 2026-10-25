"""Classify failed NTFS sessions without opening devices."""
import ctypes as C
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from xbox_media_drive import DriveError
from xbox_media_drive.native import NTFS_MNT_EXCLUSIVE, READ_ONLY_FLAGS, SecurityAPIPrefix, Volume, load_library


class FrozenLibraryTests(unittest.TestCase):
    def attempted(self, platform, names):
        with tempfile.TemporaryDirectory() as folder:
            for name in names:
                (Path(folder) / name).write_bytes(b"")
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", folder, create=True), \
                    patch("sys.platform", platform), \
                    patch("xbox_media_drive.native.C.CDLL", side_effect=OSError("not a library")) as cdll:
                with self.assertRaisesRegex(DriveError, "not found"):
                    load_library()
        return [Path(call.args[0]).name for call in cdll.call_args_list]

    def test_linux_bundle_loads_the_shared_object_only(self):
        self.assertEqual(self.attempted("linux", ["libntfs-3g.so.89", "libntfs-3g.91.dylib", "libother.so"]),
                         ["libntfs-3g.so.89"])

    def test_macos_bundle_loads_the_dylib_only(self):
        self.assertEqual(self.attempted("darwin", ["libntfs-3g.so.89", "libntfs-3g.91.dylib"]),
                         ["libntfs-3g.91.dylib"])


class VolumeOpenTests(unittest.TestCase):
    def failure(self, error, probe=None, uid=0, writable=True, platform="linux"):
        lib = Mock()
        initializer = Mock(side_effect=[None, probe])
        with patch("xbox_media_drive.native.load_library", return_value=lib), \
                patch.object(Volume, "_initialize", initializer), \
                patch("xbox_media_drive.native.C.get_errno", return_value=error) as get_errno, \
                patch("xbox_media_drive.native.os.getuid", return_value=uid), \
                patch("xbox_media_drive.native.sys.platform", platform):
            with self.assertRaises(DriveError) as raised:
                Volume("/test/partition", writable=writable)
            get_errno.assert_called_once_with()
        self.assertEqual(initializer.call_args_list[0].args,
                         ("/test/partition", NTFS_MNT_EXCLUSIVE if writable else READ_ONLY_FLAGS))
        if initializer.call_count == 2:
            self.assertEqual(initializer.call_args_list[1].args, ("/test/partition", READ_ONLY_FLAGS))
        return str(raised.exception), initializer, lib

    def test_open_journal_probe_is_released_and_linux_guidance(self):
        message, initializer, lib = self.failure(errno.EOPNOTSUPP, probe=123)
        self.assertIn("NTFS journal is still open", message)
        self.assertIn("No permissions changed", message)
        self.assertIn("Run sudo ntfsfix -d /test/partition", message)
        self.assertIn("Keep the -d", message)
        self.assertIn("copy off anything you need before letting it repair", message)
        self.assertEqual(initializer.call_count, 2)
        lib.ntfs_leave_file_security.assert_called_once_with(123)

    def test_failed_probe_uses_original_error(self):
        for error in (errno.EOPNOTSUPP, errno.EPERM):
            with self.subTest(error=error):
                message, initializer, lib = self.failure(error)
                self.assertIn("NTFS-3G refused the volume (" + os.strerror(error), message)
                self.assertEqual(initializer.call_count, 2)
                lib.ntfs_leave_file_security.assert_not_called()

    def test_root_cached_metadata(self):
        message, _, lib = self.failure(errno.EPERM, probe=123)
        self.assertIn("Windows kept this drive's metadata cached", message)
        self.assertIn("copy off anything you need before letting it repair", message)
        self.assertIn("No permissions changed", message)
        lib.ntfs_leave_file_security.assert_called_once_with(123)

    def test_nonroot_does_not_probe(self):
        message, initializer, _ = self.failure(errno.EPERM, uid=501)
        self.assertIn("requires root", message)
        self.assertEqual(initializer.call_count, 1)

    def test_read_only_does_not_probe(self):
        for error in (errno.EOPNOTSUPP, errno.EPERM):
            with self.subTest(error=error):
                _, initializer, lib = self.failure(error, writable=False)
                self.assertEqual(initializer.call_count, 1)
                lib.ntfs_leave_file_security.assert_not_called()

    def test_macos_guidance(self):
        message, _, _ = self.failure(errno.EOPNOTSUPP, probe=123, platform="darwin")
        self.assertIn("On a Linux computer", message)
        self.assertIn("sudo ntfsfix -d /dev/sdb1", message)
        self.assertNotIn("sudo ntfsfix -d /test/partition", message)

    def test_busy_and_access_messages_do_not_probe(self):
        for error, expected in ((errno.EBUSY, "mounted or in use"), (errno.EACCES, "not accessible")):
            with self.subTest(error=error):
                message, initializer, _ = self.failure(error)
                self.assertIn(expected, message)
                self.assertEqual(initializer.call_count, 1)


class VolumeDirtyTests(unittest.TestCase):
    def volume(self):
        volume = Volume.__new__(Volume)
        volume.lib = Mock()
        volume.lib.ntfs_inode_open.return_value = 123
        volume.lib.ntfs_attr_open.return_value = 456
        volume.lib.ntfs_inode_close.return_value = 0
        prefix = SecurityAPIPrefix()
        prefix.magic = 0x09042009
        prefix.volume = 789
        volume.handle = C.pointer(prefix)
        return volume

    def test_unreadable_flag_raises_and_closes_resources(self):
        for failure in ("inode", "attribute", "short-read"):
            with self.subTest(failure=failure):
                volume = self.volume()
                if failure == "inode":
                    volume.lib.ntfs_inode_open.return_value = None
                elif failure == "attribute":
                    volume.lib.ntfs_attr_open.return_value = None
                else:
                    volume.lib.ntfs_attr_pread.return_value = 11
                with self.assertRaisesRegex(DriveError, "Cannot read the NTFS dirty flag"):
                    volume.dirty()
                self.assertEqual(volume.lib.ntfs_inode_close.call_count, failure != "inode")
                self.assertEqual(volume.lib.ntfs_attr_close.call_count, failure == "short-read")

    def test_other_flags_do_not_mean_dirty(self):
        for flags in (0, 1, 2, 0x8001):
            with self.subTest(flags=flags):
                volume = self.volume()
                def read(attr, offset, count, buffer):
                    C.memmove(buffer, b"\0" * 10 + flags.to_bytes(2, "little"), 12)
                    return 12
                volume.lib.ntfs_attr_pread.side_effect = read
                self.assertEqual(volume.dirty(), bool(flags & 1))
                volume.lib.ntfs_attr_close.assert_called_once_with(456)
                volume.lib.ntfs_inode_close.assert_called_once_with(123)

    def test_clear_dirty_preserves_other_bits(self):
        for flags in (1, 0x8003, 0x0181):
            with self.subTest(flags=flags):
                volume = self.volume()
                volume.writable = True
                def read(attr, offset, count, buffer):
                    C.memmove(buffer, b"\0" * 10 + flags.to_bytes(2, "little"), 12)
                    return 12
                volume.lib.ntfs_attr_pread.side_effect = read
                volume.lib.ntfs_attr_pwrite.return_value = 2
                volume.clear_dirty()
                call = volume.lib.ntfs_attr_pwrite.call_args.args
                self.assertEqual(call[:3], (456, 10, 2))
                self.assertEqual(C.string_at(call[3], 2), (flags & ~1).to_bytes(2, "little"))
                volume.lib.ntfs_attr_pwrite.assert_called_once()

    def test_clear_dirty_read_only_refused(self):
        volume = self.volume()
        volume.writable = False
        with self.assertRaisesRegex(DriveError, "read-only"):
            volume.clear_dirty()
        volume.lib.ntfs_attr_pwrite.assert_not_called()

    def test_clear_dirty_write_failure_closes_resources(self):
        volume = self.volume()
        volume.writable = True
        def read(attr, offset, count, buffer):
            C.memmove(buffer, b"\0" * 10 + b"\x81\x01", 12)
            return 12
        volume.lib.ntfs_attr_pread.side_effect = read
        volume.lib.ntfs_attr_pwrite.return_value = -1
        with self.assertRaisesRegex(DriveError, "Cannot write the NTFS volume flags"):
            volume.clear_dirty()
        self.assertEqual(volume.lib.ntfs_attr_close.call_count, 2)
        self.assertEqual(volume.lib.ntfs_inode_close.call_count, 2)
