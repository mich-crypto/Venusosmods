"""
Which Bluetooth device is which: used by the setup guide to turn what BlueZ has heard into
"gas sensor", "water sender" and "fridge". Pure Python (no D-Bus).
"""

import re

import alpicool_protocol
import esp32_hydro_protocol
import mopeka_std_parser

MAC_RE = re.compile(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$')


def normalize_mac(text):
    mac = str(text or '').strip().upper().replace('-', ':')
    return mac if MAC_RE.match(mac) else None


def classify(name, uuids, mfr):
    """
    name: advertised name, uuids: list of lower-case service UUIDs, mfr: {company id: payload bytes}.
    -> dict(kind='gas'|'water'|'fridge', label, ...details) or None for anything else.
    """
    m = mopeka_std_parser.parse(mfr.get(mopeka_std_parser.MANUFACTURER_ID))
    if m:
        return {'kind': 'gas', 'label': 'Mopeka gas sensor', 'sync': bool(m.sync_pressed),
                'distance_mm': None if m.distance_mm is None else round(m.distance_mm),
                'battery_v': m.battery_voltage}
    h = esp32_hydro_protocol.parse(mfr.get(esp32_hydro_protocol.COMPANY_ID))
    if h:
        return {'kind': 'water', 'label': 'Water tank sender', 'device_id': h.device_id,
                'height_mm': h.height_mm, 'level': round(h.level, 1)}
    if str(name or '').startswith(alpicool_protocol.NAME_PREFIXES) or alpicool_protocol.SERVICE_UUID in (uuids or []):
        return {'kind': 'fridge', 'label': 'Fridge', 'name': name}
    return None


def bars(rssi):
    """0-4 signal bars from an RSSI in dBm."""
    if rssi is None:
        return 0
    return 4 if rssi >= -55 else 3 if rssi >= -67 else 2 if rssi >= -78 else 1 if rssi >= -88 else 0
