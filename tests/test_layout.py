import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xbox_media_drive import DriveError
from xbox_media_drive.layout import (PC_SIGNATURE, XBOX_SIGNATURE, analyze, read_head,
                                     restore_sector, switch, switched, _write_verified)


def header(signature=PC_SIGNATURE, gpt=512, mbr=True, boot=False):
    head = bytearray(8192)
    head[510:512] = signature
    if gpt:
        head[gpt:gpt + 8] = b"EFI PART"
    if mbr:
        head[450] = 0xEE if gpt else 7
        head[458:462] = (100).to_bytes(4, "little")
    if boot:
        head[0] = 0xEB
    return bytes(head)


class LayoutTests(unittest.TestCase):
    def test_layouts(self):
        for offset in (512, 4096):
            for boot in (False, True):
                info = analyze(header(gpt=offset, boot=boot))
                self.assertEqual(info["scheme"], "gpt")
                self.assertEqual(info["xbox_formatted"], not boot)
        self.assertEqual(analyze(header(gpt=None))["scheme"], "mbr")
        self.assertEqual(analyze(header(gpt=None, mbr=False))["scheme"], "none")
        self.assertEqual(analyze(header(signature=b"xx"))["signature"], "unknown")
        self.assertEqual(analyze(header(XBOX_SIGNATURE))["mode"], "game")

    def test_switched_only_changes_signature(self):
        head = header(mbr=False)
        result = switched(head, "game")
        self.assertEqual(result, head[:510] + XBOX_SIGNATURE)
        self.assertEqual(len(result), 512)
        self.assertEqual(switched(header(XBOX_SIGNATURE), "media"), header()[:512])
        signed = bytearray(header())
        signed[440:444] = b"\x12\x34\x56\x78"
        self.assertEqual(analyze(signed)["nt_disk_signature"], "12345678")
        self.assertEqual(switched(signed, "game")[440:444], signed[440:444])
        for bad, mode in ((header(b"xx"), "game"), (header(), "media"),
                          (header(gpt=None, mbr=False), "game"), (header(), "other")):
            with self.assertRaises(DriveError):
                switched(bad, mode)
        self.assertEqual(switched(header()[:512], "game")[510:], XBOX_SIGNATURE)

    def test_switch_backup_and_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            disk, backup = Path(temp) / "disk", Path(temp) / "backup"
            original = header()
            disk.write_bytes(original)
            result = switch(str(disk), "game", str(backup))
            self.assertEqual(result["previous_mode"], "media")
            self.assertEqual(backup.read_bytes(), original[:512])
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(disk.read_bytes(), original[:510] + XBOX_SIGNATURE + original[512:])
            self.assertEqual(restore_sector(str(disk), str(backup))["mode"], "media")
            self.assertEqual(disk.read_bytes(), original)
            with self.assertRaisesRegex(DriveError, "already exists"):
                switch(str(disk), "game", str(backup))
            with self.assertRaises(DriveError):
                switch(str(disk), "game", str(disk))

    def test_same_mode_does_not_create_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            disk, backup = Path(temp) / "disk", Path(temp) / "backup"
            disk.write_bytes(header())
            with self.assertRaisesRegex(DriveError, "already in media"):
                switch(str(disk), "media", str(backup))
            self.assertFalse(backup.exists())
            self.assertEqual(disk.read_bytes(), header())

    def test_switch_readback_failure_reverts_actual_file(self):
        from unittest.mock import MagicMock
        real_open = open
        with tempfile.TemporaryDirectory() as temp:
            disk, backup = Path(temp) / "disk", Path(temp) / "backup"
            original = header()
            disk.write_bytes(original)
            reads = []

            def open_disk(path, mode="r", **kwargs):
                stream = real_open(path, mode, **kwargs)
                if str(path) != str(disk) or mode != "rb+":
                    return stream
                wrapper = MagicMock(wraps=stream)
                wrapper.__enter__.return_value = wrapper
                wrapper.__exit__.side_effect = lambda *args: stream.close()

                def read(size):
                    data = stream.read(size)
                    reads.append(data)
                    return b"wrong" if len(reads) == 1 else data

                wrapper.read.side_effect = read
                return wrapper

            with patch("xbox_media_drive.layout.open", side_effect=open_disk):
                with self.assertRaisesRegex(DriveError, "restored and verified") as error:
                    switch(str(disk), "game", str(backup))
            self.assertIn(str(backup), str(error.exception))
            self.assertEqual(disk.read_bytes(), original)
            self.assertEqual(backup.read_bytes(), original[:512])
            self.assertEqual(len(reads), 2)
            self.assertEqual(reads[0][510:], XBOX_SIGNATURE)
            self.assertEqual(reads[1], original[:512])

    def test_restore_rejects_foreign_and_malformed(self):
        with tempfile.TemporaryDirectory() as temp:
            disk, backup = Path(temp) / "disk", Path(temp) / "backup"
            disk.write_bytes(header())
            backup.write_bytes(header(boot=True)[:512])
            with self.assertRaisesRegex(DriveError, "belongs to a different disk"):
                restore_sector(str(disk), str(backup))
            for content in (b"", header()[:511], header()[:513], header(b"xx")[:512]):
                backup.write_bytes(content)
                with self.assertRaisesRegex(DriveError, "exactly one"):
                    restore_sector(str(disk), str(backup))
            self.assertEqual(disk.read_bytes(), header())

    def test_read_short_and_mac_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            disk = Path(temp) / "disk"
            disk.write_bytes(bytes(512))
            with self.assertRaisesRegex(DriveError, "too short"):
                read_head(str(disk))
        with patch("xbox_media_drive.layout.sys.platform", "darwin"), patch("builtins.open", side_effect=PermissionError(errno.EPERM, "denied")):
            with self.assertRaisesRegex(DriveError, "Full Disk Access"):
                read_head("/dev/disk9")

    def test_verification_failure_restores_once(self):
        from unittest.mock import MagicMock
        stream = MagicMock()
        stream.write.return_value = 512
        stream.read.side_effect = [b"wrong", b"a" * 512]
        with patch("os.fsync"):
            with self.assertRaisesRegex(DriveError, "restored and verified.*backup.bin"):
                _write_verified(stream, b"b" * 512, b"a" * 512, "backup.bin")
        self.assertEqual(stream.write.call_count, 2)
        stream.read.side_effect = [b"wrong", b"wrong"]
        with patch("os.fsync"):
            with self.assertRaisesRegex(DriveError, "could not be restored"):
                _write_verified(stream, b"b" * 512, b"a" * 512, "backup.bin")

    def test_no_backup_is_left_when_the_disk_cannot_be_opened_for_writing(self):
        import tempfile
        from pathlib import Path
        from xbox_media_drive import layout
        head = bytearray(8192)
        head[446 + 4] = 0x07
        head[446 + 12:446 + 16] = (100).to_bytes(4, "little")
        head[510:512] = b"\x55\xaa"
        real_open = layout.open_disk

        def denied(path, writable=False):
            if writable:
                raise DriveError("blocked by privacy settings")
            return real_open(path)

        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "disk.img"
            image.write_bytes(bytes(head))
            backup = Path(folder) / "sector.mbr"
            with patch("xbox_media_drive.layout.open_disk", side_effect=denied):
                with self.assertRaisesRegex(DriveError, "privacy settings"):
                    switch(str(image), "game", str(backup))
            self.assertFalse(backup.exists())
            self.assertEqual(image.read_bytes(), bytes(head))
