"""Platform discovery and conservative checks before offline access."""
import errno
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import DriveError

MAC_DISK_ACCESS = ("macOS blocked access to {}. Open System Settings → Privacy & Security → Full Disk Access, "
                   "turn on Xbox Media Drive Helper (add it with + if it isn't listed), then quit and reopen the app "
                   "and try again.")


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, check=True, timeout=30)
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # CalledProcessError's text omits the tool's own explanation, e.g. diskutil's "dissented by PID".
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = "\n".join(text.decode(errors="replace").strip() for text in (exc.stderr, exc.stdout)
                               if text and text.strip())
        raise DriveError("Could not run {}: {}".format(args[0], detail or exc)) from exc


@dataclass
class Device:
    path: str
    label: str
    size: int
    filesystem: str
    mounted: bool
    external: bool

    def to_dict(self):
        return asdict(self)


def linux_nodes():
    data = json.loads(run(["lsblk", "--json", "--bytes", "--paths", "--output",
                           "NAME,PATH,TYPE,FSTYPE,LABEL,SIZE,MOUNTPOINTS,RM,TRAN,HOTPLUG"]))
    found = []

    def visit(node, external=False):
        external = external or bool(node.get("rm")) or bool(node.get("hotplug")) or node.get("tran") == "usb"
        found.append((node, external))
        for child in node.get("children", []):
            visit(child, external)

    for node in data.get("blockdevices", []):
        visit(node)
    return found


def mac_info(path):
    return plistlib.loads(run(["diskutil", "info", "-plist", path]))


def discover():
    if sys.platform == "darwin":
        data = plistlib.loads(run(["diskutil", "list", "-plist", "external", "physical"]))
        devices = []
        for disk in data.get("AllDisksAndPartitions", []):
            for node in disk.get("Partitions", [disk]):
                path = "/dev/" + node["DeviceIdentifier"]
                info = mac_info(path)
                devices.append(Device(path, info.get("VolumeName", "Unnamed volume"),
                                      info.get("TotalSize", info.get("Size", 0)),
                                      info.get("FilesystemType", node.get("Content", "unknown")),
                                      bool(info.get("MountPoint")), True))
        return devices
    if sys.platform.startswith("linux"):
        return [Device(node["path"], node.get("label") or node["path"], int(node.get("size") or 0),
                       node.get("fstype") or "unknown", any(node.get("mountpoints") or []), external)
                for node, external in linux_nodes() if external and node["type"] in ("part", "disk")
                and (node["type"] == "part" or not node.get("children"))]
    raise DriveError("Supported platforms are Linux and macOS.")


def resolve_path(path):
    try:
        return str(Path(path).expanduser().resolve(strict=True))
    except FileNotFoundError:
        if not str(path).startswith("/dev/"):
            raise DriveError("{} was not found.".format(path)) from None
        raise DriveError("{} is no longer connected. If you ejected it, unplug and reconnect the drive, "
                         "click Refresh, and use Unmount in this app instead of Eject.".format(path)) from None


def mac_device(target):
    """Return the buffered block device path and its info for an external physical macOS device."""
    if not re.fullmatch(r"/dev/r?disk\d+(s\d+)?", target):
        raise DriveError("Unsupported macOS device path.")
    # Use buffered block devices: these permit the small reads used by this tool.
    target = target.replace("/dev/rdisk", "/dev/disk", 1)
    info = mac_info(target)
    if info.get("Internal", True) or info.get("VirtualOrPhysical") == "Virtual":
        raise DriveError("Only external physical drives can be modified.")
    return target, info


def require_mac_partition(info):
    if info.get("WholeDisk") and info.get("Content") in ("GUID_partition_scheme", "FDisk_partition_scheme"):
        raise DriveError("Select the NTFS partition (for example /dev/disk4s1), not the whole disk.")


def linux_device(target):
    """Return the lsblk node of an external Linux device."""
    match = next(((node, external) for node, external in linux_nodes()
                  if os.path.realpath(node["path"]) == target), None)
    if not match or not match[1]:
        raise DriveError("Only external drives reported by lsblk can be modified.")
    return match[0]


def validate_target(path):
    """Return a canonical, unmounted image or external partition path."""
    target = resolve_path(path)
    mode = os.stat(target).st_mode
    if stat.S_ISREG(mode):
        if os.stat(target).st_nlink != 1:
            raise DriveError("Use an NTFS image with exactly one host filesystem link.")
        if sys.platform == "darwin":
            images = plistlib.loads(run(["hdiutil", "info", "-plist"]))
            if any(os.path.realpath(image.get("image-path", "")) == target for image in images.get("images", [])):
                raise DriveError("Detach this image with hdiutil before accessing it offline.")
        elif sys.platform.startswith("linux"):
            for backing in Path("/sys/block").glob("loop*/loop/backing_file"):
                name = backing.read_text().strip()
                if name and os.path.exists(name) and os.path.samefile(name, target):
                    raise DriveError("Detach this image from its loop device before accessing it offline.")
        return target
    if not (stat.S_ISBLK(mode) or stat.S_ISCHR(mode)):
        raise DriveError("Select an external NTFS partition or an NTFS volume image.")
    if sys.platform == "darwin":
        target, info = mac_device(target)
        if info.get("MountPoint"):
            raise DriveError("Unmount the selected volume first: click Unmount in this app, or run diskutil unmount {}. "
                             "Don't use Eject; it disconnects the drive.".format(target))
        require_mac_partition(info)
    elif sys.platform.startswith("linux"):
        node = linux_device(target)
        if any(node.get("mountpoints") or []) or node.get("children"):
            raise DriveError("Unmount the selected volume (click Unmount in this app, or run udisksctl unmount -b {}) "
                             "and select its NTFS partition, not the whole disk.".format(target))
        # Kernel mount info includes bind mounts, aliases and other mount namespaces visible here.
        device_id = os.stat(target).st_rdev
        key = "{}:{}".format(os.major(device_id), os.minor(device_id))
        if any(line.split()[2] == key for line in Path("/proc/self/mountinfo").read_text().splitlines()):
            raise DriveError("The selected device is mounted. Unmount it first (click Unmount in this app).")
    else:
        raise DriveError("Supported platforms are Linux and macOS.")
    return target


def is_mounted(path):
    """Whether path is a mounted device. Images and missing paths are not; validate_target reports those."""
    try:
        target = str(Path(path).expanduser().resolve(strict=True))
        mode = os.stat(target).st_mode
    except OSError:
        return False
    if not (stat.S_ISBLK(mode) or stat.S_ISCHR(mode)):
        return False
    if sys.platform == "darwin":
        if not re.fullmatch(r"/dev/r?disk\d+(s\d+)?", target):
            return False
        info = mac_info(target.replace("/dev/rdisk", "/dev/disk", 1))
        return bool(info.get("MountPoint"))
    if sys.platform.startswith("linux"):
        return any(any(node.get("mountpoints") or []) for node, _ in linux_nodes()
                   if os.path.realpath(node["path"]) == target)
    return False


def unmount(path):
    """Unmount a mounted external partition without ejecting or disconnecting it. Needs no privileges."""
    target = resolve_path(path)
    mode = os.stat(target).st_mode
    if not (stat.S_ISBLK(mode) or stat.S_ISCHR(mode)):
        raise DriveError("Only external partitions can be unmounted.")
    if sys.platform == "darwin":
        target, info = mac_device(target)
        require_mac_partition(info)
        command = ["diskutil", "unmount", target]
    elif sys.platform.startswith("linux"):
        if linux_device(target).get("children"):
            raise DriveError("Select the NTFS partition, not the whole disk.")
        if not shutil.which("udisksctl"):
            raise DriveError("udisksctl is not installed. Unmount {0} with your file manager or sudo umount {0}.".format(target))
        command = ["udisksctl", "unmount", "-b", target]
    else:
        raise DriveError("Supported platforms are Linux and macOS.")
    try:
        run(command)
    except DriveError as exc:
        raise DriveError("{}\nClose any apps or windows using the drive, then try again.".format(exc)) from exc
    return target


def identity(path):
    try:
        with open(path, "rb", buffering=0) as disk:
            boot = disk.read(512)
    except PermissionError as exc:
        # Privacy protection (TCC) denies raw removable-disk access with EPERM, even to root.
        if sys.platform == "darwin" and exc.errno == errno.EPERM:
            raise DriveError(MAC_DISK_ACCESS.format(path)) from exc
        raise
    if boot[3:11] == b"EXFAT   ":
        raise DriveError("This volume is exFAT, not NTFS. exFAT has no file permissions, so there is nothing "
                         "for this tool to change. No changes made.")
    if len(boot) != 512 or boot[3:11] != b"NTFS    " or boot[510:512] != b"\x55\xaa":
        raise DriveError("This is not an NTFS volume boot sector. Select a partition, not an MBR/GPT disk. No changes made.")
    sector_size = struct.unpack_from("<H", boot, 11)[0]
    sectors = struct.unpack_from("<Q", boot, 40)[0]
    if sector_size not in (512, 1024, 2048, 4096) or not sectors or not boot[13]:
        raise DriveError("Invalid NTFS geometry.")
    return {"serial": boot[72:80].hex(), "boot_sha256": hashlib.sha256(boot).hexdigest(),
            "volume_bytes": sectors * sector_size}


def require_host_backup(backup, target):
    path = Path(backup).expanduser().absolute()
    if not path.parent.is_dir() or path.is_symlink():
        raise DriveError("Choose a journal in an existing folder on your computer.")
    if str(path.resolve()) == target or (path.exists() and os.path.samefile(path, target)):
        raise DriveError("The recovery journal must be separate from the NTFS volume.")
    # A selected unmounted block device cannot contain an accessible host path.
    return str(path)
