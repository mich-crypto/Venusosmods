"""
Everything the settings web page needs to read, check and write the INI files.
Pure Python, no D-Bus and no web code: unit-testable on any PC.

Two kinds of files:

 1. our drivers' config.ini      one [section] per sensor, schema below
 2. dbus-serialbattery           config.default.ini (all keys, documented in
                                 comments) + config.ini (only the keys you
                                 changed, under [DEFAULT])

Edits are line based, so comments and unknown keys in a file survive.
"""

import configparser
import os
import re

KEY_RE = re.compile(r'^(\s*)([A-Za-z0-9_.\-]+)(\s*[=:]\s*)(.*?)\s*$')
SECTION_RE = re.compile(r'^\s*\[([^\]]+)\]\s*$')
COMMENT_RE = re.compile(r'^\s*[;#]')
MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
INT_RE = re.compile(r'^[+-]?\d+$')
FLOAT_RE = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')
TRUE = ('1', 'yes', 'true', 'on')
FALSE = ('0', 'no', 'false', 'off')


# ------------------------------------------------------------ line editing

def _section_bounds(lines, section):
    """(first line after the header, end line) of a section, or None."""
    start = None
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m:
            if start is not None:
                return start, i
            if m.group(1).strip().lower() == section.lower():
                start = i + 1
    return (start, len(lines)) if start is not None else None


def parse_ini(text):
    """{section: {key: value}} (keys lowercase, last one wins). Comments ignored."""
    result, current = {}, None
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            current = result.setdefault(m.group(1).strip(), {})
            continue
        if current is None or COMMENT_RE.match(line) or not line.strip():
            continue
        m = KEY_RE.match(line)
        if m and not line[:1].isspace():
            current[m.group(2).lower()] = m.group(4)
    return result


def set_values(text, section, values):
    """
    Change keys in one section, keeping everything else. values: key -> new
    value, or None to remove the key. Missing keys are added at the end of
    the section; a missing section is appended.
    """
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append('')
        lines.append('[%s]' % section)
        bounds = (len(lines), len(lines))
    for key, value in values.items():
        start, end = _section_bounds(lines, section)
        found = None
        for i in range(start, end):
            m = KEY_RE.match(lines[i])
            if m and not COMMENT_RE.match(lines[i]) and not lines[i][:1].isspace() \
                    and m.group(2).lower() == key.lower():
                found = i
        if found is not None:
            # drop continuation lines of a multi-line value as well
            last = found
            while last + 1 < end and lines[last + 1][:1] in (' ', '\t') \
                    and lines[last + 1].strip() and not COMMENT_RE.match(lines[last + 1]):
                last += 1
            if value is None:
                del lines[found:last + 1]
            else:
                m = KEY_RE.match(lines[found])
                lines[found:last + 1] = ['%s%s%s%s' % (m.group(1), m.group(2), m.group(3), value)]
        elif value is not None:
            insert = end
            while insert > start and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, '%s = %s' % (key, value))
    return '\n'.join(lines) + '\n'


def check_syntax(text):
    """Error text if configparser (what the drivers use) cannot read it, else ''."""
    cp = configparser.ConfigParser()
    try:
        cp.read_string(text)
        for section in [cp.default_section] + cp.sections():
            for key, value in cp.items(section):      # also catches '%' interpolation errors
                pass
    except configparser.Error as e:
        return str(e).replace('\n', ' ')
    return ''


PROTECTED_FILES = ('config.default.ini',)


def write_file(path, text):
    """Atomic write; the previous version is kept as <path>.bak."""
    if os.path.basename(path) in PROTECTED_FILES:
        # dbus-serialbattery's own defaults: read only, all changes go into config.ini
        raise PermissionError('%s is never written, change config.ini instead' % path)
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
        with open(path + '.bak', 'w') as f:
            f.write(old)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------- our driver schema

def F(key, type, label, help='', **kw):
    d = {'key': key, 'type': type, 'label': label, 'help': help}
    d.update(kw)
    return d


YES_NO = [['yes', 'Yes'], ['no', 'No']]
TEMP_TYPES = [['generic', 'Generic'], ['fridge', 'Fridge'], ['freezer', 'Freezer'],
              ['room', 'Room'], ['outdoor', 'Outdoor'], ['battery', 'Battery'],
              ['waterheater', 'Water heater']]
FLUID_TYPES = [['0', 'Fuel'], ['1', 'Fresh water'], ['2', 'Waste water'], ['5', 'Black water'],
               ['8', 'LPG'], ['11', 'Raw water']]

COMMON = [
    F('device_instance', 'int', 'VRM device instance', 'Preferred instance number in Venus OS and '
      'VRM; a free one is chosen if it is taken.', min=0, max=999, advanced=True),
    F('adapter', 'text', 'Bluetooth adapter', 'hci0 is the built-in one; hci1 for a USB adapter.',
      advanced=True),
]

SCHEMA = {
    'mopeka': {
        'title': 'Gas bottle (Mopeka)',
        'fields': [
            F('custom_name', 'text', 'Name', 'Shown in Venus OS and VRM.'),
            F('mac', 'mac', 'Sensor address',
              'Leave empty and press the sync button on the sensor once to pair.'),
            F('capacity_l', 'float', 'Usable gas volume', 'Litres of liquid at 100 %. Only used for '
              '"remaining". 11 kg propane bottle = 27, 5 kg = 12.', unit='L', min=0, max=1000),
            F('propane_ratio', 'float', 'Propane share', '1.0 = pure propane, about 0.5 for a '
              'propane-butane mix. A wrong value shifts the reading a few percent.',
              min=0, max=1, step=0.05),
            F('empty_mm', 'float', 'Reading when empty', 'What the sensor measures (mm) with an '
              'empty bottle. Use "Use current reading" below.', unit='mm', min=0, max=2000),
            F('full_mm', 'float', 'Reading when full', 'What it measures right after a refill (mm).',
              unit='mm', min=1, max=2000),
            F('level_curve', 'text', 'Bottle shape curve', 'fraction:percent pairs, e.g. '
              '"0:0, 0.1:4.6, 1:100". For bottles with a rounded bottom. Empty = straight line.',
              advanced=True),
            F('level_table', 'text', 'Distance table', 'distance_mm:percent pairs. Overrides '
              'empty/full and the curve.', advanced=True),
            F('temperature_service', 'bool', 'Publish temperature',
              'The sensor\'s temperature as its own temperature sensor with the environment sensors. '
              'The sensor sits where it feels the outside air, so it can serve as the outside temperature.',
              options=YES_NO, default='yes'),
            F('temperature_name', 'text', 'Temperature name', '', default='Gas bottle temperature'),
            F('temperature_type', 'select', 'Temperature type', 'Outdoor = outside air (the default here). '
              'Use Generic if the sensor mostly measures the bottle.', options=TEMP_TYPES, default='outdoor'),
            F('temperature_instance', 'int', 'Temperature VRM instance', '', min=0, max=999,
              advanced=True, default='22'),
            F('median_window', 'int', 'Smoothing', 'Median over this many readings.',
              min=1, max=31, advanced=True),
            F('timeout', 'int', 'Disconnected after', 'Seconds without a reading.', unit='s',
              min=30, max=86400, advanced=True),
            F('fluid_type', 'select', 'Fluid type', '', options=FLUID_TYPES, advanced=True),
            F('service', 'text', 'Service name', 'D-Bus name; changing it creates a new device.',
              readonly=True, advanced=True),
        ] + COMMON,
        'presets': [
            {'label': 'CAMPKO 30 L (24 L usable)',
             'values': {'capacity_l': '24', 'empty_mm': '0',
                        'level_curve': '0:0, 0.05:1.3, 0.10:4.6, 0.15:9.2, 0.194:13.9, 1:100'}},
            {'label': 'Propane 11 kg (27 L), straight cylinder',
             'values': {'capacity_l': '27', 'level_curve': ''}},
            {'label': 'Propane 5 kg (12 L), straight cylinder',
             'values': {'capacity_l': '12', 'level_curve': ''}},
        ],
    },
    'esp32': {
        'title': 'Water tank (ESP32)',
        'fields': [
            F('custom_name', 'text', 'Name', 'Shown in Venus OS and VRM.'),
            F('device_id', 'int', 'Sender ID', 'Must match DEVICE_ID in the ESP32 firmware.',
              min=0, max=255),
            F('mac', 'mac', 'Sender address', 'Optional; the ID is enough.'),
            F('temperature_service', 'bool', 'Publish water temperature',
              'As its own temperature sensor (needs the DS18B20 probe).', options=YES_NO, default='no'),
            F('temperature_name', 'text', 'Temperature name', '', default='Water temperature'),
            F('temperature_type', 'select', 'Temperature type', '', options=TEMP_TYPES, default='generic'),
            F('timeout', 'int', 'Disconnected after', 'Seconds without data.', unit='s',
              min=10, max=86400, advanced=True),
            F('temperature_instance', 'int', 'Temperature VRM instance', '', min=0, max=999,
              advanced=True, default='20'),
            F('level_table', 'text', 'Calibration table', 'height_mm:litres pairs made by the Calibrate button. Replaces the '
              'sender\'s own table; clear it to go back to that one.', advanced=True),
            F('capacity_l', 'float', 'Tank size', 'Litres; set by the Calibrate button.', unit='L', min=0, max=100000, advanced=True),
            F('fluid_type', 'select', 'Fluid type', '', options=FLUID_TYPES, advanced=True),
            F('service', 'text', 'Service name', 'D-Bus name; changing it creates a new device.',
              readonly=True, advanced=True),
        ] + COMMON,
        'presets': [],
    },
    'fridge': {
        'title': 'Fridge (Bluetooth)',
        'fields': [
            F('mac', 'mac', 'Fridge address', 'Required.', required=True),
            F('poll_interval', 'int', 'Read every', 'Seconds between status reads. Each read '
              'connects for a few seconds and blocks the phone app meanwhile. 300 is plenty for '
              'a fridge.', unit='s', min=10, max=3600, presets=[60, 120, 300, 600, 900]),
            F('control', 'bool', 'Control from Venus OS', 'Power, eco mode and set points on the '
              'switch pane.', options=YES_NO),
            F('zone1_type', 'select', 'Zone 1 type', 'auto = freezer if the set point is below 0 C.',
              options=[['auto', 'Automatic']] + TEMP_TYPES, advanced=True),
            F('zone2_type', 'select', 'Zone 2 type', '', options=[['auto', 'Automatic']] + TEMP_TYPES, advanced=True),
            F('stay_connected', 'bool', 'Stay connected', 'Faster updates, but the phone app cannot '
              'connect.', options=YES_NO, advanced=True),
            F('control_group', 'text', 'Switch pane card name', '', advanced=True),
            F('timeout', 'int', 'Disconnected after', 'Seconds without a reading.', unit='s',
              min=30, max=86400, advanced=True),
            F('service', 'text', 'Service name', 'D-Bus name; changing it creates a new device.',
              readonly=True, advanced=True),
        ] + COMMON,
        'presets': [],
    },
    'starlink': {
        'title': 'Starlink',
        'fields': [
            F('enabled', 'bool', 'Read the dish', 'Shows the Starlink tabs. Turn off if you have no dish.',
              options=YES_NO, default='yes'),
            F('host', 'text', 'Dish address', 'Usually 192.168.100.1; the Pi must be able to reach it.',
              default='192.168.100.1'),
            F('speedtest_interval', 'int', 'Speed test every', '0 = only when you press the button. Each test moves about '
              '33 MB, so 30 minutes is roughly 1.6 GB a day.', unit='min', min=0, max=1440, default='0',
              presets=[0, 30, 60, 360, 1440]),
            F('icmp_targets', 'text', 'Ping targets', 'Name=address pairs separated by commas, up to 8. Default: '
              'Cloudflare=1.1.1.1, Google=8.8.8.8, Quad9=9.9.9.9, Dish=192.168.100.1. Add the Starlink gateway as '
              'Gateway=100.64.0.1.', default=''),
            F('cycle_day', 'int', 'Billing cycle starts on day', 'For the data-use totals.', min=1, max=28, default='1'),
            F('cap_gb', 'float', 'Data cap', '0 = no cap bar.', unit='GB', min=0, max=100000, default='0'),
            F('price_per_kwh', 'float', 'Electricity price per kWh', '0 hides the cost.', min=0, max=100, default='0'),
            F('currency', 'text', 'Currency label', '', default='EUR'),
            F('wan_check', 'bool', 'Record the public IP address', 'Checks every 5 minutes through Cloudflare.',
              options=YES_NO, default='yes'),
            F('venus_service', 'bool', 'Show the dish in Venus OS', 'A "Starlink connection" device (a level of 0-100 % for the '
              'quality of the connection) in the Device list, on the Levels page and in VRM.', options=YES_NO, default='yes'),
            F('interval', 'int', 'Read the dish every', 'Seconds between live readings.', unit='s', min=2, max=300,
              advanced=True, default='5'),
            F('history_interval', 'int', 'Read the dish history every', 'Energy, data use and outages come from it.',
              unit='s', min=10, max=600, advanced=True, default='60'),
            F('icmp_interval', 'int', 'Ping every', '', unit='s', min=5, max=3600, advanced=True, default='30'),
            F('icmp_good_ms', 'int', 'Ping is good up to', '', unit='ms', min=1, max=1000, advanced=True, default='40'),
            F('icmp_warn_ms', 'int', 'Ping is slow up to', '', unit='ms', min=1, max=5000, advanced=True, default='100'),
            F('speedtest_down_mb', 'int', 'Download test size', '', unit='MB', min=1, max=200, advanced=True, default='25'),
            F('speedtest_up_mb', 'int', 'Upload test size', '', unit='MB', min=1, max=100, advanced=True, default='8'),
            F('min_sane_mbps', 'float', 'Retry below', 'A speed test under this is run once more.', unit='Mbps', min=0,
              max=1000, advanced=True, default='5'),
            F('port', 'int', 'gRPC-web port', 'Normally 9201.', min=1, max=65535, advanced=True, default='9201'),
        ],
        'presets': [],
    },
    'telegram': {
        'title': 'Notifications (Telegram)',
        'fields': [
            F('enabled', 'bool', 'Send notifications', 'Create a bot with @BotFather, send it a message, then enter its token '
              'and your chat ID.', options=YES_NO, default='no'),
            F('token', 'secret', 'Bot token', ''),
            F('chat_id', 'text', 'Chat ID', ''),
            F('alert_battery', 'bool', 'Battery low', 'A warning and an alarm for the lithium battery.', options=YES_NO, default='yes'),
            F('battery_warn_pct', 'int', 'Battery warning at', '', unit='%', min=2, max=99, default='20'),
            F('battery_alarm_pct', 'int', 'Battery alarm at', '', unit='%', min=1, max=98, default='10'),
            F('alert_alarms', 'bool', 'Any alarm', 'Every alarm or warning that any Victron device shows.', options=YES_NO, default='yes'),
            F('alert_sensor', 'bool', 'Gas sensor battery low', '', options=YES_NO, default='yes'),
            F('sensor_pct', 'int', 'Gas sensor battery alert at', '', unit='%', min=1, max=100, default='20'),
            F('alert_geofence', 'bool', 'Leaves the parking spot', 'Needs the GPS. Set the spot with "Use the current position".',
              options=YES_NO, default='no'),
            F('fence_m', 'int', 'Distance', '', unit='m', min=20, max=100000, default='200'),
            F('fence_lat', 'float', 'Spot latitude', '', min=-90, max=90, advanced=True),
            F('fence_lon', 'float', 'Spot longitude', '', min=-180, max=180, advanced=True),
            F('alert_orion', 'bool', 'Orion XS charging switched on', '', options=YES_NO, default='yes'),
            F('alert_freeze', 'bool', 'Frost forecast', 'Frost expected at the camper within 24 hours. Needs the GPS or a parking spot, and the internet.',
              options=YES_NO, default='yes'),
            F('alert_cold', 'bool', 'Below 0 °C for 2 hours', 'Uses a temperature sensor set to outdoor (the gas sensor by default), else the forecast service.',
              options=YES_NO, default='yes'),
            F('alert_offline', 'bool', 'Starlink offline and back', '', options=YES_NO, default='yes'),
            F('alert_drop', 'bool', 'Starlink packet loss', '', options=YES_NO, default='yes'),
            F('alert_hardware', 'bool', 'Starlink hardware alerts', 'Thermal throttle, motors stuck, water detected ...',
              options=YES_NO, default='yes'),
            F('alert_wan', 'bool', 'Public IP changed', '', options=YES_NO, default='yes'),
            F('alert_test', 'bool', 'Speed test failed', '', options=YES_NO, default='yes'),
            F('offline_min', 'int', 'Offline after', 'Minutes without an answer from the dish.', unit='min', min=1,
              max=1440, advanced=True, default='5'),
            F('drop_pct', 'float', 'Packet loss alert at', '', unit='%', min=0.1, max=100, advanced=True, default='5'),
        ],
        'presets': [],
    },
    'web': {
        'title': 'This settings page',
        'fields': [
            F('password', 'secret', 'Password', 'Needed to change anything. Empty = the page is '
              'read-only.'),
            F('port', 'int', 'Port', 'Applied after a restart of the page.', min=1024, max=65535),
            F('bind', 'text', 'Listen on', '0.0.0.0 = all networks, 127.0.0.1 = this device only.',
              advanced=True),
            F('open_firewall', 'bool', 'Open the port in the firewall',
              'For the local network only (192.168/16, 10/8, 172.16/12).', options=YES_NO,
              advanced=True),
        ],
        'presets': [],
    },
}


def kind_of(section, values=None):
    """Which driver a config section belongs to, by its `driver =` key or its name."""
    name = ((values or {}).get('driver') or section).lower()
    for prefix in ('mopeka', 'esp32', 'fridge', 'starlink', 'telegram', 'web'):
        if name.startswith(prefix):
            return prefix
    return None


def _norm_bool(text):
    t = text.strip().lower()
    if t in TRUE:
        return 'yes'
    if t in FALSE:
        return 'no'
    return None


def validate_field(field, text):
    """(normalized text, error or None)."""
    text = '' if text is None else str(text).strip()
    if '\n' in text or '\r' in text:
        return text, 'must be a single line'
    if '%' in text:
        return text, 'the % sign is not allowed'
    t = field['type']
    if text == '':
        return ('', 'is required') if field.get('required') else ('', None)
    if t == 'bool':
        v = _norm_bool(text)
        return (v, None) if v else (text, 'must be yes or no')
    if t == 'int':
        if not INT_RE.match(text):
            return text, 'must be a whole number'
        n = int(text)
    elif t == 'float':
        if not FLOAT_RE.match(text):
            return text, 'must be a number'
        n = float(text)
    elif t == 'mac':
        if not MAC_RE.match(text):
            return text, 'must look like AA:BB:CC:DD:EE:FF'
        return text.upper(), None
    elif t == 'select':
        allowed = [o[0] for o in field.get('options', [])]
        if text not in allowed:
            return text, 'must be one of: ' + ', '.join(allowed)
        return text, None
    else:
        return text, None
    if 'min' in field and n < field['min']:
        return text, 'must be at least %s' % field['min']
    if 'max' in field and n > field['max']:
        return text, 'must be at most %s' % field['max']
    return text, None


def _table_error(text):
    """Error for 'x:y, x:y' tables (level_curve / level_table)."""
    pts = []
    try:
        for item in text.replace(';', ',').split(','):
            if item.strip():
                x, y = item.split(':')
                pts.append((float(x), float(y)))
    except ValueError:
        return 'must be pairs like "0:0, 0.5:40, 1:100"'
    if len(pts) < 2:
        return 'needs at least two pairs'
    return None


def validate_section(kind, values):
    """
    values: key -> text from the form. Returns (clean, errors); clean only
    holds fields that are part of the schema and writable.
    """
    fields = dict((f['key'], f) for f in SCHEMA[kind]['fields'])
    clean, errors = {}, {}
    for key, text in values.items():
        f = fields.get(key)
        if f is None or f.get('readonly'):
            continue
        norm, err = validate_field(f, text)
        if not err and key in ('level_curve', 'level_table') and norm:
            err = _table_error(norm)
        if err:
            errors[key] = '%s %s' % (f['label'], err)
        else:
            clean[key] = norm
    if kind == 'mopeka':
        lo = clean.get('empty_mm')
        hi = clean.get('full_mm')
        if lo and hi and 'empty_mm' not in errors and 'full_mm' not in errors \
                and float(hi) <= float(lo):
            errors['full_mm'] = 'Reading when full must be larger than reading when empty'
    return clean, errors


def read_driver_config(text):
    """[{section, kind, title, values, fields, presets}] for every known section."""
    out = []
    for section, values in parse_ini(text).items():
        kind = kind_of(section, values)
        if kind is None:
            continue
        schema = SCHEMA[kind]
        out.append({'section': section, 'kind': kind, 'title': schema['title'],
                    'values': dict((f['key'], values.get(f['key'], '')) for f in schema['fields']),
                    'fields': schema['fields'], 'presets': schema.get('presets', [])})
    return out


# ------------------------------------------------------ dbus-serialbattery

GROUP_RE = re.compile(r'^;\s*(-{5,})\s*(.+?)\s*-{5,}\s*$|^;\s*-{3}\s+(\S.*?)\s*$')
SUBHEAD_RE = re.compile(r'^;\s*-{4}\s*(.+?)\s*-{4}\s*$')

ENUMS = {'LOGGING': ['ERROR', 'WARNING', 'INFO', 'DEBUG']}
NUMERIC_EMPTY = {'SOC_RESET_AFTER_DAYS': 'int', 'POLL_INTERVAL': 'float'}
# keys whose lists belong together and must have the same number of entries
PAIRED_LISTS = [
    ('CELL_VOLTAGES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_CV_FRACTION'),
    ('CELL_VOLTAGES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_CV_FRACTION'),
    ('TEMPERATURES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_T_FRACTION'),
    ('TEMPERATURES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_T_FRACTION'),
    ('MOSFET_TEMPERATURES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_T_MOSFET_FRACTION'),
    ('MOSFET_TEMPERATURES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_T_MOSFET_FRACTION'),
    ('SOC_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_SOC_FRACTION'),
    ('SOC_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_SOC_FRACTION'),
]
# shown first, in this order (only those that exist in the file)
ESSENTIALS = ['BLUETOOTH_BMS', 'BMS_TYPE', 'MAX_BATTERY_CHARGE_CURRENT',
              'MAX_BATTERY_DISCHARGE_CURRENT', 'MIN_CELL_VOLTAGE', 'MAX_CELL_VOLTAGE',
              'FLOAT_CELL_VOLTAGE', 'SOC_CALCULATION', 'SOC_RESET_CELL_VOLTAGE',
              'SOC_RESET_AFTER_DAYS', 'BATTERY_CAPACITY', 'USE_BMS_DVCC_VALUES', 'LOGGING']
# a change in these needs a reboot, a restart of the driver is not enough
# Only switching between the Pi's own Bluetooth and a USB stick needs a reboot: enable.sh changes
# the boot configuration (/u-boot/config.txt) for it.
REBOOT_KEYS = ('BLUETOOTH_USE_USB',)
# Bluetooth BMS and CAN ports become services of their own (dbus-blebattery.N, dbus-canbattery.X).
# The driver's enable.sh builds them from config.ini, then restart.sh starts them: no reboot needed.
SETUP_KEYS = ('BLUETOOTH_BMS', 'CAN_PORT')


def guess_type(key, default):
    if key in ENUMS:
        return 'select'
    if default in ('True', 'False'):
        return 'bool'
    if default == '':
        return NUMERIC_EMPTY.get(key, 'text')
    if INT_RE.match(default):
        return 'int'
    if FLOAT_RE.match(default):
        return 'float'
    if ',' in default and all(INT_RE.match(p.strip()) or FLOAT_RE.match(p.strip())
                              for p in default.split(',')):
        return 'numlist'
    return 'text'


def humanize(key):
    return key.replace('_', ' ').capitalize()


def parse_default_ini(text):
    """
    The documented defaults file -> [{'title', 'entries': [{key, default, type,
    help, options?, secret?, special?}]}] in file order. Comment lines above a
    key become its help text.
    """
    groups, group, pending = [], None, []

    def new_group(title):
        nonlocal group, pending
        group = {'title': title, 'entries': []}
        groups.append(group)
        pending = []

    for line in text.splitlines():
        m = GROUP_RE.match(line)
        if m:
            new_group(m.group(2) or m.group(3))
            continue
        if SUBHEAD_RE.match(line):
            pending.append(SUBHEAD_RE.match(line).group(1) + ':')
            continue
        if COMMENT_RE.match(line):
            pending.append(re.sub(r'^\s*[;#] ?', '', line).rstrip())
            continue
        m = KEY_RE.match(line)
        if m and not line[:1].isspace() and m.group(2).upper() == m.group(2):
            if group is None:
                new_group('Settings')
            key, default = m.group(2), m.group(4)
            while pending and not pending[-1].strip():
                pending.pop()
            entry = {'key': key, 'label': humanize(key), 'default': default,
                     'type': guess_type(key, default), 'help': '\n'.join(pending).strip('\n')}
            if key in ENUMS:
                entry['options'] = ENUMS[key]
            if 'PASSWORD' in key:
                entry['secret'] = True
            if key == 'BLUETOOTH_BMS':
                entry['special'] = 'bms_list'
            group['entries'].append(entry)
            pending = []
        elif not line.strip():
            if pending and pending[-1].strip():
                pending.append('')          # keep paragraph breaks inside a help block
    return [g for g in groups if g['entries']]


def _norm_list(text):
    return ', '.join(p.strip() for p in text.split(',') if p.strip() != '' or text.strip() == '')


def normalize_value(entry, text):
    """(normalized text, error)."""
    text = '' if text is None else str(text).strip()
    if '\n' in text or '\r' in text:
        return text, 'must be a single line'
    if '%' in text:
        return text, 'the % sign is not allowed'
    t = entry['type']
    if text == '':
        return '', None
    if t == 'bool':
        v = text.lower()
        if v in ('true', 'false'):
            return v.capitalize(), None
        return text, 'must be True or False'
    if t == 'int':
        return (text, None) if INT_RE.match(text) else (text, 'must be a whole number')
    if t == 'float':
        return (text, None) if FLOAT_RE.match(text) else (text, 'must be a number')
    if t == 'numlist':
        parts = [p.strip() for p in text.split(',')]
        if not all(INT_RE.match(p) or FLOAT_RE.match(p) for p in parts):
            return text, 'must be numbers separated by commas'
        return ', '.join(parts), None
    if t == 'select':
        up = text.upper()
        if up in entry.get('options', []):
            return up, None
        return text, 'must be one of: ' + ', '.join(entry.get('options', []))
    if entry.get('key') == 'BLUETOOTH_BMS':
        for item in text.split(','):
            bits = item.split()
            if len(bits) != 2 or not MAC_RE.match(bits[1]):
                return text, 'use "Type AA:BB:CC:DD:EE:FF", several separated by commas'
    return text, None


def same_value(entry, a, b):
    """Are two texts the same setting (3.450 == 3.45, true == True)?"""
    a, b = a.strip(), b.strip()
    t = entry['type']
    try:
        if t in ('int', 'float'):
            return float(a) == float(b)
        if t == 'numlist':
            return [float(p) for p in a.split(',')] == [float(p) for p in b.split(',')]
    except ValueError:
        pass
    if t == 'bool':
        return a.lower() == b.lower()
    if t in ('select',):
        return a.upper() == b.upper()
    return a == b


def serialbattery_view(default_text, user_text):
    """
    What the page shows: groups with, per key, the default and the user's
    override ('' override key absent -> None).
    """
    over = parse_ini(user_text).get('DEFAULT', {})
    over_ci = dict((k.lower(), v) for k, v in over.items())
    groups = parse_default_ini(default_text)
    known = set()
    for g in groups:
        for e in g['entries']:
            known.add(e['key'].lower())
            e['value'] = over_ci.get(e['key'].lower())     # None = using the default
    # a first group with the most used settings
    by_key = dict((e['key'], e) for g in groups for e in g['entries'])
    essentials = [by_key[k] for k in ESSENTIALS if k in by_key]
    unknown = dict((k, v) for k, v in over.items() if k.lower() not in known)
    return {'essentials': [e['key'] for e in essentials], 'groups': groups, 'unknown': unknown,
            'reboot_keys': list(REBOOT_KEYS), 'setup_keys': list(SETUP_KEYS)}


def apply_serialbattery(default_text, user_text, changes):
    """
    changes: key -> text, or None to go back to the default. Returns
    (new user config text, errors). Only keys that differ from the default are
    written to the file.
    """
    entries = dict((e['key'], e) for g in parse_default_ini(default_text) for e in g['entries'])
    errors, edits = {}, {}
    current = dict((k.upper(), v) for k, v in parse_ini(user_text).get('DEFAULT', {}).items())
    effective = dict((k, current.get(k, e['default'])) for k, e in entries.items())
    for key, text in changes.items():
        entry = entries.get(key)
        if entry is None:
            errors[key] = 'unknown setting'
            continue
        if text is None:
            edits[key] = None
            effective[key] = entry['default']
            continue
        norm, err = normalize_value(entry, text)
        if err:
            errors[key] = '%s %s' % (entry['label'], err)
        elif same_value(entry, norm, entry['default']):
            edits[key] = None                  # same as the default: no override needed
            effective[key] = entry['default']
        else:
            edits[key] = norm
            effective[key] = norm
    for a, b in PAIRED_LISTS:
        if a in entries and b in entries and (a in changes or b in changes):
            na = len([p for p in effective[a].split(',') if p.strip()])
            nb = len([p for p in effective[b].split(',') if p.strip()])
            if na != nb:
                errors[a if a in changes else b] = (
                    '%s has %d values but %s has %d: they must match' % (a, na, b, nb))
    if errors:
        return user_text, errors
    text = user_text if user_text.strip() else ''
    if not any(SECTION_RE.match(l) for l in text.splitlines()):
        text = '[DEFAULT]\n' + text
    # keep the user's own key spelling when it is already in the file
    spelled = dict((k.upper(), k) for k in parse_ini(text).get('DEFAULT', {}))
    text = set_values(text, 'DEFAULT', dict((spelled.get(k, k), v) for k, v in edits.items()))
    return text, {}


def find_serialbattery(candidates=('/data/apps/dbus-serialbattery', '/data/etc/dbus-serialbattery',
                                   '/data/dbus-serialbattery')):
    """Folder with config.default.ini, or None."""
    for path in candidates:
        if os.path.isfile(os.path.join(path, 'config.default.ini')):
            return path
    return None


# ------------------------------------------- battery safety (LiFePO4 cells)
#
# Limits of LiFePO4 cells as given in common cell datasheets (EVE LF280K and alike) and in the
# dbus-serialbattery documentation: charge to at most 3.65 V and only between 0 and 55 °C,
# discharge to at least 2.5 V and between -20 and 60 °C, charge current 0.5 C standard and 1 C at
# most. The driver's own limits must stay below the BMS's (OVP/UVP) so the BMS never has to cut off.
#
# Levels: 'block'  the change can damage the cells, it is refused
#         'danger' risky, saved only after the user confirms these exact findings
#         'warn'   worth knowing, saved normally

LFP = {'cell_max': 3.65, 'cell_min': 2.50, 'ovp_margin': 0.05,
       'charge_t_min': 0.0, 'charge_t_max': 55.0, 'discharge_t_min': -20.0, 'discharge_t_max': 60.0,
       'charge_c_good': 0.5, 'charge_c_max': 1.0, 'discharge_c_max': 1.0}
FACT_KEYS = ('cells', 'capacity', 'ovp', 'uvp', 'bms_current')


def serialbattery_effective(default_text, user_text, changes=None):
    """Key (upper case) -> the value the driver will use: default, overridden by config.ini and changes."""
    defaults = dict((e['key'], e['default']) for g in parse_default_ini(default_text) for e in g['entries'])
    eff = dict(defaults)
    for k, v in parse_ini(user_text).get('DEFAULT', {}).items():
        eff[k.upper()] = v
    for k, v in (changes or {}).items():
        k = k.upper()
        if v is not None:
            eff[k] = v
        elif k in defaults:                             # None = back to the default
            eff[k] = defaults[k]
        else:
            eff.pop(k, None)
    return eff


def _f(eff, key, fallback=None):
    try:
        return float(str(eff.get(key, '')).strip())
    except ValueError:
        return fallback


def _on(eff, key, fallback=True):
    v = str(eff.get(key, '')).strip().lower()
    return fallback if v == '' and key not in eff else v == 'true'


def _nums(eff, key):
    try:
        return [float(p) for p in str(eff.get(key, '')).split(',') if p.strip()]
    except ValueError:
        return []


def curve_value(xs, ys, x, step=False, return_lower=False):
    """Share of the max current at x, as the driver works it out (utils.calc_*_relationship)."""
    pts = sorted(zip(xs, ys))
    if not pts:
        return 1.0
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x < x1:
            if step:
                return y0 if not return_lower else y1
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def _curve_max(eff, xk, yk, lo, hi, step, return_lower=False):
    """Highest share of the max current anywhere between lo and hi (None: lists missing or unequal)."""
    xs, ys = _nums(eff, xk), _nums(eff, yk)
    if not xs or len(xs) != len(ys):
        return None
    n = 200
    return max(curve_value(xs, ys, lo + (hi - lo) * i / n, step, return_lower) for i in range(n + 1))


def battery_safety(eff, facts=None):
    """
    eff: key -> effective value (serialbattery_effective). facts: what the user (or the BMS on
    D-Bus) told us about the battery: cells, capacity (Ah), ovp, uvp (cell V), bms_current (A).
    Returns [{'id', 'level', 'keys', 'text'}].
    """
    facts = dict((k, v) for k, v in (facts or {}).items() if isinstance(v, (int, float)) and v > 0)
    out = []

    def add(fid, level, keys, text):
        out.append({'id': fid, 'level': level, 'keys': list(keys), 'text': text})

    has = lambda k: k in eff
    step = str(eff.get('CHARGE_MODE', '1')).strip() == '2'
    ovp = facts.get('ovp', LFP['cell_max'])
    uvp = facts.get('uvp', LFP['cell_min'])
    cap, bms_a = facts.get('capacity'), facts.get('bms_current')

    if 'ovp' not in facts or 'uvp' not in facts:
        add('facts_bms', 'warn', [], 'Enter the cell OVP and UVP of your JK BMS (Battery tab, "Your battery") so the '
            'checks compare with your BMS. Until then they use the LiFePO4 limits 3.65 V and 2.50 V.')
    if not cap:
        add('facts_capacity', 'warn', [], 'Battery capacity unknown, so the charge current cannot be checked '
            'against the cells. Enter it under "Your battery".')

    # --- currents
    cc, dc = _f(eff, 'MAX_BATTERY_CHARGE_CURRENT'), _f(eff, 'MAX_BATTERY_DISCHARGE_CURRENT')
    if cap and cc is not None:
        c = cc / cap
        if c > LFP['charge_c_max']:
            add('charge_c_max', 'block', ['MAX_BATTERY_CHARGE_CURRENT'],
                'Charge current %g A is %.2f C for %g Ah. LiFePO4 cells take at most 1 C (%g A); more heats '
                'the cells and damages them.' % (cc, c, cap, cap * LFP['charge_c_max']))
        elif c > LFP['charge_c_good']:
            add('charge_c_high', 'danger', ['MAX_BATTERY_CHARGE_CURRENT'],
                'Charge current %g A is %.2f C for %g Ah. Cells last longest at 0.5 C (%g A) or less; check '
                'the datasheet of your cells before going higher.' % (cc, c, cap, cap * LFP['charge_c_good']))
    if cap and dc is not None and dc / cap > LFP['discharge_c_max']:
        add('discharge_c_max', 'danger', ['MAX_BATTERY_DISCHARGE_CURRENT'],
            'Discharge current %g A is %.2f C for %g Ah, above the 1 C most LiFePO4 cells are rated for '
            'continuously.' % (dc, dc / cap, cap))
    for key, val, what in (('MAX_BATTERY_CHARGE_CURRENT', cc, 'charge'), ('MAX_BATTERY_DISCHARGE_CURRENT', dc, 'discharge')):
        if bms_a and val is not None and val > bms_a:
            add(key.lower() + '_bms', 'block', [key],
                'Max %s current %g A is more than the %g A your BMS is built for. The BMS would overheat '
                'or cut off; set it below the BMS rating.' % (what, val, bms_a))
        elif bms_a and val is not None and val > bms_a * 0.9:
            add(key.lower() + '_bms_close', 'warn', [key],
                'Max %s current %g A is close to the %g A of the BMS. Keep some room so the driver limits '
                'before the BMS over-current protection trips.' % (what, val, bms_a))

    # --- voltages
    mx, fl, mn = _f(eff, 'MAX_CELL_VOLTAGE'), _f(eff, 'FLOAT_CELL_VOLTAGE'), _f(eff, 'MIN_CELL_VOLTAGE')
    if mx is not None:
        if mx > LFP['cell_max']:
            add('max_cell_lfp', 'block', ['MAX_CELL_VOLTAGE'],
                'Full charge %.3f V per cell is above 3.65 V, the most a LiFePO4 cell may be charged to.' % mx)
        elif mx > ovp - LFP['ovp_margin']:
            add('max_cell_ovp', 'danger', ['MAX_CELL_VOLTAGE'],
                'Full charge %.3f V is only %d mV below the BMS cell OVP of %.3f V. One cell running ahead '
                'trips the BMS. Keep 50 to 100 mV of room; 3.45 V is the usual full charge.'
                % (mx, round((ovp - mx) * 1000), ovp))
        elif mx < 3.40:
            add('max_cell_low', 'warn', ['MAX_CELL_VOLTAGE'],
                'Full charge %.3f V is low: the battery never gets full and the balancer gets no chance '
                'at the top.' % mx)
    if fl is not None and mx is not None and fl >= mx:
        add('float_ge_max', 'block', ['FLOAT_CELL_VOLTAGE'],
            'Float %.3f V must be lower than full charge %.3f V.' % (fl, mx))
    elif fl is not None and fl > 3.40:
        add('float_high', 'warn', ['FLOAT_CELL_VOLTAGE'],
            'Float %.3f V keeps the cells nearly full all day, which ages them. 3.35 to 3.375 V is usual.' % fl)
    if mn is not None:
        if mn < LFP['cell_min']:
            add('min_cell_lfp', 'block', ['MIN_CELL_VOLTAGE'],
                'Empty %.3f V per cell is below 2.50 V. Discharging LiFePO4 that deep damages the cells.' % mn)
        elif mn <= uvp:
            add('min_cell_uvp', 'warn', ['MIN_CELL_VOLTAGE'],
                'Empty %.3f V is at or below the BMS UVP %.3f V: the BMS cuts off before the driver slows '
                'the discharge.' % (mn, uvp))

    reset_on = str(eff.get('SOC_RESET_AFTER_DAYS', '')).strip() not in ('', '0')
    rs = _f(eff, 'SOC_RESET_CELL_VOLTAGE')
    if reset_on and rs is not None:
        if rs > LFP['cell_max']:
            add('reset_lfp', 'block', ['SOC_RESET_CELL_VOLTAGE'],
                'Top balance voltage %.3f V is above 3.65 V, the most a LiFePO4 cell may be charged to.' % rs)
        elif rs > ovp - 0.02:
            add('reset_ovp', 'danger', ['SOC_RESET_CELL_VOLTAGE'],
                'Top balance voltage %.3f V is too close to the BMS OVP %.3f V: the BMS will trip on every '
                'top balance. Start at %.3f V (OVP - 0.030) and move in 0.005 V steps.' % (rs, ovp, ovp - 0.03))
        if mx is not None and rs <= mx:
            add('reset_le_max', 'warn', ['SOC_RESET_CELL_VOLTAGE'],
                'Top balance voltage %.3f V must be higher than full charge %.3f V, otherwise it does nothing.' % (rs, mx))

    drop = _f(eff, 'VOLTAGE_DROP', 0) or 0
    if drop > 0.2:
        add('voltage_drop', 'danger', ['VOLTAGE_DROP'],
            'Voltage drop %.2f V raises what the chargers are asked for by that much. If the cables do not '
            'really lose it, the cells are overcharged. Measure it at full current first.' % drop)

    # --- protections switched off
    for key, text in (
            ('CCCM_CV_ENABLE', 'Charge current is no longer lowered when one cell gets close to full. A single '
                               'high cell can then reach the BMS OVP.'),
            ('DCCM_CV_ENABLE', 'Discharge current is no longer lowered when one cell gets close to empty. A '
                               'single weak cell can then reach the BMS UVP.'),
            ('CCCM_T_ENABLE', 'Charging is no longer limited by temperature. Only the low temperature protection '
                              'of the JK BMS stops charging below 0 °C: check it is on in the JK app.')):
        if has(key) and not _on(eff, key):
            add(key.lower() + '_off', 'danger', [key], text)
    if has('DCCM_T_ENABLE') and not _on(eff, 'DCCM_T_ENABLE'):
        add('dccm_t_enable_off', 'warn', ['DCCM_T_ENABLE'], 'Discharging is no longer limited by temperature.')
    if str(eff.get('CVL_CONTROLLER_MODE', '1')).strip() == '0':
        add('cvl_off', 'danger', ['CVL_CONTROLLER_MODE'],
            'The charge voltage is no longer lowered when one cell runs ahead of the others. With MPPTs that '
            'ignore the current limit this is the only thing keeping that cell from its OVP.')
    if has('CVCM_ENABLE') and not _on(eff, 'CVCM_ENABLE'):
        add('cvcm_off', 'warn', ['CVCM_ENABLE'],
            'Without float the cells are held at the full charge voltage all day, which ages them.')

    # --- curves
    if _on(eff, 'CCCM_CV_ENABLE'):
        top = _curve_max(eff, 'CELL_VOLTAGES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_CV_FRACTION', ovp, ovp + 0.3, step)
        if top:
            add('cv_curve_ovp', 'danger', ['CELL_VOLTAGES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_CV_FRACTION'],
                'The charge curve still allows %d %% of the current at the BMS OVP (%.3f V). It should reach '
                '0 below the OVP.' % (round(top * 100), ovp))
        if reset_on and rs is not None:
            xs, ys = _nums(eff, 'CELL_VOLTAGES_WHILE_CHARGING'), _nums(eff, 'MAX_CHARGE_CURRENT_CV_FRACTION')
            if xs and len(xs) == len(ys) and curve_value(xs, ys, rs - 0.001, step) <= 0:
                add('cv_curve_reset', 'warn', ['CELL_VOLTAGES_WHILE_CHARGING'],
                    'The charge curve stops charging below the top balance voltage %.3f V, so it is never '
                    'reached. Move the last point of the curve above it.' % rs)
        # the curve has to fit the voltages: still some current at full charge, or the battery never gets full
        xs, ys = _nums(eff, 'CELL_VOLTAGES_WHILE_CHARGING'), _nums(eff, 'MAX_CHARGE_CURRENT_CV_FRACTION')
        if mx is not None and xs and len(xs) == len(ys) and curve_value(xs, ys, mx - 0.001, step) <= 0:
            stop = min(x for x, y in zip(xs, ys) if y <= 0)
            add('cv_curve_full', 'warn', ['CELL_VOLTAGES_WHILE_CHARGING', 'MAX_CELL_VOLTAGE'],
                'The charge curve stops charging at %.3f V, below the full charge voltage %.3f V. The battery never '
                'gets full and never switches to float. Move the 0 %% point of the curve above %.3f V.' % (stop, mx, mx))
    if _on(eff, 'DCCM_CV_ENABLE'):
        low = _curve_max(eff, 'CELL_VOLTAGES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_CV_FRACTION', 2.0, uvp, step, True)
        if low is not None and low > 0.01:
            add('dv_curve_uvp', 'warn', ['CELL_VOLTAGES_WHILE_DISCHARGING'],
                'The discharge curve still allows %d %% of the current at the BMS UVP (%.3f V), so the BMS cuts off '
                'before the driver stops. Let the curve reach 0 at or above the UVP.' % (round(low * 100), uvp))
    if _on(eff, 'CCCM_T_ENABLE'):
        cold = _curve_max(eff, 'TEMPERATURES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_T_FRACTION', -40, LFP['charge_t_min'], step)
        if cold:
            add('charge_cold', 'block', ['TEMPERATURES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_T_FRACTION'],
                'The temperature curve allows %d %% of the charge current at 0 °C or below. Charging LiFePO4 '
                'below 0 °C plates lithium in the cells and damages them for good.' % round(cold * 100))
        hot = _curve_max(eff, 'TEMPERATURES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_T_FRACTION', LFP['charge_t_max'], 90, step)
        if hot:
            add('charge_hot', 'block', ['TEMPERATURES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_T_FRACTION'],
                'The temperature curve allows %d %% of the charge current at 55 °C or above, where LiFePO4 '
                'must not be charged.' % round(hot * 100))
    if _on(eff, 'DCCM_T_ENABLE'):
        cold = _curve_max(eff, 'TEMPERATURES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_T_FRACTION', -40, LFP['discharge_t_min'] - 0.5, step)
        if cold:
            add('discharge_cold', 'danger', ['TEMPERATURES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_T_FRACTION'],
                'The temperature curve allows discharging below -20 °C, colder than LiFePO4 cells are rated for.')
        hot = _curve_max(eff, 'TEMPERATURES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_T_FRACTION', LFP['discharge_t_max'] + 0.5, 90, step)
        if hot:
            add('discharge_hot', 'danger', ['TEMPERATURES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_T_FRACTION'],
                'The temperature curve allows discharging above 60 °C, hotter than LiFePO4 cells are rated for.')

    # --- when the BMS is lost
    if has('BLOCK_ON_DISCONNECT') and not _on(eff, 'BLOCK_ON_DISCONNECT', False):
        to = _f(eff, 'BLOCK_ON_DISCONNECT_TIMEOUT_MINUTES', 30) or 0
        if to > 120:
            add('disconnect_timeout', 'danger', ['BLOCK_ON_DISCONNECT_TIMEOUT_MINUTES'],
                'Without BMS data the system keeps charging and discharging for %g min. Nobody watches the '
                'cells during that time; keep it at 60 min or less.' % to)
        lo, hi = _f(eff, 'BLOCK_ON_DISCONNECT_VOLTAGE_MIN'), _f(eff, 'BLOCK_ON_DISCONNECT_VOLTAGE_MAX')
        if lo is not None and hi is not None and (lo < 3.10 or hi > 3.40):
            add('disconnect_range', 'warn', ['BLOCK_ON_DISCONNECT_VOLTAGE_MIN', 'BLOCK_ON_DISCONNECT_VOLTAGE_MAX'],
                'The safe range %.3f to %.3f V reaches into the steep ends of the LiFePO4 curve, where cells '
                'change fast. 3.25 to 3.35 V is the default.' % (lo, hi))
    if has('BMS_CABLE_ALARM') and not _on(eff, 'BMS_CABLE_ALARM'):
        add('cable_alarm_off', 'warn', ['BMS_CABLE_ALARM'], 'Venus OS will not raise an alarm when the BMS stops answering.')

    # --- settings that do not fit together
    if _on(eff, 'SOC_CALCULATION', False) and str(eff.get('EXTERNAL_SENSOR_DBUS_PATH_SOC', '')).strip():
        add('soc_twice', 'block', ['SOC_CALCULATION'], 'The driver cannot count the SoC itself while an external SoC sensor is set.')
    if _on(eff, 'USE_BMS_DVCC_VALUES', False):
        add('bms_dvcc', 'warn', ['USE_BMS_DVCC_VALUES'], 'Only Felicity and Seplos V3 BMS send their own limits; for a JK BMS leave it off.')
    return out


def safety_gate(before, after, confirm=''):
    """
    Compare the findings of the file as it is (before) with the file after the change.
    Only what the change brings in counts; a finding whose text changed (other numbers) is new.
    -> ('ok' | 'block' | 'confirm', findings that decide it, confirm token)
    """
    known = set((f['id'], f['text']) for f in before)
    new = [f for f in after if (f['id'], f['text']) not in known]
    blocks = [f for f in new if f['level'] == 'block']
    if blocks:
        return 'block', blocks, ''
    dangers = [f for f in new if f['level'] == 'danger']
    token = ','.join(sorted(f['id'] for f in dangers))
    if dangers and (confirm or '') != token:
        return 'confirm', dangers, token
    return 'ok', [], token


def is_serialbattery(values):
    """Does this D-Bus battery service come from dbus-serialbattery?"""
    return ('serialbattery' in str(values.get('/Mgmt/ProcessName', '')).lower() or
            'serialbattery' in str(values.get('/ProductName', '')).lower())


def venus_checks(service, bat, settings, system, eff=None):
    """
    Are the Venus OS settings right for dbus-serialbattery to protect the battery?
    service/bat: the driver's battery service and its values (None/{} when not found);
    settings/system: values of com.victronenergy.settings and .system; eff: effective driver settings.
    -> [{'id', 'level': ok|info|warn|bad, 'title', 'text'}]
    """
    out, eff = [], eff or {}

    def add(cid, level, title, text):
        out.append({'id': cid, 'level': level, 'title': title, 'text': text})

    def num(d, path):
        v = d.get(path)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    if not service:
        add('driver', 'bad', 'Battery driver', 'dbus-serialbattery shows no battery on D-Bus. Check its log: '
            'is the BMS in range and is the MAC address right?')
    elif num(bat, '/Connected') == 0:
        add('driver', 'bad', 'Battery driver', 'dbus-serialbattery has lost the BMS.')
    else:
        add('driver', 'ok', 'Battery driver', '%s is connected.' % (bat.get('/CustomName') or bat.get('/ProductName') or service))

    bol = num(settings, '/Settings/Services/Bol')
    dvcc = bool(int(bol) & 1) if bol is not None else (num(system, '/Control/Dvcc') == 1 if num(system, '/Control/Dvcc') is not None else None)
    if dvcc is None:
        add('dvcc', 'warn', 'DVCC', 'Could not read the DVCC setting.')
    elif not dvcc:
        add('dvcc', 'bad', 'DVCC', 'DVCC is off, so the charge voltage and current limits of the driver do not reach '
            'your chargers. Turn it on: Settings → System setup → Charge control → DVCC.')
    else:
        add('dvcc', 'ok', 'DVCC', 'On: chargers follow the limits of the driver.')

    inst = num(bat, '/DeviceInstance')
    active_bms = system.get('/ActiveBmsService')
    if service:
        if active_bms == service:
            add('bms', 'ok', 'Controlling BMS', 'dbus-serialbattery controls charging.')
        else:
            add('bms', 'bad', 'Controlling BMS', 'The controlling BMS is %s, not dbus-serialbattery. Select it under '
                'Settings → System setup → Charge control → Controlling BMS.' % (active_bms or 'none'))

    active_mon = str(system.get('/ActiveBatteryService') or '')
    if service and inst is not None:
        if active_mon.startswith('com.victronenergy.battery') and active_mon.endswith('/%d' % inst):
            add('monitor', 'ok', 'Battery monitor', 'dbus-serialbattery is the battery monitor.')
        else:
            add('monitor', 'warn', 'Battery monitor', 'The battery monitor is %s. With the BMS as monitor, Venus OS also '
                'acts on its alarms (Settings → System setup → Battery monitor). A SmartShunt there is fine if you '
                'chose it on purpose.' % (active_mon or 'not set'))

    mcc = num(settings, '/Settings/SystemSetup/MaxChargeCurrent')
    driver_cc = None
    try:
        driver_cc = float(eff.get('MAX_BATTERY_CHARGE_CURRENT', ''))
    except ValueError:
        pass
    if mcc is not None and mcc >= 0:
        add('dvcc_current', 'info', 'DVCC charge current limit', 'Venus OS limits charging to %g A as well; the lower '
            'of this and the driver’s %s A applies.' % (mcc, '%g' % driver_cc if driver_cc is not None else '?'))
    mcv = num(settings, '/Settings/SystemSetup/MaxChargeVoltage')
    if mcv:
        add('dvcc_voltage', 'info', 'DVCC charge voltage limit', 'Venus OS limits the charge voltage to %.2f V as well.' % mcv)
    shared = [name for path, name in (('/Settings/SystemSetup/SharedVoltageSense', 'SVS'),
                                      ('/Settings/SystemSetup/SharedTemperatureSense', 'STS'),
                                      ('/Settings/SystemSetup/BatteryCurrentSense', 'SCS')) if num(settings, path) == 1]
    if shared:
        add('shared', 'info', 'Shared sensing', ', '.join(shared) + ' on. The driver docs advise only DVCC on this '
            'page; SVS can help if the battery never reaches the full voltage.')

    # live: what the driver allows right now
    ccl, cvl = num(bat, '/Info/MaxChargeCurrent'), num(bat, '/Info/MaxChargeVoltage')
    temps = [t for t in (num(bat, '/System/MinCellTemperature'), num(bat, '/Dc/0/Temperature')) if t is not None and t > -60]
    cells = num(bat, '/System/NrOfCellsPerBattery')
    if temps and ccl is not None:
        t = min(temps)
        if t <= LFP['charge_t_min'] and ccl > 0:
            add('live_cold', 'bad', 'Charging in the cold', 'The battery is at %.1f °C and the driver still allows %g A '
                'of charge current. LiFePO4 must not be charged at or below 0 °C: check the temperature curve and '
                'the low temperature protection of the JK BMS.' % (t, ccl))
        elif t < 5 and ccl > 0:
            add('live_cold', 'warn', 'Cold battery', 'The battery is at %.1f °C; the driver allows %g A of charge current.' % (t, ccl))
    if cvl and cells and cvl > cells * LFP['cell_max'] + 0.05:
        add('live_cvl', 'bad', 'Charge voltage', 'The driver asks for %.2f V, above %d × 3.65 V = %.2f V.' % (cvl, cells, cells * LFP['cell_max']))
    return out


# ------------------------------------------------ dbus-serialbattery versions

SB_VERSION_RE = re.compile(r'^\s*DRIVER_VERSION\s*(?::\s*str\s*)?=\s*["\']([^"\']+)["\']', re.M)
SB_INSTALL_URL = ('https://raw.githubusercontent.com/mr-manuel/venus-os_dbus-serialbattery/master/'
                  'dbus-serialbattery/install.sh')
SB_RELEASES_API = 'https://api.github.com/repos/mr-manuel/venus-os_dbus-serialbattery/releases/latest'
SB_RELEASES_URL = 'https://github.com/mr-manuel/venus-os_dbus-serialbattery/releases'


def driver_version_from_utils(text):
    """DRIVER_VERSION from dbus-serialbattery's utils.py, e.g. '2.0.20250729', or None."""
    m = SB_VERSION_RE.search(text or '')
    return m.group(1).strip() if m else None


def parse_driver_version(v):
    """'v2.0.20250729' -> (2, 0, 20250729, 1); a dev/beta/rc build sorts before its release (last item 0)."""
    m = re.match(r'^\s*v?(\d+)\.(\d+)\.(\d+)\s*([A-Za-z].*)?$', str(v or ''))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if m.group(4) else 1


def driver_update_available(installed, latest):
    """Is the latest stable release newer than what is installed? (False when either is unknown.)"""
    a, b = parse_driver_version(installed), parse_driver_version(latest)
    return bool(a and b and b > a)


# -------------------------------- a new driver version: what changed in its settings
#
# dbus-serialbattery compares config.ini with its own config.default.ini when it starts. A
# setting it does not know puts the battery in error 119 ("settings incorrect") and blocks
# charging and discharging. So after an update every leftover setting must be found.

# The settings the safety check needs; if the installed driver has one of them no more, say so.
SAFETY_KEYS = ('MAX_BATTERY_CHARGE_CURRENT', 'MAX_BATTERY_DISCHARGE_CURRENT', 'MAX_CELL_VOLTAGE', 'FLOAT_CELL_VOLTAGE',
               'MIN_CELL_VOLTAGE', 'CCCM_CV_ENABLE', 'DCCM_CV_ENABLE', 'CELL_VOLTAGES_WHILE_CHARGING',
               'MAX_CHARGE_CURRENT_CV_FRACTION', 'CELL_VOLTAGES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_CV_FRACTION',
               'CCCM_T_ENABLE', 'TEMPERATURES_WHILE_CHARGING', 'MAX_CHARGE_CURRENT_T_FRACTION', 'DCCM_T_ENABLE',
               'TEMPERATURES_WHILE_DISCHARGING', 'MAX_DISCHARGE_CURRENT_T_FRACTION', 'CVL_CONTROLLER_MODE')


def unknown_settings(default_text, user_text):
    """Keys in config.ini the installed driver does not know (as spelled in config.ini)."""
    known = set(e['key'].lower() for g in parse_default_ini(default_text) for e in g['entries'])
    user = parse_ini(user_text).get('DEFAULT', {})
    spelled = {}
    for line in user_text.splitlines():           # keep the user's own spelling for the message
        m = KEY_RE.match(line)
        if m and not COMMENT_RE.match(line) and not line[:1].isspace():
            spelled[m.group(2).lower()] = m.group(2)
    return [spelled.get(k, k.upper()) for k in user if k not in known]


def settings_findings(default_text, user_text):
    """Findings about the file as a whole, next to battery_safety()."""
    out = []
    for key in unknown_settings(default_text, user_text):
        out.append({'id': 'unknown_' + key.upper(), 'level': 'block', 'keys': [key.upper()],
                    'text': '%s is not a setting of the installed driver. At its next start the driver blocks charging '
                            'and discharging (error 119) until it is removed from config.ini.' % key})
    known = set(e['key'] for g in parse_default_ini(default_text) for e in g['entries'])
    missing = [k for k in SAFETY_KEYS if k not in known]
    if missing and known:
        out.append({'id': 'safety_keys_missing', 'level': 'warn', 'keys': [],
                    'text': 'The installed driver has no setting %s, so the safety check cannot check %s. Check the '
                            'driver’s release notes for what replaced %s.' % (
                                ', '.join(missing), 'it' if len(missing) == 1 else 'them', 'it' if len(missing) == 1 else 'them')})
    return out


def defaults_diff(old_text, new_text):
    """What changed between two config.default.ini files: added, removed and changed defaults."""
    old = dict((e['key'], e) for g in parse_default_ini(old_text) for e in g['entries'])
    new = dict((e['key'], e) for g in parse_default_ini(new_text) for e in g['entries'])
    changed = [{'key': k, 'label': new[k]['label'], 'old': old[k]['default'], 'new': new[k]['default']}
               for k in new if k in old and not same_value(new[k], old[k]['default'], new[k]['default'])]
    return {'added': [{'key': k, 'label': new[k]['label'], 'default': new[k]['default']} for k in new if k not in old],
            'removed': [{'key': k, 'label': old[k]['label'], 'default': old[k]['default']} for k in old if k not in new],
            'changed': changed}


def remove_settings(user_text, keys):
    """config.ini without these keys (whatever their case)."""
    return set_values(user_text, 'DEFAULT', dict((k, None) for k in keys)) if keys else user_text
