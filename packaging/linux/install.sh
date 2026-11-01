#!/bin/sh
# Install the launcher entry, icon and KDE Disks & Devices action for the current user (no root needed).
# Run after installing the app so `xbox-media-drive-gui` is on PATH.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
[ -d "$data/solid/actions" ] || first=1
mkdir -p "$data/applications" "$data/icons/hicolor/scalable/apps" "$data/solid/actions"
install -m 644 "$here/xbox-media-drive-helper.desktop" "$data/applications/"
install -m 644 "$here/xbox-media-drive-helper-solid.desktop" "$data/solid/actions/xbox-media-drive-helper.desktop"
install -m 644 "$root/xbox_media_drive/icon.svg" "$data/icons/hicolor/scalable/apps/xbox-media-drive-helper.svg"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$data/icons/hicolor" || true
echo "Installed launcher, icon and KDE Disks & Devices action under $data"
if [ "${first:-}" = 1 ]; then
    echo "Plasma only notices the first Disks & Devices action after it restarts: log out and back in, or run"
    echo "  systemctl --user restart plasma-plasmashell"
fi
