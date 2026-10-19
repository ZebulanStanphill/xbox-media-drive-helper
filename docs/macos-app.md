# Installable macOS applications

The release builds contain a complete Python runtime, Qt interface, NTFS-3G
security library and CLI. The minimum OS is **macOS 15.8**, for both architectures.
They do not depend on a development checkout, a system Python, Homebrew or FUSE.

## Installation

Choose `arm64` for an Apple silicon Mac or `x86_64` for an Intel Mac. The disk
images are in `dist/macos/`. Open the matching `.dmg`, drag the app to
Applications, and open the installed copy. To uninstall, quit it and move the
app to the Trash; keep any recovery journals you still need.

These local builds use ad-hoc code signatures. There is no Developer ID signing
identity or notarization credential configured on the build machine. If macOS
blocks the first launch, attempt to open the installed app and then use System
Settings > Privacy & Security > Open Anyway. Do not disable Gatekeeper globally.
[Apple's instructions](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac)

Disk operations prompt for administrator authorization. The GUI stays
unprivileged and launches the same bundled executable in CLI mode as the helper.
The app must be on the local disk, rather than running from its installation
image, before using disk operations.

Disk operations need **Full Disk Access**: macOS privacy protection blocks raw
access to external drives otherwise, even for the administrator helper. Turn on
Xbox Media Drive Helper in System Settings > Privacy & Security > Full Disk
Access, then quit and reopen it. The app offers to open that pane when access is
blocked. Builds are ad-hoc signed, so macOS treats each new build as a different
app: turn the setting off and on again (or remove and re-add the app) after
installing an update.

macOS mounts NTFS drives automatically. In the app, click **Unmount** on the
selected volume rather than Ejecting it in Finder: Eject disconnects the drive,
and it must stay connected.

## Bundled CLI

```sh
"/Applications/Xbox Media Drive Helper.app/Contents/MacOS/XboxMediaDrive" --cli --help
"/Applications/Xbox Media Drive Helper.app/Contents/MacOS/XboxMediaDrive" --cli list
sudo "/Applications/Xbox Media Drive Helper.app/Contents/MacOS/XboxMediaDrive" --cli inspect /dev/disk4s1
```

Replace the device path with the actual unmounted external NTFS partition. The
same prepare/restore arguments and recovery-journal guarantees apply.

## Rebuild

On a macOS build machine with Command Line Tools, `make`, `pkg-config` and `curl`:

```sh
python3 scripts/build-macos.py
# Build only one architecture:
python3 scripts/build-macos.py --arch arm64
python3 scripts/build-macos.py --arch x86_64
```

The script downloads the Python Software Foundation's signed **3.13.16
universal2 installer**, checks its pinned SHA-256 digest and package signature,
and extracts it under `build/macos-tools/`. It does not install Python system-wide.
It makes this extracted interpreter relocatable for build subprocesses, installs
the pinned packaging dependencies, builds architecture-specific NTFS-3G libraries,
and freezes separate app bundles with PyInstaller. `--skip-native-build` reuses
previously built libraries, but all final bundle verification still runs.

NTFS-3G is compiled with a macOS **15.0** deployment target, earlier than the
required **15.8** runtime. Python and Qt have earlier deployment targets too.
A newer build SDK is allowed; deployment targets and actual API use determine
the OS requirement. A runtime test on 15.8 is still needed to confirm behavior.

With a configured signing identity, use:

```sh
python3 scripts/build-macos.py --codesign-identity 'Developer ID Application: Your Name (TEAMID)'
```

That enables Developer ID signing. It does not claim notarization: an Apple
notary submission and stapling step must be completed separately before
advertising the release as notarized.

## Verification

For each bundle, `scripts/verify-macos.py`:

1. Checks `LSMinimumSystemVersion` is `15.8`.
2. Examines every bundled Mach-O file and requires the intended single CPU
   architecture and a deployment target no newer than 15.8.
3. Rejects library dependencies and runtime search paths pointing to the build
   machine or other non-system absolute locations.
4. Verifies the complete app's code signature.
5. Copies the app into an independent path containing spaces, clears build/Python
   loader environment variables, and runs its bundled CLI and real Qt rendering
   checks using both the offscreen and native Cocoa plugins from an unrelated
   working directory.
6. Confirms the NTFS library came from inside the relocated bundle.

The JSON reports record the actual test OS and host CPU. An Intel binary tested
through Rosetta on Apple silicon is not a physical Intel Mac test. Deployment
target verification is not a runtime test on macOS 15.8; that remains an explicit
validation gap. No packaging verification changes any physical drive.

Re-run verification independently:

```sh
python3 scripts/verify-macos.py 'dist/macos/arm64/Xbox Media Drive Helper.app' --arch arm64
python3 scripts/verify-macos.py 'dist/macos/x86_64/Xbox Media Drive Helper.app' --arch x86_64
```

Running the other architecture requires OS support for executing it (Rosetta
for Intel checks on Apple silicon). The build script also verifies both compressed
disk images and emits SHA-256 checksums and a corresponding-source archive.

## Redistribution

Keep the corresponding-source archive and third-party notices with these builds.
Application/native-library license texts are included in the app. The native
library is loaded from inside the bundle; frozen apps do not fall back to a host
Homebrew install. Qt remains dynamically linked so compatible modified libraries
can be substituted and the modified app re-signed.
