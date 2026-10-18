import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xbox_media_drive import DriveError
from xbox_media_drive.devices import identity, linux_nodes, validate_target
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
        for info in ({"Internal": True}, {"Internal": False, "Mounted": True},
                     {"Internal": False, "Whole": True, "Content": "GUID_partition_scheme"}):
            with self.subTest(info=info), self.assertRaises(DriveError):
                self.mac_validation(info)
        self.assertEqual(self.mac_validation({"Internal": False, "Mounted": False}), "/dev/disk9s1")

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
