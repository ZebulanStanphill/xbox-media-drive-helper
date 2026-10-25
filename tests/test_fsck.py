import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xbox_media_drive import DriveError
from xbox_media_drive.fsck import SYSTEM_DIRECTORIES, bundled_ntfsck, check_filesystem, find_ntfsck, is_bundled


class FindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Real files, with the expected owner standing in for root, which the tests can't create files as.
        self.owner = os.getuid()
        self.directory = self.make_directory("sbin")
        self.program = self.make_program(self.directory)
        patcher = patch("sys.platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_directory(self, name, mode=0o755):
        directory = Path(self.temp.name) / name
        directory.mkdir()
        directory.chmod(mode)
        return directory

    def make_program(self, directory, mode=0o755):
        program = directory / "ntfsck"
        program.write_bytes(b"#!/bin/sh\n")
        program.chmod(mode)
        return program

    def find(self, *directories, owner=None):
        return find_ntfsck(tuple(str(directory) for directory in directories or [self.directory]),
                           self.owner if owner is None else owner)

    def test_searches_only_fixed_system_directories(self):
        self.assertEqual(SYSTEM_DIRECTORIES, ("/usr/sbin", "/usr/bin", "/sbin", "/bin"))

    def test_accepts_a_safe_binary_and_returns_its_real_path(self):
        self.assertEqual(self.find(), os.path.realpath(self.program))

    def test_uses_the_first_acceptable_directory(self):
        empty = self.make_directory("empty")
        other = self.make_directory("bin")
        self.make_program(other)
        self.assertEqual(self.find(empty, self.directory, other), os.path.realpath(self.program))
        self.program.chmod(0o644)
        self.assertEqual(self.find(empty, self.directory, other), os.path.realpath(other / "ntfsck"))

    def test_owner_must_be_root_by_default(self):
        self.assertEqual(find_ntfsck((str(self.directory),)) is not None, os.getuid() == 0)

    def test_rejects_a_binary_owned_by_someone_else(self):
        self.assertIsNone(self.find(owner=self.owner + 1))

    def test_rejects_group_or_world_writable_binaries(self):
        for mode in (0o775, 0o757, 0o777, 0o666, 0o722):
            with self.subTest(mode=oct(mode)):
                self.program.chmod(mode)
                self.assertIsNone(self.find())

    def test_rejects_missing_non_executable_and_non_regular_files(self):
        self.assertIsNone(self.find(self.make_directory("empty")))
        for mode in (0o644, 0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                self.program.chmod(mode)
                self.assertIsNone(self.find())
        self.program.unlink()
        self.program.mkdir()
        self.assertIsNone(self.find())
        self.program.rmdir()
        self.program.symlink_to("/dev/null")
        self.assertIsNone(self.find())

    def test_rejects_unsafe_parent_directories(self):
        for mode in (0o775, 0o757, 0o777):
            with self.subTest(mode=oct(mode)):
                self.directory.chmod(mode)
                self.assertIsNone(self.find())

    def test_rejects_a_parent_directory_owned_by_someone_else(self):
        real_stat = os.stat
        folder = os.path.realpath(self.directory)

        def stat_result(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if os.fspath(path) == folder:
                return SimpleNamespace(st_uid=info.st_uid + 1, st_mode=info.st_mode)
            return info

        with patch("xbox_media_drive.fsck.os.stat", side_effect=stat_result):
            self.assertIsNone(self.find())

    def test_follows_symlinks_and_checks_the_file_they_reach(self):
        link_directory = self.make_directory("link")
        (link_directory / "ntfsck").symlink_to(self.program)
        self.assertEqual(self.find(link_directory), os.path.realpath(self.program))
        unsafe = self.make_directory("unsafe", 0o777)
        target = self.make_program(unsafe)
        (link_directory / "ntfsck").unlink()
        (link_directory / "ntfsck").symlink_to(target)
        self.assertIsNone(self.find(link_directory))
        target.chmod(0o777)
        unsafe.chmod(0o755)
        self.assertIsNone(self.find(link_directory))

    def test_never_searches_path(self):
        elsewhere = self.make_directory("elsewhere")
        program = self.make_program(elsewhere)
        with patch.dict(os.environ, {"PATH": str(elsewhere) + os.pathsep + str(self.directory)}):
            self.assertIsNone(self.find(self.make_directory("empty")))
            self.assertNotEqual(find_ntfsck(owner=self.owner), os.path.realpath(program))
            self.assertNotEqual(find_ntfsck(owner=self.owner), os.path.realpath(self.program))

    def test_only_linux_has_a_checker(self):
        for platform in ("darwin", "win32", "freebsd14"):
            with self.subTest(platform=platform), patch("sys.platform", platform):
                self.assertIsNone(self.find())


class BundledTests(unittest.TestCase):
    """The ntfsck inside an AppImage: only on a read-only mount, and only after a system copy."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.owner = os.getuid()
        self.system = Path(self.temp.name) / "sbin"
        self.system.mkdir()
        self.bundled = Path(self.temp.name) / "ntfsck"
        self.bundled.write_bytes(b"#!/bin/sh\n")
        self.bundled.chmod(0o755)
        for patcher in (patch("sys.platform", "linux"), patch.object(sys, "frozen", True, create=True),
                        patch.dict(os.environ, {"APPIMAGE": "/home/me/Xbox.AppImage"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def statvfs(self, read_only=True):
        return lambda path: SimpleNamespace(f_flag=os.ST_RDONLY if read_only else 0)

    def find(self, read_only=True, bundled=None):
        return find_ntfsck((str(self.system),), self.owner, bundled=str(bundled or self.bundled),
                           statvfs=self.statvfs(read_only))

    def test_is_used_when_no_system_copy_exists(self):
        self.assertEqual(self.find(), os.path.realpath(self.bundled))

    def test_system_copy_is_preferred(self):
        program = self.system / "ntfsck"
        program.write_bytes(b"#!/bin/sh\n")
        program.chmod(0o755)
        self.assertEqual(self.find(), os.path.realpath(program))
        self.assertFalse(is_bundled(self.find(), str(self.bundled), self.statvfs()))

    def test_unsafe_system_copy_does_not_block_the_bundled_one(self):
        program = self.system / "ntfsck"
        program.write_bytes(b"#!/bin/sh\n")
        program.chmod(0o777)
        self.assertEqual(self.find(), os.path.realpath(self.bundled))

    def test_is_reported_as_bundled(self):
        real = os.path.realpath(self.bundled)
        self.assertTrue(is_bundled(real, str(self.bundled), self.statvfs()))
        self.assertFalse(is_bundled(real, str(self.bundled), self.statvfs(read_only=False)))
        self.assertFalse(is_bundled("/usr/sbin/ntfsck", str(self.bundled), self.statvfs()))
        self.assertFalse(is_bundled(None, str(self.bundled), self.statvfs()))

    def test_ownership_is_not_required(self):
        with patch("xbox_media_drive.fsck.os.stat", side_effect=lambda path: SimpleNamespace(
                st_uid=self.owner + 1, st_mode=stat.S_IFREG | 0o755)):
            self.assertEqual(bundled_ntfsck(str(self.bundled), self.statvfs()), os.path.realpath(self.bundled))

    def test_rejected_on_a_writable_filesystem(self):
        self.assertIsNone(self.find(read_only=False))

    def test_rejected_when_group_or_world_writable(self):
        for mode in (0o775, 0o757, 0o777, 0o722):
            with self.subTest(mode=oct(mode)):
                self.bundled.chmod(mode)
                self.assertIsNone(self.find())

    def test_rejected_when_not_an_executable_regular_file(self):
        self.bundled.chmod(0o644)
        self.assertIsNone(self.find())
        self.bundled.unlink()
        self.assertIsNone(self.find())
        self.bundled.mkdir()
        self.assertIsNone(self.find())

    def test_rejected_when_not_frozen(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(self.find())

    def test_rejected_when_not_run_from_an_appimage(self):
        for value in (None, ""):
            with self.subTest(value=value):
                environment = dict(os.environ)
                environment.pop("APPIMAGE")
                if value is not None:
                    environment["APPIMAGE"] = value
                with patch.dict(os.environ, environment, clear=True):
                    self.assertIsNone(self.find())

    def test_rejected_when_not_linux(self):
        for platform in ("darwin", "win32", "freebsd14"):
            with self.subTest(platform=platform), patch("sys.platform", platform):
                self.assertIsNone(self.find())
                self.assertIsNone(bundled_ntfsck(str(self.bundled), self.statvfs()))

    def test_default_location_is_inside_the_bundle_directory(self):
        folder = Path(self.temp.name) / "bundle"
        (folder / "ntfsprogs-plus").mkdir(parents=True)
        program = folder / "ntfsprogs-plus/ntfsck"
        program.write_bytes(b"#!/bin/sh\n")
        program.chmod(0o755)
        with patch.object(sys, "_MEIPASS", str(folder), create=True):
            self.assertEqual(bundled_ntfsck(statvfs=self.statvfs()), os.path.realpath(program))
            self.assertIsNone(bundled_ntfsck(statvfs=self.statvfs(read_only=False)))

    def test_missing_bundle_directory_is_not_an_error(self):
        with patch.object(sys, "_MEIPASS", self.temp.name + "/nowhere", create=True):
            self.assertIsNone(bundled_ntfsck(statvfs=self.statvfs()))


class CheckTests(unittest.TestCase):
    CHECKER = "/usr/sbin/ntfsck"

    def setUp(self):
        patcher = patch("xbox_media_drive.fsck.validate_target", return_value="/dev/sdb1")
        self.validate = patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, code=0, output="", target="/dev/sdb1"):
        run = Mock(return_value=SimpleNamespace(returncode=code, stdout=output))
        result = check_filesystem(target, find=lambda: self.CHECKER, run=run)
        return result, run

    def fails(self, code, output=""):
        run = Mock(return_value=SimpleNamespace(returncode=code, stdout=output))
        with self.assertRaises(DriveError) as raised:
            check_filesystem("/dev/sdb1", find=lambda: self.CHECKER, run=run)
        return str(raised.exception)

    def test_runs_only_the_read_only_quiet_mode_without_a_shell_or_caller_environment(self):
        _, run = self.check()
        run.assert_called_once()
        arguments, options = run.call_args
        self.assertEqual(arguments, ([self.CHECKER, "-n", "-q", "/dev/sdb1"],))
        self.assertEqual(options, {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                                   "text": True, "errors": "replace",
                                   "env": {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}})

    def test_validates_the_target_before_looking_for_or_running_ntfsck(self):
        calls = []
        self.validate.side_effect = lambda target: calls.append(("validate", target)) or "/dev/sdb1"
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
        check_filesystem("/dev/sdb", find=lambda: calls.append(("find",)) or self.CHECKER, run=run)
        self.assertEqual(calls, [("validate", "/dev/sdb"), ("find",)])
        self.assertEqual(run.call_args.args[0][-1], "/dev/sdb1")
        self.validate.side_effect = DriveError("Unmount the selected volume first")
        run.reset_mock()
        with self.assertRaisesRegex(DriveError, "Unmount"):
            check_filesystem("/dev/sdb", find=lambda: self.fail("must validate first"), run=run)
        run.assert_not_called()

    def test_exit_zero_is_clean_and_four_is_problems(self):
        for code, verdict in ((0, "clean"), (4, "problems")):
            with self.subTest(code=code):
                result, _ = self.check(code, "some output\n")
                self.assertEqual(result, {"target": "/dev/sdb1", "checker": self.CHECKER, "bundled": False,
                                          "exit_code": code, "verdict": verdict, "output": "some output"})

    def test_result_says_when_the_bundled_ntfsck_ran(self):
        with patch("xbox_media_drive.fsck.is_bundled", return_value=True):
            self.assertTrue(self.check()[0]["bundled"])

    def test_any_other_exit_is_an_error_with_the_output_tail(self):
        for code in (1, 2, 8, 16, 32, 64, 128, -9):
            with self.subTest(code=code):
                message = self.fails(code, "ntfsck: something broke\n")
                self.assertIn("ntfsck couldn't check the volume (exit {})".format(code), message)
                self.assertIn("ntfsck: something broke", message)
        self.assertEqual(self.fails(1), "ntfsck couldn't check the volume (exit 1).")

    def test_error_shows_only_the_last_lines(self):
        message = self.fails(1, "\n".join("line {}".format(number) for number in range(100)))
        self.assertIn("line 99", message)
        self.assertIn("line 80", message)
        self.assertNotIn("line 79", message)

    def test_output_is_limited_to_the_last_200_lines_and_20000_characters(self):
        result, _ = self.check(4, "\n".join("line {}".format(number) for number in range(500)))
        lines = result["output"].splitlines()
        self.assertEqual((len(lines), lines[0], lines[-1]), (200, "line 300", "line 499"))
        result, _ = self.check(4, "a" * 30000 + "end")
        self.assertEqual((len(result["output"]), result["output"][-3:]), (20000, "end"))
        self.assertEqual(self.check(0, None)[0]["output"], "")

    def test_missing_ntfsck_explains_how_to_get_it(self):
        run = Mock()
        with self.assertRaises(DriveError) as raised:
            check_filesystem("/dev/sdb1", find=lambda: None, run=run)
        message = str(raised.exception)
        for text in ("only on Linux", "sudo apt install ntfsprogs-plus", "chkdsk"):
            self.assertIn(text, message)
        run.assert_not_called()

    def test_failing_to_start_ntfsck_is_a_drive_error(self):
        run = Mock(side_effect=PermissionError(13, "Permission denied"))
        with self.assertRaisesRegex(DriveError, "Could not run ntfsck: .*Permission denied"):
            check_filesystem("/dev/sdb1", find=lambda: self.CHECKER, run=run)

    def test_runs_a_real_process_with_merged_output_closed_stdin_and_clean_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "ntfsck"
            script.write_text('#!/bin/sh\n'
                              'echo "args: $*"\n'
                              'echo "from stderr" >&2\n'
                              'echo "env: HOME=[$HOME] LC_ALL=[$LC_ALL] PATH=[$PATH]"\n'
                              'if read line; then echo "read input"; else echo "no input"; fi\n'
                              'exit 4\n')
            script.chmod(0o755)
            result = check_filesystem("/dev/sdb1", find=lambda: str(script))
        self.assertEqual(result["verdict"], "problems")
        self.assertEqual(result["output"].splitlines(),
                         ["args: -n -q /dev/sdb1", "from stderr",
                          "env: HOME=[] LC_ALL=[C] PATH=[/usr/sbin:/usr/bin:/sbin:/bin]", "no input"])
