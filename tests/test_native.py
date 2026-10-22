"""Classify failed NTFS sessions without opening devices."""
import errno
import os
import unittest
from unittest.mock import Mock, patch

from xbox_media_drive import DriveError
from xbox_media_drive.native import NTFS_MNT_EXCLUSIVE, READ_ONLY_FLAGS, Volume


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
