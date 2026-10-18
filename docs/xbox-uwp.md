# Xbox One UWP feasibility

Investigated on October 18, 2026, for running the existing prepare/restore tool directly on Xbox One.

## Decision

Do not advertise an Xbox UWP version of the drive preparation/restoration tool. The documented UWP security model does not provide a general way to grant an app access to a drive whose existing permissions already block that app. The Linux/macOS tool remains the supported implementation.

This conclusion concerns normal sandboxed UWP deployment. It is not a claim that all ACL operations are forbidden in every UWP process. No Xbox hardware testing or Windows/UWP compilation was performed during this investigation.

## What the documentation establishes

Microsoft documents `SetNamedSecurityInfoW` for both desktop and UWP apps. Its DACL parameter requires the caller to have `WRITE_DAC` or to own the object. API availability alone does not provide those permissions. [SetNamedSecurityInfoW](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow)

`WRITE_DAC` is the right to modify a DACL. File-content read/write access is a different set of rights; the existing tool's default Modify mask (`0x001301BF`) does not contain `WRITE_DAC` (`0x00040000`). Full control (`0x001F01FF`) does. [Standard access rights](https://learn.microsoft.com/en-us/windows/win32/secauthz/standard-access-rights)

AppContainer processes are limited to the resources granted to their sandbox. Repackaging the host tool as UWP does not give the process additional access to USB storage or the ability to bypass a denied ACL. [AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)

Microsoft explicitly lists `broadFileSystemAccess` and folder-picker scenarios as unsupported on Xbox. A design that relies on either to select or acquire access to arbitrary USB folders would not have documented Xbox support. [UWP features not supported on Xbox](https://learn.microsoft.com/en-us/uwp/extension-sdks/uwp-limitations-on-xbox)

The `removableStorage` capability enables file access through Windows.Storage, filtered by manifest file-type associations. It is not documented as granting DACL modification rights or bypassing NTFS access checks. [File access permissions](https://learn.microsoft.com/en-us/windows/apps/develop/files/file-access-permissions)

`CreateFile2FromAppW` follows the UWP security model. Substituting a native file-opening API does not by itself establish a supported privileged disk-access path for the existing offline NTFS-3G engine. [CreateFile2FromAppW](https://learn.microsoft.com/en-us/windows/win32/api/fileapifromapp/nf-fileapifromapp-createfile2fromappw)

## Implications for this tool

The permission bootstrap restriction is an inference from those documented requirements: when an Xbox app lacks access to a file and has neither ownership nor a DACL modification grant, it cannot use the ACL API to authorize itself. The app would encounter the same missing permission it was meant to repair. The host implementation solves this by operating offline with root privileges; a UWP wrapper does not supply that equivalent.

An app already granted the necessary rights could potentially edit some accessible ACLs. That conditional case would still need a separate Windows security backend and real Xbox validation. Granting Full control on a computer first would not demonstrate that the app can prepare an initially inaccessible drive on its own.

A console implementation would also need to preserve the existing recovery guarantees. Windows' named security setter can propagate inheritable ACEs to descendants, so a direct call is not a substitute for the current per-object preflight, journal, verification and conflict handling. It must also remain able to verify and resume restoration when removing its own access. Those behaviors have not been implemented or validated on Xbox. [SetNamedSecurityInfoW inheritance behavior](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow)

## What a companion could do

A separately scoped UWP companion could test reading explicitly accessible media, and test creating/reading/deleting its own temporary file where permitted. It would need to distinguish its own sandbox's access from another app's access. A successful test on one folder would not prove drive-wide access, NTFS format, DACL modification rights, or successful preparation/restoration.

No such companion is included in this release. It would not replace the requested preparation/restoration functionality.
