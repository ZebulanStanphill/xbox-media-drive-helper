<img src="xbox_media_drive/icon.svg" width="64" height="64" alt="">

# Xbox Media Drive Helper

A Linux and macOS desktop app and CLI for preparing an **existing NTFS media volume** for Xbox One apps and restoring its original permissions. It changes NTFS access control lists, keeps file contents, and saves a recovery journal on your computer. It does not format, repartition, or change MBR/GPT signatures.

**Status:** working source release; tested on macOS with disposable NTFS images. Physical Xbox/USB hardware and Linux runtime behavior still need validation. The CI workflow covers Linux and macOS but has not been run here. Keep a separate backup of irreplaceable files: an ACL journal is not a backup of file data or damaged filesystem metadata.

## What “Xbox media drive” means

For media, the drive remains ordinary NTFS and remains readable by PCs. Select **Use for Media** when connecting it to Xbox. The unusual disk signature used by game-storage tools is outside this app's scope; an Xbox game drive is not a media volume to convert in place. [Seagate's Xbox One instructions](https://www.seagate.com/gb/en/support/kb/game-drive-for-xbox-one-setup-and-troubleshooting-006199en/) distinguish media use from the console's game-storage formatting.

The tool works only on NTFS. exFAT and FAT32 volumes have no file permissions, so there is nothing to grant on them; the tool says so and makes no changes. macOS may list an exFAT partition as "Windows_NTFS" because MBR uses one partition type code for both.

Some sandboxed Xbox apps need an NTFS grant to **ALL APPLICATION PACKAGES**, SID `S-1-15-2-1`. This tool adds that grant to the volume root and every existing user file/directory, with object/container inheritance on directories. It preserves other ACEs, ownership and audit ACLs. The default **Modify** grant includes read, write, execute and delete; **Full control** additionally permits changing permissions and ownership. The grant applies to packaged apps generally, not one particular Xbox app. See Microsoft's [AppContainer security model](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation), [DACL behavior](https://learn.microsoft.com/en-us/windows/win32/secauthz/dacls-and-aces), and the existing [Windows Xbox permission tool](https://github.com/YTReviveMe/Xbox-UWP-Permission-Tool).

ACLs cannot override an app's own sandbox/capabilities, unsupported codecs, EFS encryption, a file's read-only attribute, or console/USB compatibility. Preparing permissions is not proof of compatibility with every UWP app. Microsoft documents [removable-storage capability requirements](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/app-capability-declarations).

## Xbox One / UWP feasibility

There is no Xbox UWP build of the preparation/restoration tool. A normal sandboxed app cannot reliably prepare a drive whose existing permissions deny that app access: changing its DACL requires ownership or `WRITE_DAC`, and ordinary file read/write access does not include that right. Microsoft also lists `broadFileSystemAccess` as unsupported on Xbox. This is a permission bootstrap problem, not a need to change the MBR.

Windows does expose ACL APIs to UWP, so an already-authorized app may be able to edit permissions on objects for which it has the required rights. That is a narrower case than preparing an arbitrary existing NTFS drive. The default **Modify** grant made by this tool deliberately does not include `WRITE_DAC`. A **Full control** grant includes it, but does not establish that a particular Xbox app or console build supports the complete operation.

Preparation and restoration therefore remain host-side operations on Linux/macOS. A console companion could check access to already-accessible content, but such a check would not repair missing permissions or prove access for other apps. See the [feasibility findings and Microsoft references](docs/xbox-uwp.md). No Xbox runtime tests were performed.

## Install

### macOS application

Self-contained apps for **macOS 15.8 or later** are built in `dist/macos/`:

- `Xbox-Media-Drive-Helper-<version>-arm64.dmg` for Apple silicon (M-series Macs).
- `Xbox-Media-Drive-Helper-<version>-x86_64.dmg` for Intel Macs.

Open the matching disk image, drag **Xbox Media Drive Helper.app** to **Applications**, then launch the installed copy. Python, Qt and NTFS-3G are bundled; no Python, Homebrew or FUSE installation is needed on those Macs. The CLI is also included inside the app. See [macOS packaging and verification](docs/macos-app.md).

The local builds are ad-hoc signed, not Apple notarized. If macOS blocks the first launch, use **System Settings → Privacy & Security → Open Anyway** after attempting to open the installed app. [Apple's instructions](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac)

Disk operations also need **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access → Xbox Media Drive Helper), then a relaunch. Without it macOS blocks raw drive access even for the administrator helper; the app offers to open that pane. Because builds are ad-hoc signed, re-enable the setting after installing a new build.

The bundle minimum OS and every bundled Mach-O deployment target are checked against 15.8. Runtime checks are performed on the available build Mac; this is not a claim of testing on macOS 15.8 itself. Architecture-specific verification reports accompany the builds.

### From source / Linux

Requirements: Python 3.9+, Linux or macOS, and NTFS-3G's shared library. The GUI uses PySide6. Disk access is offline through the library; **FUSE, macFUSE and a mounted NTFS write driver are not required**.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[gui]'
```

For just the CLI, install `-e .` instead. All commands below assume the project directory is your working directory.

On Debian/Ubuntu, an installed `ntfs-3g` package normally provides the runtime library:

```sh
sudo apt-get install ntfs-3g
.venv/bin/xbox-media-drive doctor
```

For macOS, or to use the same tested library on Linux, build a project-local copy. The script downloads **NTFS-3G 2026.9.28**, verifies its pinned SHA-256 digest, and installs the library and NTFS image tools in `.native/`. It requires no root access and makes no changes to disks. It needs a C compiler, `make`, `pkg-config` and `curl`.

```sh
# macOS prerequisites: install Xcode Command Line Tools if needed, then:
brew install pkgconf

# Debian/Ubuntu build prerequisites instead:
# sudo apt-get install build-essential pkg-config curl

.venv/bin/python scripts/build-native.py
.venv/bin/xbox-media-drive doctor
```

Use `--library /absolute/path/to/libntfs-3g.dylib` (macOS) or `.so` (Linux) before the subcommand to choose another trusted library. The GUI has an equivalent field. System libraries and a project-local `.native/lib` are detected automatically. Native code loaded as root must come from a trusted installation.

## Desktop app

```sh
.venv/bin/xbox-media-drive-gui
# or: .venv/bin/xbox-media-drive gui
```

1. Select the external NTFS partition, or open a raw NTFS volume image.
2. Click **Unmount** to unmount it (inspecting a mounted volume also offers to). Do not use Eject or the OS's eject button: that disconnects the drive. If you already ejected it, unplug and reconnect it, then click **Refresh drives**.
3. Click **Inspect permissions**. This is a read-only scan.
4. Click **Prepare for Xbox**, save a new recovery journal on your computer, and confirm the displayed target.
5. When complete, connect it to Xbox and choose **Use for Media**.
6. To reverse the operation, reconnect the drive, click **Unmount**, open **Restore permissions**, and select its original journal.

The UI stays unprivileged. macOS shows its standard administrator prompt for the separate CLI helper. Linux uses `pkexec` and needs a working PolicyKit authentication agent; install your distribution's polkit package if necessary. If graphical authorization is unavailable, use the CLI with `sudo`. NTFS-3G requires UID 0 even for image-file inspections. Journals created by the helper are root-owned, mode `0600`; restore through the app or inspect/copy them with `sudo`.

On Linux, `packaging/linux/install.sh` adds a launcher entry and the icon for your user (no root needed); run it after installing the app.

## CLI

List external volumes without root:

```sh
.venv/bin/xbox-media-drive list
```

Select a **partition**, e.g. `/dev/sdb1` on Linux or `/dev/disk4s1` on macOS. Replace the examples with your actual NTFS partition. Unmount it without disconnecting the disk:

```sh
# Linux example
udisksctl unmount -b /dev/sdb1

# macOS example
diskutil unmount /dev/disk4s1
```

Inspect, prepare, then restore:

```sh
sudo .venv/bin/xbox-media-drive inspect /dev/disk4s1
sudo .venv/bin/xbox-media-drive prepare /dev/disk4s1 --backup "$PWD/media-original.sqlite"
sudo .venv/bin/xbox-media-drive restore /dev/disk4s1 --backup "$PWD/media-original.sqlite"
```

Writes require typing the target path, or explicit `--yes` for automation. The journal must be a **new file** for preparation; an existing journal is never overwritten. Both operations verify every changed descriptor. `--access full` selects Full control for apps that need it. `prepare --expect-volume SHA256` requires the `boot_sha256` returned by an earlier inspection; the GUI always supplies it to detect a swapped drive or a reused device path.

```sh
sudo .venv/bin/xbox-media-drive --json inspect /dev/sdb1
sudo .venv/bin/xbox-media-drive --json prepare /dev/sdb1 --backup /home/me/media.sqlite --yes
sudo .venv/bin/xbox-media-drive journal /home/me/media.sqlite
```

`--json` writes one result object to stdout; native diagnostics and progress go to stderr. Exit codes: `0` success; `1` failure; `2` invalid arguments; `3` inspection blockers or an incomplete restore; `130` interruption. Library discovery failures explain how to install or select the dependency.

## Preservation and recovery

- **No partition changes or file-content writes.** The only requested changes are NTFS DACLs. NTFS-3G also maintains its own filesystem metadata when closing a writable volume.
- **Offline/external only.** Mounted volumes and internal physical disks are refused. Select a volume boot sector, not an MBR/GPT whole-disk image. Attached disk images, NTFS metadata records, Windows' root `System Volume Information` and `$RECYCLE.BIN` folders, and reparse points are excluded/refused as appropriate. Raw NTFS “superfloppy” volumes without a partition table are accepted.
- **Preflight before ACL writes.** All planned descriptors are validated and journaled before changing the first ACL. Existing deny ACEs, complex ACLs, noncanonical ACL order, invalid descriptors and EFS files stop automatic preparation. Reparse points are skipped and reported; their targets are not followed. Hard-linked files are processed once.
- **Durable journal.** Each original and proposed security descriptor is stored in a host-side SQLite journal before changes. Full synchronous commits, and full filesystem sync on macOS, protect the journal. Interrupted operations retain the journal; restore checks actual NTFS descriptors instead of trusting possibly stale progress flags.
- **Restore matches the volume and file identity.** Boot-sector fingerprint and NTFS volume serial prevent using the wrong journal. Files are matched by their MFT record **and sequence number**, so renamed files can be restored and reused/deleted records are not mistaken for the original files. Byte-identical cloned volumes share these identifiers; choose the intended physical drive yourself.
- **Later edits are preserved.** If an object's current descriptor differs from both the recorded original and the prepared descriptor, restore reports a conflict. Missing objects are reported. It restores other matching objects, retains the journal, and returns exit code `3` for follow-up. Retrying restore is safe.
- **The journal is the only way back.** Automatic restoration needs the journal saved at preparation. If it is lost, remove the **ALL APPLICATION PACKAGES** entries manually in Windows (Security tab, or `icacls`).
- **New files retain current ACLs.** Files created after preparation have no recorded original descriptor. Their inherited app grant is left in place. This does not change the filesystem type: the drive remains standard NTFS throughout.

A journal reverses this tool's recorded permission changes, not unknown changes made by another utility. If your drive already has Xbox app permissions, inspection reports that; the tool cannot reconstruct permissions from before its first use. It never runs repair, clears hibernation, or enables a force/recovery mount. Fully shut down Windows and check the filesystem there if NTFS-3G refuses a writable session.

## Tests

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python scripts/test-native.py .native
```

The ordinary suite covers binary descriptor parsing, ACL preservation/inheritance, deny handling, CLI behavior, GUI gating/layout/authorization quoting, wrong-volume refusal, renamed/reused records, conflicts, failed journal writes, interrupted preparation and repeatable restoration. GUI checks are skipped if PySide6 is not installed.

The native suite builds an **image-only test harness** against NTFS-3G and creates disposable 64 MiB NTFS images, including nested directories, Unicode names, hard links, a Windows system folder and Windows-style auto-inherited descriptors. It exercises the real library's enumeration, security reads/writes and flushes, verifies that read-only inspection changes no image bytes, checks media bytes and the unchanged boot sector, and tests interruption recovery. A test-only context constructor permits owned regular-file images without root; production always uses NTFS-3G's root-checking initializer, which is tested separately. The harness refuses devices and is not loaded by the application. The native suite is skipped by ordinary discovery and run explicitly by the second command.

Upstream API: [NTFS-3G security.h](https://github.com/tuxera/ntfs-3g/blob/2026.9.28/include/ntfs-3g/security.h), [security.c](https://github.com/tuxera/ntfs-3g/blob/2026.9.28/libntfs-3g/security.c), [release and checksums](https://github.com/tuxera/ntfs-3g/releases/tag/2026.9.28).

GPL-2.0-or-later. See [LICENSE](LICENSE). Unaffiliated with Microsoft or Xbox.
