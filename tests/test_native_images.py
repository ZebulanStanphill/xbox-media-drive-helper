"""Opt-in real NTFS integration tests, run by scripts/test-native.py.

Only disposable images created under TemporaryDirectory are accessed. A test-only
initializer permits those images without UID 0; all other API calls are upstream.
"""
import ctypes as C
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from xbox_media_drive import DriveError
from xbox_media_drive.native import Volume, load_library
from xbox_media_drive.operations import inspect, prepare, restore
from xbox_media_drive.security import signature

PREFIX = os.environ.get("XMD_TEST_PREFIX")
HARNESS = os.environ.get("XMD_TEST_HARNESS")


class ImageVolume(Volume):
    def __init__(self, target, writable=False, library=None):
        self.lib = load_library(library)
        self.writable = writable
        self.shim = C.CDLL(HARNESS, use_errno=True)
        self.shim.xmd_image_initialize.argtypes = [C.c_char_p, C.c_ulong]
        self.shim.xmd_image_initialize.restype = C.c_void_p
        flags = 0x08000000 | (0 if writable else 0x04000001)
        self.handle = self.shim.xmd_image_initialize(os.fsencode(target), flags)
        if not self.handle:
            raise DriveError("Image session failed: " + os.strerror(C.get_errno()))


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

    def test_real_ntfs_permissions_round_trip_preserves_data_and_boot(self):
        before = Path(self.image).read_bytes()[:512]
        with ImageVolume(self.image, library=self.library) as volume:
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

    @unittest.skipIf(os.getuid() == 0, "Only applies to an unprivileged process")
    def test_production_initializer_requires_root_without_modifying_image(self):
        before = hashlib.sha256(Path(self.image).read_bytes()).digest()
        with self.assertRaisesRegex(DriveError, "requires root"):
            Volume(self.image, library=self.library)
        self.assertEqual(hashlib.sha256(Path(self.image).read_bytes()).digest(), before)


if __name__ == "__main__":
    unittest.main()
