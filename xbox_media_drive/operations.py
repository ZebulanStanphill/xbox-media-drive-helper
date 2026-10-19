"""Shared CLI/GUI operations and durable, resumable permission journals."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import DriveError, __version__
from .devices import identity, require_host_backup, validate_target
from .native import Volume
from .security import Descriptor, grant, lost_auto_inherited, signature


def inspect(target, access="modify", library=None, volume_factory=Volume):
    target = validate_target(target)
    info = identity(target)
    total = changes = directories = 0
    blockers = []
    previously_prepared = False
    with volume_factory(target, library=library) as volume:
        for entry in volume.walk():
            raw = volume.get(entry.path)
            total += 1
            directories += entry.directory
            try:
                needs_change = signature(grant(raw, entry.directory, access)) != signature(raw)
                changes += needs_change
                if entry.path == "/":
                    previously_prepared = bool(Descriptor.parse(raw).dacl) and not needs_change
            except DriveError as exc:
                if len(blockers) < 30:
                    blockers.append({"path": entry.path, "reason": str(exc)})
        skipped = volume.skipped
        system_skipped = getattr(volume, "system_skipped", [])
    return {"target": target, "identity": info, "objects": total, "directories": directories,
            "changes_needed": changes, "previously_prepared": previously_prepared,
            "access": access, "blockers": blockers,
            "skipped_reparse_points": skipped, "skipped_system_paths": system_skipped, "ready": not blockers}


@contextmanager
def journal(path, create=False):
    if create:
        # Never clobber another recovery journal; secure before SQLite opens it.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    elif not os.path.isfile(path) or os.path.islink(path):
        raise DriveError("Select an existing recovery journal file, not a symlink.")
    connection = sqlite3.connect(path, timeout=1)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        # On macOS this requests F_FULLFSYNC, including physical device caches.
        connection.execute("PRAGMA fullfsync=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if create:
            connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE entries (reference TEXT PRIMARY KEY, path TEXT NOT NULL,
                    original BLOB NOT NULL, prepared BLOB NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0, restored INTEGER NOT NULL DEFAULT 0);
            """)
            # Make the journal's directory entry durable before touching NTFS.
            parent = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        yield connection
    finally:
        connection.close()


def put_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps(value)))
    db.commit()


def get_meta(db, key):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    if row is None:
        raise DriveError("Recovery journal is missing its {} field.".format(key))
    return json.loads(row[0])


def prepare(target, backup, access="modify", library=None, progress=lambda value: None, volume_factory=Volume,
            expected_boot=None, append=False):
    target = validate_target(target)
    info = identity(target)
    if expected_boot is not None and info["boot_sha256"] != expected_boot:
        raise DriveError("The selected volume no longer matches the inspection. Inspect the drive again before preparing.")
    backup = require_host_backup(backup, target)
    changed = 0
    with journal(backup, create=not append) as db:
        if append:
            if get_meta(db, "format") != 1:
                raise DriveError("Unsupported recovery journal version.")
            if get_meta(db, "identity") != info:
                raise DriveError("This recovery journal belongs to a different NTFS volume. No permissions changed.")
            # A restore that failed before touching the drive leaves no restored rows and is no bar.
            if db.execute("SELECT 1 FROM entries WHERE restored=1 LIMIT 1").fetchone():
                raise DriveError("This journal was already used to restore the drive. Prepare with a new journal.")
            put_meta(db, "updated", datetime.now(timezone.utc).isoformat())
        else:
            put_meta(db, "format", 1)
            put_meta(db, "identity", info)
            put_meta(db, "created", datetime.now(timezone.utc).isoformat())
            put_meta(db, "access", access)
        put_meta(db, "tool_version", __version__)
        previous_state = get_meta(db, "state") if append else None
        put_meta(db, "state", "planning")
        touched = False
        try:
            with volume_factory(target, writable=True, library=library) as volume:
                if identity(target) != info:
                    raise DriveError("The target volume changed while opening it. No permissions changed.")
                # Complete preflight and journal every proposed change BEFORE writing any ACL.
                paths = {}
                for entry in volume.walk():
                    paths[str(entry.reference)] = entry.path
                    raw = volume.get(entry.path)
                    try:
                        desired = grant(raw, entry.directory, access)
                    except DriveError as exc:
                        raise DriveError("{}: {}".format(entry.path, exc)) from exc
                    if signature(desired) != signature(raw):
                        row = db.execute("SELECT * FROM entries WHERE reference=?",
                                         (str(entry.reference),)).fetchone() if append else None
                        if row is None:
                            db.execute("INSERT INTO entries(reference,path,original,prepared) VALUES (?,?,?,?)",
                                       (str(entry.reference), entry.path, raw, desired))
                        elif not row["applied"] and signature(raw) == signature(row["original"]):
                            if signature(desired) != signature(row["prepared"]):
                                raise DriveError("{}: recorded in this journal with a different access level. "
                                                 "Prepare with a new journal.".format(entry.path))
                        else:
                            raise DriveError("{}: its permissions changed since this journal recorded it. "
                                             "Prepare with a new journal.".format(entry.path))
                db.commit()
                put_meta(db, "skipped", volume.skipped)
                put_meta(db, "skipped_system_paths", getattr(volume, "system_skipped", []))
                put_meta(db, "state", "applying")
                pending = " WHERE applied=0" if append else ""
                count = db.execute("SELECT COUNT(*) FROM entries" + pending).fetchone()[0]
                for index, row in enumerate(db.execute("SELECT * FROM entries" + pending + " ORDER BY rowid")):
                    # Rows left pending by an interrupted run may since have been renamed or deleted;
                    # use the current path, and leave deleted objects for restore to report as missing.
                    path = paths.get(row["reference"])
                    if path is None:
                        continue
                    # Persisted original already exists even if write or verification fails.
                    current = signature(volume.get(path))
                    prepared = signature(row["prepared"])
                    if append and (current == prepared or lost_auto_inherited(current, prepared)):
                        pass  # A prior run wrote this ACL but did not mark it durably.
                    elif current == signature(row["original"]):
                        touched = True
                        volume.set(path, row["prepared"])
                        if signature(volume.get(path)) != prepared:
                            raise DriveError("Permission verification failed for {}".format(path))
                        changed += 1
                    else:
                        raise DriveError("Permissions changed during preflight: {}".format(path))
                    db.execute("UPDATE entries SET applied=1 WHERE reference=?", (row["reference"],))
                    db.commit()
                    progress({"completed": index + 1, "total": count, "path": path})
            # Complete means the volume was also successfully flushed/closed.
            put_meta(db, "state", "prepared")
        except BaseException:
            # Do not mask the failure if the host has run out of journal space.
            try:
                # An append refused before its first write leaves a prepared drive prepared.
                put_meta(db, "state", previous_state if previous_state == "prepared" and not touched else "interrupted")
            except sqlite3.Error:
                pass
            raise
    return {"target": target, "changed": changed, "journal": backup, "state": "prepared", "appended": append}


def restore(target, backup, library=None, progress=lambda value: None, volume_factory=Volume):
    target = validate_target(target)
    backup = require_host_backup(backup, target)
    restored = unchanged = 0
    conflicts, missing = [], []
    with journal(backup) as db:
        if get_meta(db, "format") != 1:
            raise DriveError("Unsupported recovery journal version.")
        if get_meta(db, "identity") != identity(target):
            raise DriveError("This recovery journal belongs to a different NTFS volume. No permissions changed.")
        put_meta(db, "state", "restoring")
        try:
            with volume_factory(target, writable=True, library=library) as volume:
                if get_meta(db, "identity") != identity(target):
                    raise DriveError("The target volume changed while opening it. No permissions changed.")
                # Include the sequence number: a deleted/reused MFT slot is a new object.
                objects = {str(entry.reference): entry.path for entry in volume.walk()}
                total = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                rows = db.execute("SELECT * FROM entries ORDER BY rowid DESC")
                for index, row in enumerate(rows):
                    path = objects.get(row["reference"])
                    if path is None:
                        missing.append(row["path"])
                        continue
                    current = signature(volume.get(path))
                    if current == signature(row["original"]):
                        unchanged += 1
                    elif current == signature(row["prepared"]) or lost_auto_inherited(current, signature(row["prepared"])):
                        volume.set(path, row["original"])
                        if signature(volume.get(path)) != signature(row["original"]):
                            raise DriveError("Restoration verification failed for {}".format(path))
                        restored += 1
                    else:
                        # Never overwrite permissions edited since preparation, regardless
                        # of the applied flag: a crash may leave that flag out of date.
                        conflicts.append(path)
                        continue
                    db.execute("UPDATE entries SET restored=1 WHERE reference=?", (row["reference"],))
                    db.commit()
                    progress({"completed": index + 1, "total": total, "path": path})
            state = "restore-incomplete" if conflicts or missing else "restored"
            put_meta(db, "state", state)
        except BaseException:
            try:
                put_meta(db, "state", "restore-interrupted")
            except sqlite3.Error:
                pass
            raise
    return {"target": target, "restored": restored, "already_original": unchanged,
            "conflicts": conflicts, "missing": missing, "state": state, "journal": backup}


def journal_info(path):
    # Open read-only: inspecting a journal does not change it.
    from pathlib import Path
    uri = Path(path).expanduser().resolve(strict=True).as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        metadata = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM metadata")}
        metadata["entries"] = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        return metadata
