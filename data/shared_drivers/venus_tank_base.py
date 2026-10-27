"""
Base class for BLE tank sensors published on the Venus OS D-Bus.

A subclass only has to:
  * set the class attributes (PRODUCT_NAME, FLUID_TYPE, ...)
  * implement handle_advertisement(mac, manufacturer_data, rssi) and call
    self.publish(...) with decoded values.

Everything else - D-Bus service, device instance, custom name, timeout
handling and the GLib main loop - lives here.
"""

import argparse
import configparser
import logging
import os
import sys

# velib_python ships with Venus OS; prefer a local copy if one is bundled
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE, 'ext', 'velib_python'),
          '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'):
    if os.path.isdir(p):
        sys.path.insert(1, p)
        break

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402

from vedbus import VeDbusService  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

from ble_scanner import BlueZScanner  # noqa: E402

log = logging.getLogger(__name__)

VERSION = '1.0.0'
DEFAULT_CONFIG = os.path.join(HERE, 'config.ini')

# Venus OS tank FluidType enum
FLUID_FUEL = 0
FLUID_FRESH_WATER = 1
FLUID_WASTE_WATER = 2
FLUID_LIVE_WELL = 3
FLUID_OIL = 4
FLUID_BLACK_WATER = 5
FLUID_GASOLINE = 6
FLUID_DIESEL = 7
FLUID_LPG = 8
FLUID_LNG = 9
FLUID_HYDRAULIC_OIL = 10
FLUID_RAW_WATER = 11

# Venus OS tank /Status enum
STATUS_OK = 0
STATUS_DISCONNECTED = 1
STATUS_SHORT_CIRCUITED = 2
STATUS_UNKNOWN = 3
STATUS_ERROR = 4               # "configuration error" in the dbus docs


def parse_table(text):
    """'0:0, 120:15.5, 400:60' -> [(0.0, 0.0), (120.0, 15.5), (400.0, 60.0)] sorted by x."""
    points = []
    for item in text.replace(';', ',').split(','):
        item = item.strip()
        if not item:
            continue
        x, y = item.split(':')
        points.append((float(x), float(y)))
    points.sort()
    if len(points) < 2:
        raise ValueError('a lookup table needs at least two points')
    return points


def interpolate(table, x):
    """Linear interpolation in a sorted [(x, y), ...] table, clamped at both ends."""
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


class VenusTankBaseDriver(object):
    # Override in subclasses
    PRODUCT_NAME = 'BLE tank sensor'
    PRODUCT_ID = 0xFFFF            # not a Victron product
    FLUID_TYPE = FLUID_FRESH_WATER
    DEFAULT_SERVICE = 'ble_tank'
    DEFAULT_TIMEOUT_S = 600

    def __init__(self, section, config):
        self.section = section
        self.cfg = config
        self.mac = config.get('mac', '').strip().upper() or None
        self.timeout_s = config.getint('timeout', self.DEFAULT_TIMEOUT_S)
        self.capacity_m3 = config.getfloat('capacity_l', 0.0) / 1000.0 or None
        self.fluid_type = config.getint('fluid_type', self.FLUID_TYPE)
        self._last_seen = None

        self.bus = dbus.SystemBus()
        uid = config.get('service', self.DEFAULT_SERVICE)
        servicename = 'com.victronenergy.tank.' + uid

        # Device instance and custom name are stored in localsettings, so the
        # user can change them from the GUI (Settings > Devices) and they
        # survive reboots. localsettings resolves instance clashes itself.
        settings_base = '/Settings/Devices/%s' % uid
        self.settings = SettingsDevice(self.bus, {
            'instance': [settings_base + '/ClassAndVrmInstance',
                         'tank:%d' % config.getint('device_instance', 20), 0, 0],
            'customname': [settings_base + '/CustomName',
                           config.get('custom_name', ''), 0, 0],
        }, self._on_setting_changed, timeout=60)
        instance = int(self.settings['instance'].split(':')[1])

        try:
            svc = VeDbusService(servicename, self.bus, register=False)
        except TypeError:  # velib_python older than 2024 has no register kwarg
            svc = VeDbusService(servicename, self.bus)
        self.service = svc

        svc.add_mandatory_paths(
            processname=os.path.basename(sys.argv[0]),
            processversion=VERSION,
            connection='Bluetooth LE %s' % (self.mac or 'any'),
            deviceinstance=instance,
            productid=self.PRODUCT_ID,
            productname=self.PRODUCT_NAME,
            firmwareversion=None,
            hardwareversion=None,
            connected=0)
        svc.add_path('/CustomName', self.settings['customname'], writeable=True,
                     onchangecallback=self._on_customname_changed)
        svc.add_path('/FluidType', self.fluid_type, writeable=True,
                     onchangecallback=lambda p, v: 0 <= int(v) <= 11)
        svc.add_path('/Capacity', self.capacity_m3,
                     gettextcallback=lambda p, v: '%.0f L' % (v * 1000))
        svc.add_path('/Level', None, gettextcallback=lambda p, v: '%.1f %%' % v)
        svc.add_path('/Remaining', None,
                     gettextcallback=lambda p, v: '%.1f L' % (v * 1000))
        svc.add_path('/Status', STATUS_UNKNOWN)
        svc.add_path('/Temperature', None,
                     gettextcallback=lambda p, v: '%.1f C' % v)
        svc.add_path('/BatteryVoltage', None,
                     gettextcallback=lambda p, v: '%.2f V' % v)
        svc.add_path('/RawValue', None)
        svc.add_path('/RawUnit', '')
        svc.add_path('/Rssi', None)
        self.add_extra_paths(svc)

        if hasattr(svc, 'register') and getattr(svc, '_dbusname', None) is None:
            svc.register()
        log.info('Registered %s (instance %d)', servicename, instance)

    # --------------------------------------------------------- subclass hooks

    def add_extra_paths(self, service):
        """Override to add sensor-specific D-Bus paths."""

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        raise NotImplementedError

    # ---------------------------------------------------------------- publish

    def publish(self, level=None, remaining_m3=None, capacity_m3=None,
                temperature=None, battery_voltage=None, raw_value=None,
                raw_unit=None, rssi=None, status=STATUS_OK, extra=None):
        """Push a fresh reading to D-Bus. level in %, volumes in m3."""
        if capacity_m3 is not None:
            self.capacity_m3 = capacity_m3
        if remaining_m3 is None and level is not None and self.capacity_m3:
            remaining_m3 = self.capacity_m3 * level / 100.0
        s = self.service
        s['/Connected'] = 1
        s['/Status'] = status
        s['/Capacity'] = round(self.capacity_m3, 4) if self.capacity_m3 else None
        s['/Level'] = None if level is None else round(max(0.0, min(100.0, level)), 1)
        s['/Remaining'] = None if remaining_m3 is None else round(remaining_m3, 5)
        s['/Temperature'] = temperature
        s['/BatteryVoltage'] = battery_voltage
        s['/RawValue'] = raw_value
        if raw_unit is not None:
            s['/RawUnit'] = raw_unit
        s['/Rssi'] = rssi
        for path, value in (extra or {}).items():
            s[path] = value
        self._last_seen = GLib.get_monotonic_time()

    def _check_timeout(self):
        if self._last_seen is None:
            return True
        age_s = (GLib.get_monotonic_time() - self._last_seen) / 1e6
        if age_s > self.timeout_s and self.service['/Connected'] == 1:
            log.warning('No data for %d s, marking sensor disconnected', age_s)
            s = self.service
            s['/Connected'] = 0
            s['/Status'] = STATUS_DISCONNECTED
            s['/Level'] = None
            s['/Remaining'] = None
        return True

    # --------------------------------------------------------------- settings

    def _on_customname_changed(self, path, value):
        self.settings['customname'] = value
        return True

    def _on_setting_changed(self, setting, old, new):
        if setting == 'customname':
            self.service['/CustomName'] = new
        elif setting == 'instance':
            self.service['/DeviceInstance'] = int(new.split(':')[1])

    # ------------------------------------------------------------------- run

    def run(self):
        scanner = BlueZScanner(self.bus, self.handle_advertisement,
                               adapter=self.cfg.get('adapter', 'hci0'),
                               mac_filter=[self.mac] if self.mac else None)
        scanner.start()
        GLib.timeout_add_seconds(10, self._check_timeout)
        GLib.MainLoop().run()

    @classmethod
    def main(cls):
        ap = argparse.ArgumentParser(description=cls.PRODUCT_NAME + ' Venus OS driver')
        ap.add_argument('--config', default=DEFAULT_CONFIG)
        ap.add_argument('--section', default=cls.DEFAULT_SERVICE,
                        help='section in config.ini describing this sensor')
        ap.add_argument('--debug', action='store_true')
        args = ap.parse_args()

        logging.basicConfig(
            level=logging.DEBUG if args.debug else logging.INFO,
            format='%(levelname)s %(name)s: %(message)s')

        parser = configparser.ConfigParser()
        if not parser.read(args.config):
            log.error('Config file %s not found', args.config)
            sys.exit(1)
        if not parser.has_section(args.section):
            log.error('Section [%s] missing in %s', args.section, args.config)
            sys.exit(1)

        DBusGMainLoop(set_as_default=True)
        cls(args.section, parser[args.section]).run()
