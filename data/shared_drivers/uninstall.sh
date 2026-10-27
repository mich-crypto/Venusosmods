#!/bin/sh
# Stop and remove the given services (default: all). Files in /data stay.
SERVICES=/data/etc/init.d
RCLOCAL=/data/rc.local

[ $# -gt 0 ] || set -- $(ls "$SERVICES")

for name in "$@"; do
    if [ -e "/service/$name" ]; then
        svc -d "/service/$name" "/service/$name/log" 2>/dev/null
        rm "/service/$name"
        echo "Stopped $name"
    fi
    [ -f "$RCLOCAL" ] && sed -i "\|ln -s $SERVICES/$name /service/$name|d" "$RCLOCAL"
done
