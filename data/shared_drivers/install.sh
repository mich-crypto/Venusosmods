#!/bin/sh
# Install / re-enable the custom drivers on Venus OS.
#
# Normally started by get.sh (see README). By hand, on the GX/Raspberry Pi:
#   sh /data/shared_drivers/install.sh                 # the settings page only (the default)
#   sh /data/shared_drivers/install.sh fridge web_ui   # or name the services to enable
#
# The gas sensor, fridge, water tank and Starlink are added from the settings page, which
# enables their services itself. Arguments: services to enable (folders in /data/etc/init.d).
#   --force   enable even if the pre-flight diagnostics report a FAIL
# Safe to run repeatedly. Firmware updates wipe /service, but /data/rc.local
# (run by Venus OS at every boot) re-creates the links.

set -e

DATA=${VENUS_DATA:-/data}                  # the overrides are for the tests
SVC=${VENUS_SERVICE_DIR:-/service}
DRIVERS=$DATA/shared_drivers
SERVICES=$DATA/etc/init.d
RCLOCAL=$DATA/rc.local

[ -d "$DRIVERS" ] || { echo "$DRIVERS missing - copy the files first"; exit 1; }
chmod +x "$DRIVERS"/*.py "$DRIVERS"/*.sh "$DRIVERS"/diagnostics/*.py

if [ ! -f "$DRIVERS/config.ini" ]; then
    printf '# Settings of the Venus OS mods. The settings page edits this file; config.ini.example lists every option.\n' > "$DRIVERS/config.ini"
fi

FORCE=0
if [ "$1" = "--force" ]; then FORCE=1; shift; fi

[ $# -gt 0 ] || set -- web_ui                        # the settings page: it adds everything else

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
        PW=$(tr -dc 'a-km-np-zA-HJ-NP-Z2-9' < /dev/urandom | fold -w 10 | head -n 1)
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

    line="[ -e $SVC/$name ] || ln -s $dir $SVC/$name"
    grep -qxF "$line" "$RCLOCAL" || echo "$line" >> "$RCLOCAL"

    if [ -e "$SVC/$name" ]; then
        svc -t "$SVC/$name"
        echo "Restarted $name"
    else
        ln -s "$dir" "$SVC/$name"
        echo "Enabled $name (svscan starts it within ~5 s)"
    fi
done

if grep -q web_ui "$RCLOCAL"; then
    ADDRS=$(ip -4 addr show 2>/dev/null | sed -n 's/.*inet \(192\.168\.[0-9.]*\|10\.[0-9.]*\|172\.\(1[6-9]\|2[0-9]\|3[01]\)\.[0-9.]*\|100\.[0-9.]*\)[/ ].*/\1/p')
    PORT=$(sed -n '/^\[web\]/,/^\[/s/^port *= *\([0-9]*\).*/\1/p' "$DRIVERS/config.ini" | head -n 1)
    echo
    echo "=============================================================="
    echo " Done. Open the settings page on your phone or computer:"
    for a in $ADDRS; do echo "     http://$a:${PORT:-8088}"; done
    [ -n "$ADDRS" ] || echo "     http://<ip of this device>:${PORT:-8088}"
    if [ -n "$PW" ]; then
        echo " Password: $PW   (any user name; it is saved in config.ini)"
    else
        echo " Password: see [web] in $DRIVERS/config.ini"
    fi
    echo " Press 'Unlock', then follow the setup guide to add your devices."
    echo "=============================================================="
fi

echo
echo "Logs:   tail -F /var/log/<name>/current | tai64nlocal"
echo "Check:  python3 $DRIVERS/diagnostics/test_venus_compatibility.py --scan 30"
