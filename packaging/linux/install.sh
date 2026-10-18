#!/bin/sh
# Install the launcher entry and icon for the current user (no root needed).
# Run after installing the app so `xbox-media-drive-gui` is on PATH.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
mkdir -p "$data/applications" "$data/icons/hicolor/scalable/apps"
install -m 644 "$here/xbox-media-drive-helper.desktop" "$data/applications/"
install -m 644 "$root/xbox_media_drive/icon.svg" "$data/icons/hicolor/scalable/apps/xbox-media-drive-helper.svg"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$data/icons/hicolor" || true
echo "Installed launcher and icon under $data"
