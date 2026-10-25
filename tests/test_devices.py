import errno
import json
import os
import plistlib
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import subprocess
from unittest.mock import mock_open, patch

from xbox_media_drive import DriveError, devices
from xbox_media_drive.devices import mac_full_disk_access, discover, identity, is_mounted, linux_nodes, unmount, validate_target
from tests.test_operations import make_boot


class DeviceTests(unittest.TestCase):
    def test_mac_full_disk_access(self):
        with patch("sys.platform", "linux"), patch("builtins.open") as opened:
            self.assertIsNone(mac_full_disk_access())
            opened.assert_not_called()
        with patch("sys.platform", "darwin"), patch("builtins.open", mock_open()) as opened:
            self.assertIs(mac_full_disk_access(), True)
            opened.assert_called_once_with("/Library/Application Support/com.apple.TCC/TCC.db", "rb")
            opened().__exit__.assert_called_once()
        for error, expected in ((PermissionError(errno.EPERM, "denied"), False),
                                (FileNotFoundError(errno.ENOENT, "missing"), None),
                                (PermissionError(errno.EACCES, "denied"), None)):
            with self.subTest(error=error), patch("sys.platform", "darwin"), \
                    patch("builtins.open", side_effect=error):
                self.assertIs(mac_full_disk_access(), expected)

    def test_volume_identity_requires_ntfs_boot_sector(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "volume.img"
            image.write_bytes(make_boot())
            self.assertEqual(identity(image)["volume_bytes"], 16777216)
            image.write_bytes(b"\0" * 512)
            with self.assertRaisesRegex(DriveError, "not an NTFS"):
                identity(image)
            image.write_bytes(b"\xebv\x90EXFAT   " + b"\0" * 498 + b"\x55\xaa")
            with self.assertRaisesRegex(DriveError, "exFAT, not NTFS"):
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

    def test_mac_privacy_denial_explains_full_disk_access(self):
        denied = PermissionError(1, "Operation not permitted")
        with patch("sys.platform", "darwin"), patch("builtins.open", side_effect=denied):
            with self.assertRaisesRegex(DriveError, "Full Disk Access"):
                identity("/dev/disk8s1")
        with patch("sys.platform", "linux"), patch("builtins.open", side_effect=denied):
            with self.assertRaises(PermissionError):
                identity("/dev/sdb1")

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


class WholeDiskTests(unittest.TestCase):
    def test_mac_discovery_partitionless_disk_has_media_label_and_disk_path(self):
        listing = plistlib.dumps({"AllDisksAndPartitions": [{"DeviceIdentifier": "disk4"}]})
        with patch("sys.platform", "darwin"), patch("xbox_media_drive.devices.run", return_value=listing), \
                patch("xbox_media_drive.devices.mac_info", return_value={"MediaName": "USB", "Content": "FDisk_partition_scheme"}):
            device = discover()[0]
            self.assertEqual((device.label, device.disk, device.filesystem), ("USB", "/dev/disk4", "FDisk_partition_scheme"))

    def test_linux_parent_chain_resolves_stacked_device(self):
        from xbox_media_drive.devices import whole_disk
        nodes = [({"path": "/dev/sdz", "type": "disk"}, True),
                 ({"path": "/dev/sdz1", "type": "part", "pkname": "sdz"}, True),
                 ({"path": "/dev/dm-0", "type": "crypt", "pkname": "/dev/sdz1"}, True)]
        with patch("sys.platform", "linux"), patch("xbox_media_drive.devices.resolve_path", return_value="/dev/dm-0"), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                patch("xbox_media_drive.devices.linux_nodes", return_value=nodes):
            self.assertEqual(whole_disk("/dev/dm-0"), "/dev/sdz")

    def test_game_mode_rejects_images(self):
        from xbox_media_drive.devices import validate_whole_disk
        with tempfile.NamedTemporaryFile() as image:
            with self.assertRaisesRegex(DriveError, "Game-drive mode applies to a whole external disk, not an image"):
                validate_whole_disk(image.name)

    def test_mac_parent_resolution_and_whole_unmount(self):
        from xbox_media_drive.devices import whole_disk
        infos = {"/dev/disk9s1": {"Internal": False, "ParentWholeDisk": "disk9"},
                 "/dev/disk9": {"Internal": False, "WholeDisk": True}}
        with patch("sys.platform", "darwin"), patch("xbox_media_drive.devices.resolve_path", return_value="/dev/disk9s1"), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                patch("xbox_media_drive.devices.mac_info", side_effect=infos.__getitem__), \
                patch("xbox_media_drive.devices.run") as run:
            self.assertEqual(whole_disk("/dev/disk9s1"), "/dev/disk9")
            self.assertEqual(unmount("/dev/disk9s1", whole=True), "/dev/disk9")
            run.assert_called_once_with(["diskutil", "unmountDisk", "/dev/disk9"])

    def test_whole_disk_validation_refuses_mounted_children(self):
        from xbox_media_drive.devices import validate_whole_disk
        with patch("xbox_media_drive.devices.whole_disk", return_value="/dev/sdz"), \
                patch("xbox_media_drive.devices.is_disk_mounted", return_value=True), \
                patch("sys.platform", "linux"), patch("xbox_media_drive.devices.linux_device"):
            with self.assertRaisesRegex(DriveError, "Click Unmount"):
                validate_whole_disk("/dev/sdz1")

    def test_mac_disk_mount_state_checks_child_volumes(self):
        from xbox_media_drive.devices import is_disk_mounted
        listing = plistlib.dumps({"AllDisks": ["disk9", "disk9s1"]})
        infos = {"/dev/disk9": {}, "/dev/disk9s1": {"MountPoint": "/Volumes/Media"}}
        with patch("sys.platform", "darwin"), patch("xbox_media_drive.devices.whole_disk", return_value="/dev/disk9"), \
                patch("xbox_media_drive.devices.run", return_value=listing), \
                patch("xbox_media_drive.devices.mac_info", side_effect=infos.__getitem__):
            self.assertTrue(is_disk_mounted("/dev/disk9"))

    def test_linux_disk_mount_state_and_unmount_include_descendants(self):
        from xbox_media_drive.devices import is_disk_mounted
        children = [{"path": "/dev/sdz1", "type": "part", "mountpoints": ["/media/a"]},
                    {"path": "/dev/sdz2", "type": "part", "mountpoints": [None]}]
        disk = {"path": "/dev/sdz", "type": "disk", "children": children}
        with patch("sys.platform", "linux"), patch("xbox_media_drive.devices.whole_disk", return_value="/dev/sdz"), \
                patch("xbox_media_drive.devices.linux_nodes", return_value=[(disk, True)]), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                patch("xbox_media_drive.devices.shutil.which", return_value="/usr/bin/udisksctl"), \
                patch("xbox_media_drive.devices.run") as run:
            self.assertTrue(is_disk_mounted("/dev/sdz"))
            self.assertEqual(unmount("/dev/sdz", whole=True), "/dev/sdz")
            run.assert_called_once_with(["udisksctl", "unmount", "-b", "/dev/sdz1"])

    def test_mac_discovery_parent_and_unnamed_fallback(self):
        listing = plistlib.dumps({"AllDisksAndPartitions": [{"DeviceIdentifier": "disk9", "Partitions": [
            {"DeviceIdentifier": "disk9s1"}]}]})
        with patch("sys.platform", "darwin"), patch("xbox_media_drive.devices.run", return_value=listing), \
                patch("xbox_media_drive.devices.mac_info", return_value={"ParentWholeDisk": "disk10"}):
            device = discover()[0]
            self.assertEqual((device.label, device.disk), ("Unnamed volume", "/dev/disk10"))

    def test_linux_discovery_sets_parent_disk(self):
        disk = {"path": "/dev/sdz", "type": "disk", "tran": "usb", "children": [
            {"path": "/dev/sdz1", "type": "part"}]}
        with patch("sys.platform", "linux"), patch("xbox_media_drive.devices.run", return_value=json.dumps({"blockdevices": [disk]}).encode()):
            self.assertEqual(discover()[0].disk, "/dev/sdz")

    def test_mac_resolution_allows_internal_but_validation_and_unmount_refuse(self):
        from xbox_media_drive.devices import whole_disk, validate_whole_disk
        for info in ({"WholeDisk": True, "Internal": True},
                     {"WholeDisk": True, "Internal": False, "VirtualOrPhysical": "Virtual"}):
            with self.subTest(info=info), patch("sys.platform", "darwin"), \
                    patch("xbox_media_drive.devices.resolve_path", return_value="/dev/disk9"), \
                    patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                    patch("xbox_media_drive.devices.mac_info", return_value=info):
                self.assertEqual(whole_disk("/dev/disk9"), "/dev/disk9")
                with self.assertRaisesRegex(DriveError, "external physical"):
                    validate_whole_disk("/dev/disk9")
                with self.assertRaisesRegex(DriveError, "external physical"):
                    unmount("/dev/disk9", whole=True)

    def test_linux_resolution_internal_and_rejection_of_virtual_devices(self):
        from xbox_media_drive.devices import whole_disk, validate_whole_disk
        for kind in ("disk", "loop", "crypt"):
            nodes = [({"path": "/dev/sdz", "type": kind}, False)]
            with self.subTest(kind=kind), patch("sys.platform", "linux"), \
                    patch("xbox_media_drive.devices.resolve_path", return_value="/dev/sdz"), \
                    patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
                    patch("xbox_media_drive.devices.linux_nodes", return_value=nodes):
                if kind == "disk":
                    self.assertEqual(whole_disk("/dev/sdz"), "/dev/sdz")
                else:
                    with self.assertRaises(DriveError):
                        whole_disk("/dev/sdz")
                with self.assertRaises(DriveError):
                    validate_whole_disk("/dev/sdz")
                with self.assertRaises(DriveError):
                    unmount("/dev/sdz", whole=True)

    def test_linux_disk_mount_state_checks_kernel_mount_aliases(self):
        from xbox_media_drive.devices import is_disk_mounted
        node = {"path": "/dev/sdz", "type": "disk", "mountpoints": [None]}
        with patch("sys.platform", "linux"), patch("xbox_media_drive.devices.whole_disk", return_value="/dev/sdz"), \
                patch("xbox_media_drive.devices.linux_nodes", return_value=[(node, True)]), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_rdev=os.makedev(8, 240))), \
                patch.object(Path, "read_text", return_value="1 2 8:240 / /mnt rw - ntfs /dev/alias rw"):
            self.assertTrue(is_disk_mounted("/dev/sdz"))
        with patch("sys.platform", "linux"), patch("xbox_media_drive.devices.whole_disk", return_value="/dev/sdz"), \
                patch("xbox_media_drive.devices.linux_nodes", return_value=[(node, True)]), \
                patch("xbox_media_drive.devices.os.stat", return_value=SimpleNamespace(st_rdev=os.makedev(8, 240))), \
                patch.object(Path, "read_text", return_value=""):
            self.assertFalse(is_disk_mounted("/dev/sdz"))


class SystemEnvironmentTests(unittest.TestCase):
    def environment(self, environ, frozen=True, platform="linux"):
        with patch.object(devices.sys, "frozen", frozen, create=True), patch.object(devices.sys, "platform", platform):
            return devices.system_environment(environ)

    def test_frozen_linux_restores_the_original_library_path(self):
        env = self.environment({"LD_LIBRARY_PATH": "/tmp/.mount_x/_internal", "LD_LIBRARY_PATH_ORIG": "/opt/lib",
                                "HOME": "/home/u"})
        self.assertEqual(env, {"LD_LIBRARY_PATH": "/opt/lib", "HOME": "/home/u"})

    def test_frozen_linux_drops_the_bundle_path_when_there_was_none(self):
        self.assertEqual(self.environment({"LD_LIBRARY_PATH": "/tmp/.mount_x/_internal"}), {})

    def test_other_builds_are_unchanged(self):
        environ = {"LD_LIBRARY_PATH": "/opt/lib", "LD_LIBRARY_PATH_ORIG": "/x"}
        self.assertEqual(self.environment(environ, frozen=False), environ)
        self.assertEqual(self.environment(environ, platform="darwin"), environ)

    def test_run_passes_the_system_environment(self):
        with patch.object(devices, "system_environment", return_value={"A": "1"}), \
                patch.object(devices.subprocess, "run", return_value=SimpleNamespace(stdout=b"ok")) as run:
            self.assertEqual(devices.run(["lsblk"]), b"ok")
        self.assertEqual(run.call_args.kwargs["env"], {"A": "1"})
