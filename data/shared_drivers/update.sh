#!/bin/sh
# Download the latest drivers from GitHub and install them over the old ones.
# Run ON THE PI (works from a phone/iPad SSH app, no computer needed):
#   sh /data/shared_drivers/update.sh
# config.ini is not part of the download, so your settings are kept.
# Services that are running are restarted afterwards.

set -e

URL=https://github.com/mich-crypto/Venusosmods/archive/refs/heads/master.tar.gz
TMP=/tmp/venusosmods-update

rm -rf "$TMP"
mkdir -p "$TMP"
echo "Downloading $URL"
if command -v wget >/dev/null 2>&1; then
    wget -q -O "$TMP/src.tar.gz" "$URL"
else
    curl -fsSL -o "$TMP/src.tar.gz" "$URL"
fi
tar -xzf "$TMP/src.tar.gz" -C "$TMP"
SRC=$(ls -d "$TMP"/Venusosmods-*)

cp -r "$SRC/data/shared_drivers" "$SRC/data/etc" /data/
chmod +x /data/shared_drivers/*.sh /data/shared_drivers/*.py \
         /data/shared_drivers/diagnostics/*.py /data/etc/init.d/*/run /data/etc/init.d/*/log/run
rm -rf "$TMP"
echo "Files updated in /data/shared_drivers and /data/etc/init.d"

# remember which commit this is, for the "update available" check on the settings page
SHA=$( (curl -fsSL -m 10 https://api.github.com/repos/mich-crypto/Venusosmods/commits/master 2>/dev/null \
        || wget -q -T 10 -O - https://api.github.com/repos/mich-crypto/Venusosmods/commits/master 2>/dev/null) \
      | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1)
[ -n "$SHA" ] && echo "$SHA" > /data/shared_drivers/.version

for svc in /data/etc/init.d/*; do
    name=$(basename "$svc")
    if [ -e "/service/$name" ]; then
        svc -t "/service/$name" && echo "Restarted $name"
    fi
done
