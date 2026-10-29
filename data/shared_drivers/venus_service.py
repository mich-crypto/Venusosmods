"""
Shared Venus OS plumbing for all drivers in this folder.

Layers:
  parsers  (mopeka_std_parser, esp32_hydro_protocol, alpicool_protocol)
           pure Python, decode bytes into readings, no D-Bus
  devices  (VenusDevice and its subclasses TankDevice / TemperatureDevice)
           one com.victronenergy.<class>.<id> service each
  drivers  (BleDriver subclasses: mopeka_venus, esp32_hydro_tank, fridge_venus)
           one process per config section: receive data, feed devices

Every VenusDevice gets its own private D-Bus connection. vedbus exports
paths like /Temperature as D-Bus objects, and object paths are per
connection, so two services in one process must not share a connection.
"""

import argparse
import configparser
import glob
import logging
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VELIB_CANDIDATES = [os.path.join(HERE, 'ext', 'velib_python'),
                    '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python']


def find_velib():
    """First velib_python directory that exists, or None."""
    for p in VELIB_CANDIDATES + sorted(glob.glob('/opt/victronenergy/*/ext/velib_python')):
        if os.path.isfile(os.path.join(p, 'vedbus.py')):
            return p
    return None


_velib = find_velib()
if _velib:
    sys.path.insert(1, _velib)

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402

try:
    from vedbus import VeDbusService  # noqa: E402
    from settingsdevice import SettingsDevice  # noqa: E402
except ImportError:  # reported by main() and the diagnostics script
    VeDbusService = SettingsDevice = None

log = logging.getLogger(__name__)

VERSION = '1.1.0'
DEFAULT_CONFIG = os.path.join(HERE, 'config.ini')


def monotonic_s():
    return GLib.get_monotonic_time() / 1e6


class VenusDevice(object):
    """
    One D-Bus service with the mandatory paths, a VRM device instance and a
    custom name, both stored in localsettings (editable in the GUI).
    Subclasses add their paths in add_paths() and their values in publish().
    """
    DEVICE_CLASS = None            # 'tank', 'temperature', ...
    STATUS_OK = 0
    STATUS_DISCONNECTED = 1

    def __init__(self, settings_bus, uid, product_name, connection,
                 default_instance=20, custom_name='', timeout_s=600,
                 product_id=0xFFFF, extra_paths=(), setup=None):
        self.uid = uid
        self.timeout_s = timeout_s
        self.last_seen = None
        self.servicename = 'com.victronenergy.%s.%s' % (self.DEVICE_CLASS, uid)

        base = '/Settings/Devices/%s' % uid
        self.settings = SettingsDevice(settings_bus, {
            'instance': [base + '/ClassAndVrmInstance',
                         '%s:%d' % (self.DEVICE_CLASS, default_instance), 0, 0],
            'customname': [base + '/CustomName', custom_name, 0, 0],
        }, self._on_setting_changed, timeout=60)
        instance = int(self.settings['instance'].split(':')[1])

        self.bus = dbus.SystemBus(private=True)
        try:
            svc = VeDbusService(self.servicename, self.bus, register=False)
        except TypeError:  # velib_python older than 2024 has no register kwarg
            svc = VeDbusService(self.servicename, self.bus)
        self.service = svc

        svc.add_mandatory_paths(
            processname=os.path.basename(sys.argv[0]),
            processversion=VERSION,
            connection=connection,
            deviceinstance=instance,
            productid=product_id,
            productname=product_name,
            firmwareversion=None,
            hardwareversion=None,
            connected=0)
        svc.add_path('/CustomName', self.settings['customname'], writeable=True,
                     onchangecallback=self._on_customname_changed)
        svc.add_path('/Status', self.STATUS_DISCONNECTED)
        self.add_paths(svc)
        for path in extra_paths:   # sensor-specific paths, all added before register()
            svc.add_path(path, None)
        if setup is not None:      # writable/special paths: setup(service)
            setup(svc)

        if getattr(svc, '_dbusname', None) is None:
            svc.register()
        log.info('Registered %s (instance %d)', self.servicename, instance)

    def add_paths(self, service):
        """Override: add the class-specific D-Bus paths."""

    def __getitem__(self, path):
        return self.service[path]

    def __setitem__(self, path, value):
        self.service[path] = value

    def mark_seen(self, status=None):
        self.service['/Connected'] = 1
        self.service['/Status'] = self.STATUS_OK if status is None else status
        self.last_seen = monotonic_s()

    def check_timeout(self):
        """Mark the device disconnected and invalidate values when data stops."""
        if self.last_seen is None or self.service['/Connected'] == 0:
            return
        age = monotonic_s() - self.last_seen
        if age > self.timeout_s:
            log.warning('%s: no data for %d s, marking disconnected', self.servicename, age)
            self.service['/Connected'] = 0
            self.service['/Status'] = self.STATUS_DISCONNECTED
            self.invalidate()

    def invalidate(self):
        """Override: set measurement paths to None."""

    def _on_customname_changed(self, path, value):
        self.settings['customname'] = value
        return True

    def _on_setting_changed(self, setting, old, new):
        if setting == 'customname':
            self.service['/CustomName'] = new
        elif setting == 'instance':
            self.service['/DeviceInstance'] = int(new.split(':')[1])


# ------------------------------------------------------------------ tanks

# Venus OS tank /FluidType (same as NMEA 2000)
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


class TankDevice(VenusDevice):
    """com.victronenergy.tank.* - volumes are m3 on D-Bus, litres in config."""
    DEVICE_CLASS = 'tank'
    STATUS_SHORT_CIRCUITED = 2
    STATUS_UNKNOWN = 3
    STATUS_ERROR = 4               # "configuration error" in the dbus docs

    def __init__(self, settings_bus, uid, product_name, connection, fluid_type,
                 capacity_l=None, **kw):
        self.fluid_type = fluid_type
        self.capacity_m3 = capacity_l / 1000.0 if capacity_l else None
        super(TankDevice, self).__init__(settings_bus, uid, product_name, connection, **kw)

    def add_paths(self, s):
        s.add_path('/FluidType', self.fluid_type, writeable=True,
                   onchangecallback=lambda p, v: 0 <= int(v) <= 11)
        s.add_path('/Capacity', self.capacity_m3,
                   gettextcallback=lambda p, v: '%.0f L' % (v * 1000))
        s.add_path('/Level', None, gettextcallback=lambda p, v: '%.1f %%' % v)
        s.add_path('/Remaining', None,
                   gettextcallback=lambda p, v: '%.1f L' % (v * 1000))
        s.add_path('/Temperature', None, gettextcallback=lambda p, v: '%.1f C' % v)
        s.add_path('/BatteryVoltage', None, gettextcallback=lambda p, v: '%.2f V' % v)
        s.add_path('/RawValue', None)
        s.add_path('/RawUnit', '')
        s.add_path('/Rssi', None)

    def publish(self, level=None, remaining_m3=None, capacity_m3=None,
                temperature=None, battery_voltage=None, raw_value=None,
                raw_unit=None, rssi=None, status=None, extra=None):
        """level in %, volumes in m3. Remaining is derived from level if not given."""
        if capacity_m3 is not None:
            self.capacity_m3 = capacity_m3
        if remaining_m3 is None and level is not None and self.capacity_m3:
            remaining_m3 = self.capacity_m3 * level / 100.0
        s = self.service
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
        self.mark_seen(status)

    def invalidate(self):
        self.service['/Level'] = None
        self.service['/Remaining'] = None


# ------------------------------------------------------------ temperatures

TEMP_BATTERY = 0
TEMP_FRIDGE = 1
TEMP_GENERIC = 2
TEMP_ROOM = 3
TEMP_OUTDOOR = 4
TEMP_WATER_HEATER = 5
TEMP_FREEZER = 6

TEMPERATURE_TYPES = {
    'battery': TEMP_BATTERY, 'fridge': TEMP_FRIDGE, 'generic': TEMP_GENERIC,
    'room': TEMP_ROOM, 'outdoor': TEMP_OUTDOOR, 'waterheater': TEMP_WATER_HEATER,
    'freezer': TEMP_FREEZER,
}


class TemperatureDevice(VenusDevice):
    """com.victronenergy.temperature.* - shows up under the GUI's environment sensors."""
    DEVICE_CLASS = 'temperature'
    STATUS_UNKNOWN = 4

    def __init__(self, settings_bus, uid, product_name, connection, temperature_type,
                 **kw):
        self.temperature_type = temperature_type
        super(TemperatureDevice, self).__init__(settings_bus, uid, product_name, connection, **kw)

    def add_paths(self, s):
        s.add_path('/Temperature', None, gettextcallback=lambda p, v: '%.1f C' % v)
        s.add_path('/TemperatureType', self.temperature_type, writeable=True,
                   onchangecallback=lambda p, v: 0 <= int(v) <= 6)

    def publish(self, temperature, temperature_type=None, extra=None, status=None):
        s = self.service
        s['/Temperature'] = None if temperature is None else round(temperature, 2)
        if temperature_type is not None:
            s['/TemperatureType'] = temperature_type
        for path, value in (extra or {}).items():
            s[path] = value
        self.mark_seen(status)

    def invalidate(self):
        self.service['/Temperature'] = None


# ---------------------------------------------------------------- helpers

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


# ----------------------------------------------------------------- driver

class BleDriver(object):
    """
    One process = one config section. Owns the GLib main loop, the shared
    BLE scanner and the per-device timeout check.
    """
    DEFAULT_SECTION = None
    USES_SCANNER = True

    def __init__(self, section, config):
        self.section = section
        self.cfg = config
        self.mac = config.get('mac', '').strip().upper() or None
        self.bus = dbus.SystemBus()   # BlueZ + localsettings
        self.devices = []
        self.loop = None

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        """Override if USES_SCANNER."""

    def start(self):
        """Override for extra start-up work (timers, connections)."""

    def shutdown(self):
        """Override to release resources (e.g. disconnect GATT)."""

    def _check_timeouts(self):
        for d in self.devices:
            d.check_timeout()
        return True

    def _on_signal(self):
        log.info('Stopping')
        try:
            self.shutdown()
        finally:
            self.loop.quit()
        return False

    def run(self):
        from ble_scanner import BlueZScanner
        if self.USES_SCANNER:
            self.scanner = BlueZScanner(
                self.bus, self.handle_advertisement,
                adapter=self.cfg.get('adapter', 'hci0'),
                mac_filter=[self.mac] if self.mac else None)
            self.scanner.start()
        self.start()
        GLib.timeout_add_seconds(10, self._check_timeouts)
        self.loop = GLib.MainLoop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
        self.loop.run()

    @classmethod
    def main(cls):
        ap = argparse.ArgumentParser(description='Venus OS driver: ' + cls.__name__)
        ap.add_argument('--config', default=DEFAULT_CONFIG)
        ap.add_argument('--section', default=cls.DEFAULT_SECTION,
                        help='section in config.ini describing this sensor')
        ap.add_argument('--debug', action='store_true')
        args = ap.parse_args()

        logging.basicConfig(
            level=logging.DEBUG if args.debug else logging.INFO,
            format='%(levelname)s %(name)s: %(message)s')

        if VeDbusService is None:
            log.error('velib_python not found (looked in %s)', ', '.join(VELIB_CANDIDATES))
            sys.exit(1)
        parser = configparser.ConfigParser()
        if not parser.read(args.config):
            log.error('Config file %s not found', args.config)
            sys.exit(1)
        if not parser.has_section(args.section):
            log.error('Section [%s] missing in %s', args.section, args.config)
            sys.exit(1)

        DBusGMainLoop(set_as_default=True)
        cls(args.section, parser[args.section]).run()
