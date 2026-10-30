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


def write_file(path, text):
    """Atomic write; the previous version is kept as <path>.bak."""
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
REBOOT_KEYS = ('BLUETOOTH_BMS', 'BLUETOOTH_USE_USB', 'CAN_PORT')


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
            'reboot_keys': list(REBOOT_KEYS)}


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
