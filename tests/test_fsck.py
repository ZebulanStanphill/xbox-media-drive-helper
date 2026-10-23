import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xbox_media_drive import DriveError
from xbox_media_drive.fsck import SYSTEM_DIRECTORIES, check_filesystem, find_ntfsck


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
                self.assertEqual(result, {"target": "/dev/sdb1", "checker": self.CHECKER, "exit_code": code,
                                          "verdict": verdict, "output": "some output"})

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
