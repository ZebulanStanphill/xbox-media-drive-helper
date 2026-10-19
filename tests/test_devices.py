import json
import os
import plistlib
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import subprocess
from unittest.mock import patch

from xbox_media_drive import DriveError
from xbox_media_drive.devices import discover, identity, is_mounted, linux_nodes, unmount, validate_target
from tests.test_operations import make_boot


class DeviceTests(unittest.TestCase):
    def test_volume_identity_requires_ntfs_boot_sector(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "volume.img"
            image.write_bytes(make_boot())
            self.assertEqual(identity(image)["volume_bytes"], 16777216)
            image.write_bytes(b"\0" * 512)
            with self.assertRaisesRegex(DriveError, "not an NTFS"):
                identity(image)

    def test_mac_discovery_reports_mounted_from_mount_point(self):
        # Real diskutil plists have MountPoint ("" when unmounted) and no Mounted key.
        listing = plistlib.dumps({"AllDisksAndPartitions": [{"DeviceIdentifier": "disk4", "Partitions": [
            {"DeviceIdentifier": "disk4s1"}, {"DeviceIdentifier": "disk4s2"}]}]})
        infos = {"/dev/disk4s1": {"VolumeName": "Media", "TotalSize": 1, "FilesystemType": "ntfs", "MountPoint": "/Volumes/Media"},
                 "/dev/disk4s2": {"VolumeName": "Other", "TotalSize": 1, "FilesystemType": "ntfs", "MountPoint": ""}}
        with patch("sys.platform", "darwin"), patch("xbox_media_drive.devices.run", return_value=listing), \
                patch("xbox_media_drive.devices.mac_info", side_effect=infos.__getitem__):
            self.assertEqual([device.mounted for device in discover()], [True, False])

    def test_linux_external_status_inherits_from_parent_disk(self):
        disk = {"path": "/dev/sdz", "type": "disk", "tran": "usb", "children": [
            {"path": "/dev/sdz1", "type": "part", "tran": None}]}
        with patch("xbox_media_drive.devices.run", return_value=json.dumps({"blockdevices": [disk]}).encode()):
            self.assertEqual([external for node, external in linux_nodes()], [True, True])

    def mac_validation(self, info):
        with patch("sys.platform", "darwin"), patch.object(Path, "resolve", return_value=Path("/dev/disk9s1")), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                patch("xbox_media_drive.devices.mac_info", return_value=info):
            return validate_target("/dev/disk9s1")

    def test_mac_refuses_internal_mounted_and_partitioned_whole_disks(self):
        for info in ({"Internal": True}, {"Internal": False, "MountPoint": "/Volumes/X"},
                     {"Internal": False, "WholeDisk": True, "Content": "GUID_partition_scheme"}):
            with self.subTest(info=info), self.assertRaises(DriveError):
                self.mac_validation(info)
        self.assertEqual(self.mac_validation({"Internal": False, "MountPoint": ""}), "/dev/disk9s1")

    def test_attached_mac_image_is_refused(self):
        import plistlib
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "media.img"
            image.write_bytes(make_boot())
            data = plistlib.dumps({"images": [{"image-path": str(image.resolve())}]})
            with patch("sys.platform", "darwin"), patch("xbox_media_drive.devices.run", return_value=data):
                with self.assertRaisesRegex(DriveError, "Detach"):
                    validate_target(str(image))

    def test_hard_linked_host_image_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            image, alias = Path(folder) / "a.img", Path(folder) / "b.img"
            image.write_bytes(make_boot())
            os.link(image, alias)
            with self.assertRaisesRegex(DriveError, "one host"):
                validate_target(str(image))

    def test_linux_refuses_internal_mounted_and_whole_partitioned_devices(self):
        # /dev/null supplies a real device stat; lsblk/mount discovery is isolated.
        for node, external in (({"path": "/dev/null"}, False),
                               ({"path": "/dev/null", "mountpoints": ["/media/drive"]}, True),
                               ({"path": "/dev/null", "children": [{"path": "/dev/child"}]}, True)):
            with self.subTest(node=node), patch("sys.platform", "linux"), \
                    patch("xbox_media_drive.devices.linux_nodes", return_value=[(node, external)]):
                with self.assertRaises(DriveError):
                    validate_target("/dev/null")

    def test_missing_device_reports_that_it_is_no_longer_connected(self):
        with self.assertRaisesRegex(DriveError, "no longer connected.*Unmount in this app"):
            validate_target("/dev/disk99s1")

    def test_missing_image_is_reported_as_not_found(self):
        with self.assertRaisesRegex(DriveError, "was not found"):
            validate_target("/nonexistent/media.img")

    def mac_unmount(self, info, path="/dev/rdisk9s1", **kwargs):
        resolved = Path(path)
        with patch("sys.platform", "darwin"), patch.object(Path, "resolve", return_value=resolved), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
                patch("xbox_media_drive.devices.mac_info", return_value=info), \
                patch("xbox_media_drive.devices.run", **kwargs) as run:
            return unmount(path), run

    def test_mac_unmount_uses_diskutil_unmount_on_the_buffered_partition(self):
        for path in ("/dev/disk9s1", "/dev/rdisk9s1"):
            with self.subTest(path=path):
                target, run = self.mac_unmount({"Internal": False}, path)
                self.assertEqual(target, "/dev/disk9s1")
                run.assert_called_once_with(["diskutil", "unmount", "/dev/disk9s1"])

    def test_unmount_refuses_internal_virtual_and_partitioned_whole_disks(self):
        for info in ({"Internal": True}, {"Internal": False, "VirtualOrPhysical": "Virtual"},
                     {"Internal": False, "WholeDisk": True, "Content": "GUID_partition_scheme"}):
            with self.subTest(info=info), self.assertRaises(DriveError):
                self.mac_unmount(info)

    def test_unmount_failure_includes_tool_output_and_a_hint(self):
        failure = subprocess.CalledProcessError(1, ["diskutil"], b"", b"Volume Media failed to unmount: dissented by PID 42")
        with patch("sys.platform", "darwin"), patch.object(Path, "resolve", return_value=Path("/dev/disk9s1")), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                patch("xbox_media_drive.devices.mac_info", return_value={"Internal": False}), \
                patch("xbox_media_drive.devices.subprocess.run", side_effect=failure):
            with self.assertRaisesRegex(DriveError, "dissented by PID 42(.|\n)*Close any apps"):
                unmount("/dev/disk9s1")

    def linux_unmount(self, node, external=True, udisksctl="/usr/bin/udisksctl"):
        with patch("sys.platform", "linux"), \
                patch("xbox_media_drive.devices.linux_nodes", return_value=[(node, external)]), \
                patch("xbox_media_drive.devices.shutil.which", return_value=udisksctl), \
                patch("xbox_media_drive.devices.run") as run:
            return unmount("/dev/null"), run

    def test_linux_unmount_uses_udisksctl_for_external_partitions_only(self):
        target, run = self.linux_unmount({"path": "/dev/null"})
        run.assert_called_once_with(["udisksctl", "unmount", "-b", "/dev/null"])
        for node, external in (({"path": "/dev/null"}, False), ({"path": "/dev/null", "children": [{}]}, True)):
            with self.subTest(node=node), self.assertRaises(DriveError):
                self.linux_unmount(node, external)

    def test_linux_unmount_explains_missing_udisksctl(self):
        with self.assertRaisesRegex(DriveError, "file manager or sudo umount /dev/null"):
            self.linux_unmount({"path": "/dev/null"}, udisksctl=None)

    def test_is_mounted_ignores_images_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "volume.img"
            image.write_bytes(make_boot())
            self.assertFalse(is_mounted(str(image)))
        self.assertFalse(is_mounted("/dev/disk99s1"))

    def test_is_mounted_reads_platform_mount_state(self):
        with patch("sys.platform", "darwin"), patch.object(Path, "resolve", return_value=Path("/dev/disk9s1")), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)):
            for info, expected in (({"MountPoint": "/Volumes/X"}, True), ({"MountPoint": ""}, False), ({}, False)):
                with self.subTest(info=info), patch("xbox_media_drive.devices.mac_info", return_value=info):
                    self.assertEqual(is_mounted("/dev/disk9s1"), expected)
        for node, expected in (({"path": "/dev/null", "mountpoints": ["/media/x"]}, True),
                               ({"path": "/dev/null", "mountpoints": [None]}, False)):
            with self.subTest(node=node), patch("sys.platform", "linux"), \
                    patch("xbox_media_drive.devices.linux_nodes", return_value=[(node, True)]):
                self.assertEqual(is_mounted("/dev/null"), expected)
