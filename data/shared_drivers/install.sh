#!/bin/sh
# Install / re-enable the custom drivers on Venus OS.
#
# Run ON THE GX/RASPBERRY PI after copying this repo's data/ folder to /data:
#   scp -r data/shared_drivers data/etc root@venus.local:/data/
#   ssh root@venus.local sh /data/shared_drivers/install.sh mopeka_standard esp32_hydro fridge web_ui
#
# Arguments: the services to enable (folders in /data/etc/init.d).
#   --force   enable even if the pre-flight diagnostics report a FAIL
# Safe to run repeatedly. Firmware updates wipe /service, but /data/rc.local
# (run by Venus OS at every boot) re-creates the links.

set -e

DRIVERS=/data/shared_drivers
SERVICES=/data/etc/init.d
RCLOCAL=/data/rc.local

[ -d "$DRIVERS" ] || { echo "$DRIVERS missing - copy the files first"; exit 1; }
chmod +x "$DRIVERS"/*.py "$DRIVERS"/*.sh "$DRIVERS"/diagnostics/*.py

if [ ! -f "$DRIVERS/config.ini" ]; then
    cp "$DRIVERS/config.ini.example" "$DRIVERS/config.ini"
    echo "Created $DRIVERS/config.ini - edit it (MAC addresses!) and run this again."
    exit 0
fi

FORCE=0
if [ "$1" = "--force" ]; then FORCE=1; shift; fi

if [ $# -eq 0 ]; then
    echo "Usage: $0 [--force] <service> [<service> ...]"
    echo "Available: $(ls "$SERVICES" | tr '\n' ' ')"
    exit 1
fi

echo "Running pre-flight diagnostics..."
if ! python3 "$DRIVERS/diagnostics/test_venus_compatibility.py"; then
    if [ $FORCE -eq 0 ]; then
        echo
        echo "Diagnostics reported FAIL - fix that first, or re-run with --force."
        exit 1
    fi
    echo "Continuing anyway (--force)."
fi
echo

# The settings page needs a password before it can change anything
for name in "$@"; do
    if [ "$name" = web_ui ] && ! grep -q '^\[web\]' "$DRIVERS/config.ini"; then
        PW=$(tr -dc 'a-km-np-zA-HJ-NP-Z2-9' < /dev/urandom | head -c 10)
        printf '\n[web]\npassword = %s\nport = 8088\nopen_firewall = yes\n' "$PW" >> "$DRIVERS/config.ini"
        echo "Settings page password: $PW   (saved in config.ini, section [web])"
        NEWPW=1
    fi
done

[ -f "$RCLOCAL" ] || printf '#!/bin/sh\n' > "$RCLOCAL"
chmod +x "$RCLOCAL"

for name in "$@"; do
    dir="$SERVICES/$name"
    [ -f "$dir/run" ] || { echo "No service $dir, skipping"; continue; }
    chmod +x "$dir/run" "$dir/log/run"

    line="[ -e /service/$name ] || ln -s $dir /service/$name"
    grep -qxF "$line" "$RCLOCAL" || echo "$line" >> "$RCLOCAL"

    if [ -e "/service/$name" ]; then
        svc -t "/service/$name"
        echo "Restarted $name"
    else
        ln -s "$dir" "/service/$name"
        echo "Enabled $name (svscan starts it within ~5 s)"
    fi
done

if [ "$NEWPW" = 1 ] || grep -q web_ui "$RCLOCAL"; then
    IP=$(ip -4 addr show 2>/dev/null | sed -n 's/.*inet \(192\.168\.[0-9.]*\|10\.[0-9.]*\).*/\1/p' | head -n 1)
    echo
    echo "Settings page: http://${IP:-<pi-ip>}:8088"
fi

echo
echo "Logs:   tail -F /var/log/<name>/current | tai64nlocal"
echo "Check:  python3 $DRIVERS/diagnostics/test_venus_compatibility.py --scan 30"
