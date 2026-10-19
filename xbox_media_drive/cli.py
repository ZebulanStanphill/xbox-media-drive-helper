import argparse
import json
import os
import sqlite3
import sys

from . import DriveError, __version__
from .devices import discover, whole_disk, validate_whole_disk
from .layout import analyze, read_head, restore_sector, switch
from .native import load_library
from .operations import inspect, journal_info, prepare, restore


def parser():
    root = argparse.ArgumentParser(description="Prepare existing NTFS media for Xbox apps and restore original permissions. "
                                   "Includes reversible whole-disk Xbox game/media signature switching; no formatting or repartitioning.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--json", action="store_true", help="Machine-readable result on stdout; progress/errors on stderr")
    root.add_argument("--library", help="Absolute path to a trusted libntfs-3g shared library")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List external disks/partitions (no root required)")
    sub.add_parser("doctor", help="Check the platform and native library")
    sub.add_parser("gui", help="Open the desktop interface")
    info = sub.add_parser("journal", help="Inspect a recovery journal without opening the drive")
    info.add_argument("backup")
    mode = sub.add_parser("mode", help="Inspect or reversibly switch a whole disk's Xbox game/media mode")
    mode.add_argument("target", help="External whole disk")
    action = mode.add_mutually_exclusive_group()
    action.add_argument("--set", choices=("game", "media"))
    action.add_argument("--restore", metavar="FILE", help="Restore a saved first sector")
    mode.add_argument("--backup", help="New host-side first-sector backup (required with --set)")
    mode.add_argument("--yes", action="store_true", help="Confirm the displayed disk noninteractively")
    for command in ("inspect", "prepare", "restore"):
        cmd = sub.add_parser(command, help={"inspect": "Read-only permission preflight",
                                           "prepare": "Grant Xbox app permissions with a recovery journal",
                                           "restore": "Restore original permissions from a recovery journal"}[command])
        cmd.add_argument("target", help="Unmounted external NTFS partition or raw NTFS volume image")
        if command != "restore":
            cmd.add_argument("--access", choices=("modify", "full"), default="modify",
                             help="modify: read/write/delete; full: also change permissions/owner")
        if command == "prepare":
            cmd.add_argument("--expect-volume", help="Require this boot_sha256 from a previous inspection")
            cmd.add_argument("--append", action="store_true",
                             help="Add newly found objects to an existing journal from preparation of this drive")
        if command != "inspect":
            cmd.add_argument("--backup", required=True, help="Host-side SQLite recovery journal")
            cmd.add_argument("--yes", action="store_true", help="Confirm the displayed target noninteractively")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.library and not os.path.isabs(args.library):
            raise DriveError("--library must be an absolute path to a trusted native library.")
        if args.command == "gui":
            from .gui import main as gui_main
            return gui_main()
        if args.command == "list":
            result = {"devices": [device.to_dict() for device in discover()]}
        elif args.command == "doctor":
            result = {"version": __version__, "platform": sys.platform,
                      "library": str(load_library(args.library)._name), "root": os.getuid() == 0}
        elif args.command == "journal":
            result = journal_info(args.backup)
        elif args.command == "mode":
            if args.set and not args.backup:
                raise DriveError("--set requires --backup FILE.")
            if args.backup and not args.set:
                raise DriveError("--backup is only used with --set.")
            writing = bool(args.set or args.restore)
            disk = validate_whole_disk(args.target) if writing else whole_disk(args.target)
            if writing:
                if args.set:
                    consequence = "unreadable here" if args.set == "game" else "unreadable by Xbox game storage"
                    print("Switching to {} mode makes the drive {} until switched back.".format(args.set, consequence), file=sys.stderr)
                else:
                    print("Restoring the saved disk signature may make the drive unreadable here or by Xbox until switched back.", file=sys.stderr)
                if not args.yes:
                    if not sys.stdin.isatty():
                        raise DriveError("Pass --yes after checking the target with mode.")
                    print("Type the complete disk path {} to confirm: ".format(disk), end="", file=sys.stderr, flush=True)
                    if input() != disk:
                        raise DriveError("Confirmation did not match; no changes made.")
                result = switch(disk, args.set, args.backup) if args.set else restore_sector(disk, args.restore)
            else:
                result = dict(analyze(read_head(disk)), disk=disk)
        else:
            if args.command in ("prepare", "restore") and not args.yes:
                if not sys.stdin.isatty():
                    raise DriveError("Pass --yes after checking the target with inspect.")
                if args.command == "prepare" and args.append:
                    message = "Prepare permissions on {} and add to journal {}.".format(args.target, args.backup)
                else:
                    message = "{} permissions on {} using journal {}.".format(args.command.capitalize(), args.target, args.backup)
                print(message, file=sys.stderr)
                print("Type the complete target path to confirm: ", end="", file=sys.stderr, flush=True)
                if input() != args.target:
                    raise DriveError("Confirmation did not match; no permissions changed.")

            def progress(value):
                if args.json:
                    print(json.dumps({"progress": value}), file=sys.stderr, flush=True)
                else:
                    print("[{completed}/{total}] {path}".format(**value), file=sys.stderr, flush=True)

            if args.command == "inspect":
                result = inspect(args.target, args.access, args.library)
            elif args.command == "prepare":
                result = prepare(args.target, args.backup, args.access, args.library, progress,
                                 expected_boot=args.expect_volume, append=args.append)
            else:
                result = restore(args.target, args.backup, args.library, progress)
        print(json.dumps(result, ensure_ascii=True, indent=None if args.json else 2))
        return 3 if result.get("state") == "restore-incomplete" or result.get("ready") is False else 0
    except (DriveError, OSError, sqlite3.Error, ValueError) as exc:
        message = str(exc)
        print(json.dumps({"error": message}) if args.json else "Error: " + message, file=sys.stderr)
        return 1
    except ImportError as exc:
        print("GUI dependency missing. Install with: python3 -m pip install '.[gui]'\n" + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Keep the recovery journal; restore can undo a partial preparation.", file=sys.stderr)
        return 130
