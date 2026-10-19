"""Read disk layouts and reversibly change only the two-byte Xbox signature."""
import errno
import os
import sys

from . import DriveError
from .devices import MAC_DISK_ACCESS, require_host_backup

PC_SIGNATURE = b"\x55\xaa"
XBOX_SIGNATURE = b"\x99\xcc"
SIGNATURE_OFFSET = 0x1FE
SECTOR = 512


def open_disk(path, writable=False):
    try:
        return open(path, "rb+" if writable else "rb", buffering=0)
    except PermissionError as exc:
        # Privacy protection (TCC) denies raw removable-disk access with EPERM, even to root.
        if sys.platform == "darwin" and exc.errno == errno.EPERM:
            raise DriveError(MAC_DISK_ACCESS.format(path)) from exc
        raise


def read_head(path):
    with open_disk(path) as disk:
        head = disk.read(8192)
    if len(head) < 1024:
        raise DriveError("The disk header is too short. No changes made.")
    return head


def analyze(head):
    head = bytes(head)
    signature = {PC_SIGNATURE: "pc", XBOX_SIGNATURE: "xbox"}.get(head[SIGNATURE_OFFSET:SECTOR], "unknown")
    gpt = any(head[offset:offset + 8] == b"EFI PART" for offset in (512, 4096))
    mbr = any(head[offset + 4:offset + 5] not in (b"", b"\0") and
              int.from_bytes(head[offset + 12:offset + 16], "little")
              for offset in range(446, 510, 16))
    return {"signature": signature, "scheme": "gpt" if gpt else "mbr" if mbr else "none",
            "xbox_formatted": gpt and not any(head[:0x1B8]),
            "mode": {"pc": "media", "xbox": "game"}.get(signature),
            "nt_disk_signature": head[440:444].hex()}


def switched(sector0, target):
    """Return sector zero; a full header also identifies GPT without MBR entries."""
    head = sector0
    if target not in ("game", "media"):
        raise DriveError("Choose game or media mode.")
    info = analyze(head)
    if len(head) < SECTOR or info["signature"] == "unknown":
        raise DriveError("Unknown disk signature. No changes made.")
    if info["scheme"] == "none":
        raise DriveError("No partition table found; this disk is blank or not supported. No changes made.")
    if info["mode"] == target:
        raise DriveError("The disk is already in {} mode. No changes made.".format(target))
    return head[:SIGNATURE_OFFSET] + (XBOX_SIGNATURE if target == "game" else PC_SIGNATURE)


def _write_verified(disk, replacement, original, backup):
    """Write sector zero of an open disk and read it back; put the original back if that fails."""
    def write_and_check(sector):
        disk.seek(0)
        if disk.write(sector) != SECTOR:
            raise OSError("Incomplete sector write")
        disk.flush()
        os.fsync(disk.fileno())
        disk.seek(0)
        if disk.read(SECTOR) != sector:
            raise OSError("Sector verification failed")
    try:
        write_and_check(replacement)
    except (OSError, ValueError) as exc:
        try:
            write_and_check(original)
            outcome = "The original sector was restored and verified."
        except (OSError, ValueError):
            outcome = "The original sector could not be restored and verified."
        raise DriveError("Disk write failed: {}. {} Keep the backup: {}".format(exc, outcome, backup)) from exc


def switch(path, target, backup):
    head = read_head(path)
    info = analyze(head)
    replacement = switched(head, target)
    backup = require_host_backup(backup, path)
    if os.path.exists(backup):
        raise DriveError("A boot sector backup already exists at {}. Existing backups are never overwritten; "
                         "choose a new file name. No changes made.".format(backup))
    # Open the disk first so an access failure leaves no stray backup behind.
    with open_disk(path, writable=True) as disk:
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise DriveError("A boot sector backup already exists at {}. Existing backups are never overwritten; "
                             "choose a new file name. No changes made.".format(backup)) from exc
        with os.fdopen(fd, "wb") as stream:
            stream.write(head[:SECTOR])
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(os.path.dirname(backup), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _write_verified(disk, replacement, head[:SECTOR], backup)
    return {"target": path, "previous_mode": info["mode"], "mode": target, "backup": backup,
            "scheme": info["scheme"], "xbox_formatted": info["xbox_formatted"]}


def restore_sector(path, backup):
    backup = require_host_backup(backup, path)
    with open(backup, "rb") as stream:
        original = stream.read(SECTOR + 1)
    if len(original) != SECTOR or original[SIGNATURE_OFFSET:] not in (PC_SIGNATURE, XBOX_SIGNATURE):
        raise DriveError("The backup must contain exactly one 512-byte sector with a known signature. No changes made.")
    current = read_head(path)[:SECTOR]
    if current[:SIGNATURE_OFFSET] != original[:SIGNATURE_OFFSET]:
        raise DriveError("This backup belongs to a different disk, or its partition table changed since the backup. No changes made.")
    with open_disk(path, writable=True) as disk:
        _write_verified(disk, original, current, backup)
    return {"target": path, "mode": analyze(original)["mode"]}
