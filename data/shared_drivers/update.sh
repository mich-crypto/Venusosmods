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

# the settings are not in the download, but keep a copy next to them in case anything goes wrong
[ -f /data/shared_drivers/config.ini ] && cp -p /data/shared_drivers/config.ini /data/shared_drivers/config.ini.pre-update

cp -r "$SRC/data/shared_drivers" "$SRC/data/etc" /data/
[ -f /data/shared_drivers/config.ini ] || { [ -f /data/shared_drivers/config.ini.pre-update ] && cp -p /data/shared_drivers/config.ini.pre-update /data/shared_drivers/config.ini; }
chmod +x /data/shared_drivers/*.sh /data/shared_drivers/*.py \
         /data/shared_drivers/diagnostics/*.py /data/etc/init.d/*/run /data/etc/init.d/*/log/run
rm -rf "$TMP"
echo "Files updated in /data/shared_drivers and /data/etc/init.d"

# remember which commit this is, for the "update available" check on the settings page
SHA=$( (curl -fsSL -m 10 https://api.github.com/repos/mich-crypto/Venusosmods/commits/master 2>/dev/null \
        || wget -q -T 10 -O - https://api.github.com/repos/mich-crypto/Venusosmods/commits/master 2>/dev/null) \
      | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1)
[ -n "$SHA" ] && echo "$SHA" > /data/shared_drivers/.version

# Restart every running driver so it runs the new code: stop it, wait until it is really
# down (a busy driver may take a moment), kill it after 10 s if it still runs, start it again.
# This page (web_ui) last: the update runs in its own session and goes on meanwhile.
restart() {
    s=/service/$1
    svc -d "$s"
    i=0
    while svstat "$s" 2>/dev/null | grep -q ': up'; do
        i=$((i + 1))
        if [ $i -eq 10 ]; then
            pid=$(svstat "$s" | sed -n 's/.*(pid \([0-9]*\)).*/\1/p')
            [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null && echo "$1 did not stop by itself: killed"
        fi
        [ $i -ge 15 ] && break
        sleep 1
    done
    svc -u "$s" && echo "Restarted $1"
}
for svc in /data/etc/init.d/*; do
    name=$(basename "$svc")
    [ "$name" = web_ui ] && continue
    [ -e "/service/$name" ] && restart "$name"
done
[ -e /service/web_ui ] && restart web_ui
echo "Update finished"
