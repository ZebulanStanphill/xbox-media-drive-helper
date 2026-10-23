"""Opt-in real NTFS integration tests, run by scripts/test-native.py.

Only disposable images created under TemporaryDirectory are accessed. A test-only
initializer permits those images without UID 0; all other API calls are upstream.
"""
import ctypes as C
import struct
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from xbox_media_drive import DriveError
from xbox_media_drive.native import Volume
from xbox_media_drive.operations import clear_dirty, inspect, prepare, restore
from xbox_media_drive.security import AUTHENTICATED_USERS, Descriptor, MODIFY, acl_entries, signature
from tests.test_security import ace, descriptor

PREFIX = os.environ.get("XMD_TEST_PREFIX")
HARNESS = os.environ.get("XMD_TEST_HARNESS")


class ImageVolume(Volume):
    def _initialize(self, target, flags):
        self.shim = C.CDLL(HARNESS, use_errno=True)
        self.shim.xmd_image_initialize.argtypes = [C.c_char_p, C.c_ulong]
        self.shim.xmd_image_initialize.restype = C.c_void_p
        return self.shim.xmd_image_initialize(os.fsencode(target), flags)


@unittest.skipUnless(PREFIX and HARNESS, "Run scripts/test-native.py with an NTFS-3G prefix")
class NativeImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.image = str(Path(self.temp.name) / "media.img")
        self.backup = str(Path(self.temp.name) / "journal.sqlite")
        libs = list((Path(PREFIX) / "lib").glob("libntfs-3g.dylib")) or list((Path(PREFIX) / "lib").glob("libntfs-3g.so"))
        self.library = str(libs[0])
        with open(self.image, "wb") as out:
            out.truncate(64 * 1024 * 1024)
        self.tool("mkntfs", "-F", "-Q", "-L", "Xbox Test", self.image)
        self.payload = b"Preserve all media bytes\0\xff" * 8192
        source = Path(self.temp.name) / "payload.bin"
        source.write_bytes(self.payload)
        self.tool("ntfscp", self.image, str(source), "/movie.mkv")
        self.tool("ntfscp", self.image, str(source), "/caf\u00e9-\u65e5\u672c.mkv")
        with ImageVolume(self.image, writable=True, library=self.library) as volume:
            volume.shim.xmd_fixture_directory.argtypes = [C.c_void_p, C.c_char_p]
            volume.shim.xmd_fixture_directory.restype = C.c_int
            volume.shim.xmd_fixture_link.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p]
            volume.shim.xmd_fixture_link.restype = C.c_int
            for name in (b"Media", b"System Volume Information"):
                self.assertEqual(volume.shim.xmd_fixture_directory(volume.handle, name), 0)
            self.assertEqual(volume.shim.xmd_fixture_link(volume.handle, b"/movie.mkv", b"alias.mkv"), 0)
        self.tool("ntfscp", self.image, str(source), "/Media/nested.mkv")

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, name, *args):
        path = Path(PREFIX) / "bin" / name
        if not path.exists():
            path = Path(PREFIX) / "sbin" / name
        return subprocess.run([str(path), *args], capture_output=True, check=True).stdout

    def inspect(self):
        return inspect(self.image, library=self.library, volume_factory=ImageVolume)

    def mark_dirty(self, volume):
        volume.shim.xmd_fixture_dirty.argtypes = [C.c_void_p]
        volume.shim.xmd_fixture_dirty.restype = C.c_int
        self.assertEqual(volume.shim.xmd_fixture_dirty(volume.handle), 0)

    def test_clear_dirty_persists_and_preserves_flags_acls_and_data(self):
        with ImageVolume(self.image, writable=True, library=self.library) as volume:
            originals = {entry.path: volume.get(entry.path) for entry in volume.walk()}
            self.mark_dirty(volume)
            # Include undocumented bits that upstream write_flags masks away.
            flags = volume._volume_flags() | 0x0180
            volume._volume_flags(write=flags)
        result = clear_dirty(self.image, library=self.library, volume_factory=ImageVolume)
        self.assertEqual(result, {"target": os.path.realpath(self.image), "dirty": False, "cleared": True})
        with ImageVolume(self.image, library=self.library) as volume:
            self.assertFalse(volume.dirty())
            self.assertEqual(volume._volume_flags(), flags & ~1)
            self.assertEqual({entry.path: volume.get(entry.path) for entry in volume.walk()}, originals)
        for path in ("/movie.mkv", "/Media/nested.mkv", "/alias.mkv", "/caf\u00e9-\u65e5\u672c.mkv"):
            self.assertEqual(self.tool("ntfscat", self.image, path), self.payload)
        self.assertFalse(Path(self.backup).exists())

    def test_clear_dirty_refuses_open_journal_without_changes(self):
        with ImageVolume(self.image, writable=True, library=self.library) as volume:
            originals = {entry.path: volume.get(entry.path) for entry in volume.walk()}
            self.mark_dirty(volume)
            volume.shim.xmd_fixture_open_journal.argtypes = [C.c_void_p]
            volume.shim.xmd_fixture_open_journal.restype = C.c_int
            self.assertEqual(volume.shim.xmd_fixture_open_journal(volume.handle), 0)
        before = hashlib.sha256(Path(self.image).read_bytes()).digest()
        with self.assertRaisesRegex(DriveError, "NTFS journal is still open"):
            clear_dirty(self.image, library=self.library, volume_factory=ImageVolume)
        self.assertEqual(hashlib.sha256(Path(self.image).read_bytes()).digest(), before)
        with ImageVolume(self.image, library=self.library) as volume:
            self.assertTrue(volume.dirty())
            self.assertEqual({entry.path: volume.get(entry.path) for entry in volume.walk()}, originals)

    def test_dirty_flag_warns_and_allows_preparation(self):
        with ImageVolume(self.image, library=self.library) as volume:
            self.assertFalse(volume.dirty())
        self.assertFalse(self.inspect()["dirty"])
        with ImageVolume(self.image, writable=True, library=self.library) as volume:
            volume.shim.xmd_fixture_dirty.argtypes = [C.c_void_p]
            volume.shim.xmd_fixture_dirty.restype = C.c_int
            self.assertEqual(volume.shim.xmd_fixture_dirty(volume.handle), 0)
        with ImageVolume(self.image, library=self.library) as volume:
            self.assertTrue(volume.dirty())
        info = self.inspect()
        self.assertTrue(info["dirty"])
        self.assertTrue(info["ready"])
        result = prepare(self.image, self.backup, library=self.library, volume_factory=ImageVolume)
        self.assertEqual(result["state"], "prepared")
        self.assertTrue(result["dirty"])
        self.assertTrue(restore(self.image, self.backup, library=self.library,
                                volume_factory=ImageVolume)["dirty"])

    def test_open_journal_refuses_writes_without_changing_image_or_acls(self):
        with ImageVolume(self.image, writable=True, library=self.library) as volume:
            originals = {entry.path: volume.get(entry.path) for entry in volume.walk()}
            volume.shim.xmd_fixture_open_journal.argtypes = [C.c_void_p]
            volume.shim.xmd_fixture_open_journal.restype = C.c_int
            self.assertEqual(volume.shim.xmd_fixture_open_journal(volume.handle), 0)
        before = hashlib.sha256(Path(self.image).read_bytes()).digest()
        with self.assertRaisesRegex(DriveError, "NTFS journal is still open"):
            ImageVolume(self.image, writable=True, library=self.library)
        self.assertEqual(hashlib.sha256(Path(self.image).read_bytes()).digest(), before)
        self.assertTrue(self.inspect()["ready"])
        with self.assertRaisesRegex(DriveError, "NTFS journal is still open"):
            prepare(self.image, self.backup, library=self.library, volume_factory=ImageVolume)
        self.assertEqual(hashlib.sha256(Path(self.image).read_bytes()).digest(), before)
        with ImageVolume(self.image, library=self.library) as volume:
            self.assertEqual({entry.path: volume.get(entry.path) for entry in volume.walk()}, originals)

    def test_real_ntfs_permissions_round_trip_preserves_data_and_boot(self):
        before = Path(self.image).read_bytes()[:512]
        with ImageVolume(self.image, writable=True, library=self.library) as volume:
            # Model content copied with no ordinary user grant.
            volume.set("/Media/nested.mkv", descriptor([ace()]))
            originals = {entry.path: signature(volume.get(entry.path)) for entry in volume.walk()}
        read_only_hash = hashlib.sha256(Path(self.image).read_bytes()).digest()
        info = self.inspect()
        self.assertEqual(hashlib.sha256(Path(self.image).read_bytes()).digest(), read_only_hash)
        self.assertTrue(info["ready"])
        # Root, Media folder, nested file, two root files; hard link is deduplicated.
        self.assertEqual(info["objects"], 5)
        self.assertIn("/System Volume Information", info["skipped_system_paths"])
        self.assertGreaterEqual(info["changes_needed"], 3)
        prepared = prepare(self.image, self.backup, library=self.library, volume_factory=ImageVolume)
        self.assertGreaterEqual(prepared["changed"], 3)
        self.assertEqual(self.inspect()["changes_needed"], 0)
        with ImageVolume(self.image, library=self.library) as volume:
            entries = acl_entries(Descriptor.parse(volume.get("/Media/nested.mkv")).dacl)
            self.assertIn(ace(AUTHENTICATED_USERS, MODIFY), entries)
        self.assertEqual(self.tool("ntfscat", self.image, "/movie.mkv"), self.payload)
        self.assertEqual(self.tool("ntfscat", self.image, "/Media/nested.mkv"), self.payload)
        self.assertEqual(self.tool("ntfscat", self.image, "/alias.mkv"), self.payload)
        self.assertEqual(self.tool("ntfscat", self.image, "/caf\u00e9-\u65e5\u672c.mkv"), self.payload)
        result = restore(self.image, self.backup, library=self.library, volume_factory=ImageVolume)
        self.assertEqual(result["state"], "restored")
        with ImageVolume(self.image, library=self.library) as volume:
            for path, value in originals.items():
                self.assertEqual(signature(volume.get(path)), value)
        self.assertEqual(Path(self.image).read_bytes()[:512], before)
        self.assertEqual(self.tool("ntfscat", self.image, "/movie.mkv"), self.payload)
        self.assertEqual(restore(self.image, self.backup, library=self.library,
                                 volume_factory=ImageVolume)["already_original"], prepared["changed"])

    def test_real_interruption_can_be_undone(self):
        with ImageVolume(self.image, library=self.library) as volume:
            before = {entry.path: signature(volume.get(entry.path)) for entry in volume.walk()}

        def interrupt(value):
            raise DriveError("deliberate test interruption")

        with self.assertRaisesRegex(DriveError, "deliberate"):
            prepare(self.image, self.backup, library=self.library, volume_factory=ImageVolume, progress=interrupt)
        self.assertEqual(restore(self.image, self.backup, library=self.library,
                                 volume_factory=ImageVolume)["state"], "restored")
        with ImageVolume(self.image, library=self.library) as volume:
            for path, value in before.items():
                self.assertEqual(signature(volume.get(path)), value)
        self.assertEqual(self.tool("ntfscat", self.image, "/movie.mkv"), self.payload)

    def test_append_grants_later_ntfs3g_file_and_single_journal_restores_all(self):
        with ImageVolume(self.image, library=self.library) as volume:
            originals = {entry.path: signature(volume.get(entry.path)) for entry in volume.walk()}
        prepare(self.image, self.backup, library=self.library, volume_factory=ImageVolume)
        source = Path(self.temp.name) / "later.bin"
        source.write_bytes(self.payload)
        self.tool("ntfscp", self.image, str(source), "/Media/added-later.mkv")
        with ImageVolume(self.image, library=self.library) as volume:
            originals["/Media/added-later.mkv"] = signature(volume.get("/Media/added-later.mkv"))
        info = self.inspect()
        self.assertTrue(info["previously_prepared"])
        self.assertEqual(info["changes_needed"], 1)
        result = prepare(self.image, self.backup, append=True, library=self.library, volume_factory=ImageVolume)
        self.assertTrue(result["appended"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.inspect()["changes_needed"], 0)
        self.assertEqual(restore(self.image, self.backup, library=self.library,
                                 volume_factory=ImageVolume)["state"], "restored")
        with ImageVolume(self.image, library=self.library) as volume:
            for entry in volume.walk():
                self.assertEqual(signature(volume.get(entry.path)), originals[entry.path], entry.path)
        self.assertEqual(self.tool("ntfscat", self.image, "/Media/added-later.mkv"), self.payload)

    def windows_like(self):
        """Give every object SE_DACL_AUTO_INHERITED, as Windows-formatted volumes have; mkntfs leaves it clear."""
        with ImageVolume(self.image, writable=True, library=self.library) as volume:
            for entry in list(volume.walk()):
                raw = volume.get(entry.path)
                control = struct.unpack_from("<H", raw, 2)[0] | 0x0400 | 0x0100  # AUTO_INHERITED + its request bit
                volume.lib.ntfs_set_file_security(volume.handle, entry.path.encode(), 4,
                                                  C.create_string_buffer(raw[:2] + struct.pack("<H", control) + raw[4:]))
        with ImageVolume(self.image, library=self.library) as volume:
            values = {entry.path: signature(volume.get(entry.path)) for entry in volume.walk()}
        self.assertTrue(all(value[0] & 0x0400 for value in values.values()))
        return values

    def test_windows_auto_inherited_descriptors_round_trip(self):
        before = self.windows_like()
        prepare(self.image, self.backup, library=self.library, volume_factory=ImageVolume)
        with ImageVolume(self.image, library=self.library) as volume:
            self.assertTrue(all(signature(volume.get(path))[0] & 0x0400 for path in before))
        self.assertEqual(restore(self.image, self.backup, library=self.library,
                                 volume_factory=ImageVolume)["state"], "restored")
        with ImageVolume(self.image, library=self.library) as volume:
            for path, value in before.items():
                self.assertEqual(signature(volume.get(path)), value)

    def test_restore_recovers_a_write_that_dropped_auto_inherited(self):
        # Versions before this fix wrote DACLs without the request bit, so NTFS-3G dropped
        # SE_DACL_AUTO_INHERITED and verification of the first object failed.
        before = self.windows_like()

        class LegacyVolume(ImageVolume):
            def set(self, path, raw):
                if not self.lib.ntfs_set_file_security(self.handle, path.encode(), 4, C.create_string_buffer(raw)):
                    raise DriveError("write failed")

        with self.assertRaisesRegex(DriveError, "verification failed for /"):
            prepare(self.image, self.backup, library=self.library, volume_factory=LegacyVolume)
        result = restore(self.image, self.backup, library=self.library, volume_factory=ImageVolume)
        self.assertEqual((result["state"], result["restored"], result["conflicts"]), ("restored", 1, []))
        with ImageVolume(self.image, library=self.library) as volume:
            for path, value in before.items():
                self.assertEqual(signature(volume.get(path)), value)

    @unittest.skipIf(os.getuid() == 0, "Only applies to an unprivileged process")
    def test_production_initializer_requires_root_without_modifying_image(self):
        before = hashlib.sha256(Path(self.image).read_bytes()).digest()
        with self.assertRaisesRegex(DriveError, "requires root"):
            Volume(self.image, library=self.library)
        self.assertEqual(hashlib.sha256(Path(self.image).read_bytes()).digest(), before)


if __name__ == "__main__":
    unittest.main()
