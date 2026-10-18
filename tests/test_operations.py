import json
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xbox_media_drive import DriveError
from xbox_media_drive.native import Entry
from xbox_media_drive.operations import inspect, journal_info, prepare, restore
from xbox_media_drive.security import grant, signature
from tests.test_security import ace, descriptor


def make_boot(serial=b"12345678"):
    boot = bytearray(512)
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 11, 512)
    boot[13] = 8
    struct.pack_into("<Q", boot, 40, 32768)
    boot[72:80] = serial
    boot[510:512] = b"\x55\xaa"
    return boot


class MemoryVolume:
    def __init__(self):
        self.files = {"/": [5, True, descriptor([ace()])],
                      "/Movies": [70, True, descriptor([ace()])],
                      "/Movies/film.mkv": [71, False, descriptor([ace()])]}
        self.fail_at = None
        self.writes = 0
        self.skipped = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def walk(self):
        for path, (ref, directory, raw) in self.files.items():
            yield Entry(path, ref, directory)

    def get(self, path):
        return self.files[path][2]

    def set(self, path, raw):
        self.files[path][2] = raw
        self.writes += 1
        # Model a successful NTFS write followed by failure before journal marking.
        if self.writes == self.fail_at:
            raise DriveError("simulated interruption after NTFS write")


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.target = str(Path(self.temp.name) / "volume.img")
        self.backup = str(Path(self.temp.name) / "recovery.sqlite")
        Path(self.target).write_bytes(make_boot())
        self.volume = MemoryVolume()
        self.original = {path: signature(values[2]) for path, values in self.volume.files.items()}
        # The in-memory backend has no operating-system image attachments.
        self.validation = patch("xbox_media_drive.operations.validate_target", side_effect=lambda target: target)
        self.validation.start()

    def tearDown(self):
        self.validation.stop()
        self.temp.cleanup()

    def prepare(self):
        return prepare(self.target, self.backup, volume_factory=self.volume)

    def restore(self):
        return restore(self.target, self.backup, volume_factory=self.volume)

    def test_read_only_inspection_and_round_trip(self):
        info = inspect(self.target, volume_factory=self.volume)
        self.assertEqual(info["changes_needed"], 3)
        self.assertEqual(self.volume.writes, 0)
        self.assertEqual(self.prepare()["changed"], 3)
        self.assertEqual(os.stat(self.backup).st_mode & 0o777, 0o600)
        self.assertEqual(self.restore()["restored"], 3)
        for path in self.original:
            self.assertEqual(signature(self.volume.get(path)), self.original[path])
        self.assertEqual(self.restore()["already_original"], 3)

    def test_interrupted_unmarked_write_is_recoverable(self):
        self.volume.fail_at = 2
        with self.assertRaisesRegex(DriveError, "interruption"):
            self.prepare()
        self.assertEqual(journal_info(self.backup)["state"], "interrupted")
        self.assertEqual(self.restore()["restored"], 2)
        for path in self.original:
            self.assertEqual(signature(self.volume.get(path)), self.original[path])

    def test_all_preflight_completes_before_first_acl_write(self):
        self.volume.files["/Movies/film.mkv"][2] = descriptor([ace(kind=1)])
        with self.assertRaisesRegex(DriveError, "deny"):
            self.prepare()
        self.assertEqual(self.volume.writes, 0)

    def test_host_journal_failure_prevents_acl_writes(self):
        with patch("xbox_media_drive.operations.put_meta", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.prepare()
        self.assertEqual(self.volume.writes, 0)

    def test_renamed_files_are_restored_by_reference(self):
        self.prepare()
        self.volume.files["/renamed.mkv"] = self.volume.files.pop("/Movies/film.mkv")
        result = self.restore()
        self.assertEqual(result["state"], "restored")
        self.assertEqual(signature(self.volume.get("/renamed.mkv")), self.original["/Movies/film.mkv"])

    def test_conflicts_missing_new_files_and_reused_records_are_preserved(self):
        self.prepare()
        changed = descriptor([ace(mask=1)])
        self.volume.files["/Movies"][2] = changed
        self.volume.files["/Movies/film.mkv"][0] += 1 << 48
        self.volume.files["/new.mkv"] = [99, False, descriptor([ace()])]
        result = self.restore()
        self.assertEqual(result["conflicts"], ["/Movies"])
        self.assertEqual(result["missing"], ["/Movies/film.mkv"])
        self.assertEqual(result["state"], "restore-incomplete")
        self.assertEqual(self.volume.get("/Movies"), changed)
        self.assertEqual(self.volume.get("/new.mkv"), descriptor([ace()]))

    def test_different_volume_and_existing_journal_refused(self):
        self.prepare()
        with self.assertRaises(FileExistsError):
            self.prepare()
        Path(self.target).write_bytes(make_boot(b"87654321"))
        count = self.volume.writes
        with self.assertRaisesRegex(DriveError, "different NTFS"):
            self.restore()
        self.assertEqual(self.volume.writes, count)

    def test_journal_cannot_be_the_image_or_an_alias(self):
        with self.assertRaises(DriveError):
            prepare(self.target, self.target, volume_factory=self.volume)
        Path(self.backup).symlink_to(self.target)
        with self.assertRaises(DriveError):
            self.prepare()

    def test_rollback_ignores_stale_applied_flags(self):
        self.prepare()
        with sqlite3.connect(self.backup) as db:
            db.execute("UPDATE entries SET applied=0")
        self.assertEqual(self.restore()["restored"], 3)

    def test_stale_inspection_refuses_preparation_before_journal_or_acl_writes(self):
        with self.assertRaisesRegex(DriveError, "no longer matches"):
            prepare(self.target, self.backup, volume_factory=self.volume, expected_boot="wrong-volume")
        self.assertFalse(Path(self.backup).exists())
        self.assertEqual(self.volume.writes, 0)


if __name__ == "__main__":
    unittest.main()
