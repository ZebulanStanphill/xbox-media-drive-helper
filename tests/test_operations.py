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
from xbox_media_drive.operations import inspect, journal, journal_info, prepare, put_meta, restore
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

    def prepare(self, **kwargs):
        return prepare(self.target, self.backup, volume_factory=self.volume, **kwargs)

    def restore(self):
        return restore(self.target, self.backup, volume_factory=self.volume)

    def test_read_only_inspection_and_round_trip(self):
        info = inspect(self.target, volume_factory=self.volume)
        self.assertEqual(info["changes_needed"], 3)
        self.assertFalse(info["previously_prepared"])
        self.assertEqual(self.volume.writes, 0)
        result = self.prepare()
        self.assertEqual(result["changed"], 3)
        self.assertFalse(result["appended"])
        self.assertTrue(inspect(self.target, volume_factory=self.volume)["previously_prepared"])
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

    def test_null_or_absent_root_dacl_is_not_previously_prepared(self):
        for control in (0x8000, 0x8004):
            with self.subTest(control=control):
                self.volume.files["/"][2] = struct.pack("<BBHIIII", 1, 0, control, 0, 0, 0, 0)
                self.assertFalse(inspect(self.target, volume_factory=self.volume)["previously_prepared"])

    def test_previously_prepared_uses_requested_access(self):
        self.prepare()
        self.assertFalse(inspect(self.target, access="full", volume_factory=self.volume)["previously_prepared"])

    def test_append_only_new_objects_and_one_journal_restores_everything(self):
        self.prepare()
        metadata = journal_info(self.backup)
        later = descriptor([ace(mask=1)])
        self.volume.files["/Movies/later.mkv"] = [72, False, later]
        info = inspect(self.target, volume_factory=self.volume)
        self.assertTrue(info["previously_prepared"])
        self.assertEqual(info["changes_needed"], 1)
        result = self.prepare(append=True)
        self.assertTrue(result["appended"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.volume.writes, 4)
        appended = journal_info(self.backup)
        for key in ("created", "access", "format", "identity"):
            self.assertEqual(appended[key], metadata[key])
        self.assertIn("updated", appended)
        self.assertEqual(appended["entries"], 4)
        self.assertEqual(inspect(self.target, volume_factory=self.volume)["changes_needed"], 0)
        self.assertEqual(self.prepare(append=True)["changed"], 0)
        self.assertEqual(self.restore()["restored"], 4)
        for path, original in self.original.items():
            self.assertEqual(signature(self.volume.get(path)), original)
        self.assertEqual(signature(self.volume.get("/Movies/later.mkv")), signature(later))

    def test_append_rejects_wrong_volume_before_opening(self):
        self.prepare()
        Path(self.target).write_bytes(make_boot(b"87654321"))
        with patch("xbox_media_drive.operations.Volume") as factory:
            with self.assertRaisesRegex(DriveError, "different NTFS volume"):
                prepare(self.target, self.backup, append=True, volume_factory=factory)
            factory.assert_not_called()
        self.assertEqual(self.volume.writes, 3)

    def test_append_rejects_used_journals_and_invalid_metadata(self):
        self.prepare()
        # A restore that could not open the drive touched nothing, so appending stays possible.
        with patch("xbox_media_drive.operations.Volume", side_effect=DriveError("busy")) as factory:
            with self.assertRaisesRegex(DriveError, "busy"):
                restore(self.target, self.backup, volume_factory=factory)
        self.assertEqual(journal_info(self.backup)["state"], "restore-interrupted")
        self.assertEqual(self.prepare(append=True)["changed"], 0)
        self.assertEqual(journal_info(self.backup)["state"], "prepared")
        # Once any object has been restored the journal is spent.
        self.restore()
        with self.assertRaisesRegex(DriveError, "already used to restore"):
            self.prepare(append=True)
        with journal(self.backup) as db:
            put_meta(db, "format", 2)
        with self.assertRaisesRegex(DriveError, "Unsupported recovery journal"):
            self.prepare(append=True)
        self.assertEqual(self.volume.writes, 6)

    def test_append_refused_before_writing_keeps_the_journal_prepared(self):
        self.prepare()
        self.volume.files["/Movies/film.mkv"][2] = descriptor([ace(mask=1)])
        with self.assertRaisesRegex(DriveError, "changed since this journal recorded it"):
            self.prepare(append=True)
        self.assertEqual(journal_info(self.backup)["state"], "prepared")
        self.assertEqual(self.volume.writes, 3)

    def test_append_requires_existing_journal(self):
        with self.assertRaisesRegex(DriveError, "existing recovery journal"):
            self.prepare(append=True)
        self.assertFalse(Path(self.backup).exists())

    def test_append_journals_all_new_objects_durably_before_first_write(self):
        self.prepare()
        for ref in (72, 73):
            self.volume.files["/{}.mkv".format(ref)] = [ref, False, descriptor([ace()])]
        write = self.volume.set

        def check_journal_then_write(path, raw):
            with sqlite3.connect(self.backup) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 5)
                row = db.execute("SELECT original,prepared FROM entries WHERE path=?", (path,)).fetchone()
            self.assertEqual(signature(row[0]), signature(self.volume.get(path)))
            self.assertEqual(signature(row[1]), signature(raw))
            write(path, raw)

        with patch.object(self.volume, "set", side_effect=check_journal_then_write):
            self.assertEqual(self.prepare(append=True)["changed"], 2)

    def test_append_rechecks_current_permissions_before_writing(self):
        self.prepare()
        self.volume.files["/new.mkv"] = [72, False, descriptor([ace()])]
        walk = self.volume.walk

        def changed_after_walk():
            yield from walk()
            self.volume.files["/new.mkv"][2] = descriptor([ace(mask=1)])

        with patch.object(self.volume, "walk", side_effect=changed_after_walk):
            with self.assertRaisesRegex(DriveError, "Permissions changed during preflight: /new.mkv"):
                self.prepare(append=True)
        self.assertEqual(self.volume.writes, 3)

    def test_append_rejects_changed_recorded_object_before_any_writes(self):
        self.prepare()
        self.volume.files["/new.mkv"] = [72, False, descriptor([ace()])]
        self.volume.files["/Movies/film.mkv"] = self.volume.files.pop("/Movies/film.mkv")
        self.volume.files["/Movies/film.mkv"][2] = descriptor([ace(mask=1)])
        with self.assertRaisesRegex(DriveError, "permissions changed since this journal recorded it"):
            self.prepare(append=True)
        self.assertEqual(self.volume.writes, 3)
        self.assertEqual(signature(self.volume.get("/new.mkv")), signature(descriptor([ace()])))

    def test_interrupted_append_resumes_unmarked_write_and_unwritten_rows(self):
        self.prepare()
        for ref in (72, 73):
            self.volume.files["/{}.mkv".format(ref)] = [ref, False, descriptor([ace()])]
        self.volume.fail_at = 4
        with self.assertRaisesRegex(DriveError, "interruption"):
            self.prepare(append=True)
        self.assertEqual(journal_info(self.backup)["state"], "interrupted")
        with sqlite3.connect(self.backup) as db:
            self.assertEqual(db.execute("SELECT SUM(applied) FROM entries").fetchone()[0], 3)
        progress = []
        result = self.prepare(append=True, progress=progress.append)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.volume.writes, 5)
        self.assertEqual([p["completed"] for p in progress], [1, 2])
        with sqlite3.connect(self.backup) as db:
            self.assertEqual(db.execute("SELECT SUM(applied) FROM entries").fetchone()[0], 5)
        self.assertEqual(self.restore()["restored"], 5)

    def test_interrupted_append_resumes_renamed_rows_and_skips_deleted_ones(self):
        self.prepare()
        for ref in (72, 73):
            self.volume.files["/{}.mkv".format(ref)] = [ref, False, descriptor([ace()])]
        self.volume.fail_at = 4
        with self.assertRaisesRegex(DriveError, "interruption"):
            self.prepare(append=True)
        # The interrupted run wrote /72.mkv; /73.mkv is still pending. Both change before the retry.
        del self.volume.files["/72.mkv"]
        self.volume.files["/Movies/renamed.mkv"] = self.volume.files.pop("/73.mkv")
        self.assertEqual(self.prepare(append=True)["changed"], 1)
        self.assertEqual(inspect(self.target, volume_factory=self.volume)["changes_needed"], 0)
        result = self.restore()
        self.assertEqual(result["missing"], ["/72.mkv"])
        self.assertEqual(signature(self.volume.get("/Movies/renamed.mkv")), signature(descriptor([ace()])))

    def test_append_pending_row_rejects_different_access(self):
        with patch.object(self.volume, "set", side_effect=DriveError("interrupted before write")):
            with self.assertRaises(DriveError):
                self.prepare()
        with self.assertRaisesRegex(DriveError, "different access level"):
            self.prepare(append=True, access="full")
        self.assertEqual(self.volume.writes, 0)

    def test_append_marks_legacy_unmarked_write_without_rewriting(self):
        self.prepare()
        with journal(self.backup) as db:
            row = db.execute("SELECT * FROM entries WHERE path='/'").fetchone()
            raw = row["prepared"]
            control = struct.unpack_from("<H", raw, 2)[0]
            prepared = raw[:2] + struct.pack("<H", control | 0x0400) + raw[4:]
            db.execute("UPDATE entries SET prepared=?, applied=0 WHERE path='/'", (prepared,))
            db.commit()
        self.assertEqual(self.prepare(append=True)["changed"], 0)
        self.assertEqual(self.volume.writes, 3)
        with sqlite3.connect(self.backup) as db:
            self.assertEqual(db.execute("SELECT applied FROM entries WHERE path='/'").fetchone()[0], 1)

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
