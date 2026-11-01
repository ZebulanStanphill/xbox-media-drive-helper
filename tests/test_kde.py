import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xbox_media_drive import kde


class ActionTextTests(unittest.TestCase):
    def test_action_file_content(self):
        text = kde.action_text("/home/me/Xbox.AppImage", "/home/me/.local/share/xbox-media-drive-helper/icon.svg")
        self.assertEqual(text, """[Desktop Entry]
Type=Service
X-KDE-Solid-Predicate=[ IS_NOT OpticalDisc AND [ StorageVolume.ignored == false AND [ StorageVolume.usage == 'FileSystem' AND StorageVolume.fsType == 'ntfs' ] ] ]
Actions=open;
# Without this, Plasma mounts the volume before launching the action. The app needs it unmounted.
X-KDE-SkipMount=true
X-XboxMediaDrive-Managed=true

[Desktop Action open]
Exec=/home/me/Xbox.AppImage "--device=%d"
Icon=/home/me/.local/share/xbox-media-drive-helper/icon.svg
Name=Open in Xbox Media Drive Helper
""")

    def test_predicate_terms_are_binary(self):
        # Solid predicates take exactly two terms per bracket.
        self.assertEqual(kde.PREDICATE.count("["), kde.PREDICATE.count("]"))
        self.assertEqual(kde.PREDICATE.count(" AND "), kde.PREDICATE.count("["))
        self.assertEqual(kde.PREDICATE.count("["), 3)

    def test_source_install_file_matches_the_predicate_and_skips_mounting(self):
        path = Path(__file__).resolve().parent.parent / "packaging/linux/xbox-media-drive-helper-solid.desktop"
        lines = path.read_text().splitlines()
        self.assertIn("X-KDE-Solid-Predicate=" + kde.PREDICATE, lines)
        self.assertIn("X-KDE-SkipMount=true", lines)
        self.assertIn('Exec=xbox-media-drive-gui "--device=%d"', lines)
        self.assertNotIn(kde.MARKER, lines)

    def test_exec_line_quotes_the_path_for_a_shell_and_escapes_for_kconfig(self):
        for path, expected in (
                ("/home/me/Xbox.AppImage", '/home/me/Xbox.AppImage "--device=%d"'),
                ("/home/me/My Apps/Xbox.AppImage", "'/home/me/My Apps/Xbox.AppImage' \"--device=%d\""),
                ("/home/it's/x.AppImage", "'/home/it'\"'\"'s/x.AppImage' \"--device=%d\""),
                ("/home/100%/x.AppImage", '/home/100%%/x.AppImage "--device=%d"'),
                ("/home/a\\b/x.AppImage", "'/home/a\\\\b/x.AppImage' \"--device=%d\"")):
            with self.subTest(path=path):
                self.assertEqual(kde.exec_line(path), expected)

    def test_control_characters_are_refused(self):
        for path in ("/home/a\nb/x.AppImage", "/home/a\tb/x", "/home/a\x00b", "/home/a\x7fb"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    kde.exec_line(path)
        with self.assertRaises(ValueError):
            kde.action_text("/x.AppImage", "/home/a\nb/icon.svg")

    def test_icon_path_backslashes_are_escaped(self):
        self.assertIn("\nIcon=/a\\\\b/icon.svg\n", kde.action_text("/x.AppImage", "/a\\b/icon.svg"))


class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.environ = {"XDG_DATA_HOME": str(self.home / "data"), "HOME": str(self.home / "home")}
        self.action = self.home / "data/solid/actions/xbox-media-drive-helper-appimage.desktop"
        self.icon = self.home / "data/xbox-media-drive-helper/icon.svg"

    def test_paths_follow_xdg_data_home_or_home(self):
        self.assertEqual(kde.data_home(self.environ), self.home / "data")
        self.assertEqual(kde.action_path(self.environ), self.action)
        self.assertEqual(kde.icon_path(self.environ), self.icon)
        for environ in ({"XDG_DATA_HOME": "relative/data", "HOME": "/h"}, {"XDG_DATA_HOME": "", "HOME": "/h"},
                        {"HOME": "/h"}):
            with self.subTest(environ=environ):
                self.assertEqual(kde.data_home(environ), Path("/h/.local/share"))

    def test_install_writes_action_and_icon(self):
        self.assertFalse(kde.installed(self.environ))
        self.assertEqual(kde.install("/apps/Xbox.AppImage", self.environ), self.action)
        self.assertTrue(kde.installed(self.environ))
        self.assertEqual(self.action.read_text(), kde.action_text("/apps/Xbox.AppImage", self.icon))
        self.assertEqual(self.icon.read_bytes(), Path(kde.__file__).with_name("icon.svg").read_bytes())
        for path in (self.action, self.icon):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual([p.name for p in self.action.parent.iterdir()], [self.action.name])

    def test_install_refuses_a_bad_path_before_writing_anything(self):
        with self.assertRaises(ValueError):
            kde.install("/apps/a\nb.AppImage", self.environ)
        self.assertFalse((self.home / "data").exists())

    def test_failed_write_leaves_no_temporary_file(self):
        kde.install("/apps/Old.AppImage", self.environ)
        with patch("xbox_media_drive.kde.os.replace", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                kde.install("/apps/New.AppImage", self.environ)
        self.assertEqual([p.name for p in self.action.parent.iterdir()], [self.action.name])
        self.assertIn("Old.AppImage", self.action.read_text())

    def test_installed_requires_the_marker(self):
        self.action.parent.mkdir(parents=True)
        self.action.write_text("[Desktop Entry]\nType=Service\n")
        self.assertFalse(kde.installed(self.environ))
        self.action.write_text("[Desktop Entry]\nX-XboxMediaDrive-Managed=true\n")
        self.assertTrue(kde.installed(self.environ))
        self.action.write_bytes(b"\xff\xfe")
        self.assertFalse(kde.installed(self.environ))

    def test_remove_deletes_only_what_install_wrote(self):
        kde.install("/apps/Xbox.AppImage", self.environ)
        unrelated = self.action.parent / "partitionmanager.desktop"
        unrelated.write_text("keep")
        self.assertTrue(kde.remove(self.environ))
        self.assertFalse(self.action.exists())
        self.assertFalse(self.icon.parent.exists())
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertFalse(kde.remove(self.environ))

    def test_remove_keeps_a_non_empty_icon_directory(self):
        kde.install("/apps/Xbox.AppImage", self.environ)
        other = self.icon.parent / "other"
        other.write_text("keep")
        self.assertTrue(kde.remove(self.environ))
        self.assertFalse(self.icon.exists())
        self.assertEqual(other.read_text(), "keep")

    def test_remove_leaves_an_unmarked_file_alone(self):
        self.action.parent.mkdir(parents=True)
        self.action.write_text("[Desktop Entry]\nType=Service\n")
        self.icon.parent.mkdir(parents=True)
        self.icon.write_text("icon")
        self.assertFalse(kde.remove(self.environ))
        self.assertTrue(self.action.exists())
        self.assertTrue(self.icon.exists())

    def test_refresh_rewrites_only_a_stale_installation(self):
        self.assertFalse(kde.refresh("/apps/Xbox.AppImage", self.environ))
        self.assertFalse(self.action.exists())
        kde.install("/apps/Xbox.AppImage", self.environ)
        self.assertFalse(kde.refresh("/apps/Xbox.AppImage", self.environ))
        self.assertTrue(kde.refresh("/moved/Xbox-0.14.0.AppImage", self.environ))
        self.assertEqual(self.action.read_text(), kde.action_text("/moved/Xbox-0.14.0.AppImage", self.icon))
        self.icon.unlink()
        self.assertTrue(kde.refresh("/moved/Xbox-0.14.0.AppImage", self.environ))
        self.assertTrue(self.icon.is_file())

    def test_refresh_does_not_touch_an_unmarked_file(self):
        self.action.parent.mkdir(parents=True)
        self.action.write_text("other")
        self.assertFalse(kde.refresh("/apps/Xbox.AppImage", self.environ))
        self.assertEqual(self.action.read_text(), "other")


class AppImagePathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.image = Path(temp.name).resolve() / "Xbox.AppImage"
        self.image.write_text("")

    def appimage(self, environ, platform="linux", frozen=True):
        with patch.object(sys, "platform", platform), patch.object(sys, "frozen", frozen, create=True):
            return kde.appimage_path(environ)

    def test_returns_the_file_only_inside_a_frozen_linux_app(self):
        environ = {"APPIMAGE": str(self.image)}
        self.assertEqual(self.appimage(environ), str(self.image))
        self.assertIsNone(self.appimage(environ, platform="darwin"))
        self.assertIsNone(self.appimage(environ, frozen=False))

    def test_refuses_missing_relative_or_unset_values(self):
        for value in (str(self.image.parent), str(self.image.parent / "missing.AppImage"), "Xbox.AppImage", ""):
            with self.subTest(value=value):
                self.assertIsNone(self.appimage({"APPIMAGE": value}))
        self.assertIsNone(self.appimage({}))


if __name__ == "__main__":
    unittest.main()
