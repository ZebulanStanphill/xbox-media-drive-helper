# Bundled software

Xbox Media Drive Helper is GPL-2.0-or-later; its complete application source and
build scripts accompany these builds. It is unaffiliated with Microsoft or Xbox.

- CPython 3.13.16: Python Software Foundation License, including its bundled
  third-party components. License: https://docs.python.org/3.13/license.html
  Source: https://www.python.org/ftp/python/3.13.16/Python-3.13.16.tar.xz
- Qt for Python / PySide6, Shiboken6 and Qt 6.9.3: their respective LGPL/GPL and
  third-party licenses. License texts from the installed wheels are included
  alongside this notice. Qt libraries are dynamically linked and may be replaced
  with compatible modified libraries; a modified app can be re-signed locally.
  Sources: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.9.3-src/
  and https://download.qt.io/archive/qt/6.9/6.9.3/submodules/
- NTFS-3G 2026.9.28: GPL-2.0-or-later. Its unmodified source archive is included
  in the corresponding-source archive. Source/checksums:
  https://github.com/tuxera/ntfs-3g/releases/tag/2026.9.28
- PyInstaller 6.22.3: GPL-2.0-or-later with the bootloader exception that permits
  distribution of frozen applications. Its license text accompanies this build.
  Source/license: https://github.com/pyinstaller/pyinstaller/tree/v6.22.3

Any remaining Python libraries are included under the licenses in their
distribution metadata. Packaging tools are not required on the destination Mac.
