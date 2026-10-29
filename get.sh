#!/bin/sh
# One-command install for Venus OS (Raspberry Pi or any GX device).
#
# Open an SSH session to the device as root (Termius works) and paste:
#
#   wget -qO- https://raw.githubusercontent.com/mich-crypto/Venusosmods/master/get.sh | sh
#
# It downloads the mods, runs a pre-flight check, switches on the settings page and prints
# its address and password. Everything else (gas sensor, fridge, water tank, Starlink) is
# added from that page, step by step. Run it again at any time to update: your settings stay.

set -e

URL=https://github.com/mich-crypto/Venusosmods/archive/refs/heads/master.tar.gz
TMP=/tmp/venusosmods-get

[ "$(id -u)" = 0 ] || { echo "Please run this as root (ssh root@<device>)."; exit 1; }
[ -d /data ] || { echo "There is no /data folder: this does not look like Venus OS."; exit 1; }

echo "Downloading the Venus OS mods ..."
rm -rf "$TMP"
mkdir -p "$TMP"
if command -v wget >/dev/null 2>&1; then
    wget -q -O "$TMP/src.tar.gz" "$URL"
else
    curl -fsSL -o "$TMP/src.tar.gz" "$URL"
fi
tar -xzf "$TMP/src.tar.gz" -C "$TMP"
SRC=$(ls -d "$TMP"/Venusosmods-*)

# a previous config.ini stays untouched: it is not part of the download
[ -f /data/shared_drivers/config.ini ] && cp -p /data/shared_drivers/config.ini /data/shared_drivers/config.ini.pre-update
cp -r "$SRC/data/shared_drivers" "$SRC/data/etc" /data/
rm -rf "$TMP"
chmod +x /data/shared_drivers/*.sh /data/shared_drivers/*.py /data/shared_drivers/diagnostics/*.py \
         /data/etc/init.d/*/run /data/etc/init.d/*/log/run

# remember the version for the update check on the settings page
SHA=$( (wget -q -T 10 -O - https://api.github.com/repos/mich-crypto/Venusosmods/commits/master 2>/dev/null \
        || curl -fsSL -m 10 https://api.github.com/repos/mich-crypto/Venusosmods/commits/master 2>/dev/null) \
      | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1)
[ -n "$SHA" ] && echo "$SHA" > /data/shared_drivers/.version

# switch on the settings page (and any services already enabled keep running)
exec sh /data/shared_drivers/install.sh "$@"
