"""
Off-device tests: run with  python3 -m unittest discover tests
D-Bus, GLib and velib_python are replaced by small fakes, so this runs on any PC.
"""

import configparser
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))


# ------------------------------------------------------------------ fakes

class FakeService(dict):
    def __init__(self, name, bus=None, register=None):
        super().__init__()
        self.name = name
        self.registered = register is not False
        self._dbusname = None

    def add_mandatory_paths(self, **kw):
        self['/DeviceInstance'] = kw['deviceinstance']
        self['/ProductName'] = kw['productname']
        self['/Connected'] = kw['connected']

    def add_path(self, path, value, **kw):
        self[path] = value

    def register(self):
        self.registered = True


class FakeSettings(dict):
    def __init__(self, bus, supported, callback, timeout=0):
        super().__init__({k: v[1] for k, v in supported.items()})


def _install_fakes():
    clock = {'us': 0}
    glib = types.SimpleNamespace(
        get_monotonic_time=lambda: clock['us'],
        timeout_add_seconds=lambda *a: None,
        MainLoop=None)
    dbus = types.ModuleType('dbus')
    dbus.SystemBus = lambda: object()
    dbus.exceptions = types.SimpleNamespace(DBusException=Exception)
    mainloop = types.ModuleType('dbus.mainloop')
    mainloop_glib = types.ModuleType('dbus.mainloop.glib')
    mainloop_glib.DBusGMainLoop = lambda **kw: None
    gi = types.ModuleType('gi')
    gi_repo = types.ModuleType('gi.repository')
    gi_repo.GLib = glib
    vedbus = types.ModuleType('vedbus')
    vedbus.VeDbusService = FakeService
    settingsdevice = types.ModuleType('settingsdevice')
    settingsdevice.SettingsDevice = FakeSettings
    sys.modules.update({
        'dbus': dbus, 'dbus.mainloop': mainloop, 'dbus.mainloop.glib': mainloop_glib,
        'gi': gi, 'gi.repository': gi_repo,
        'vedbus': vedbus, 'settingsdevice': settingsdevice})
    return clock


CLOCK = _install_fakes()

import esp32_hydro_protocol as proto  # noqa: E402
import mopeka_std_parser  # noqa: E402
import venus_tank_base  # noqa: E402
from esp32_hydro_tank import Esp32HydroDriver  # noqa: E402
from mopeka_venus import MopekaStandardDriver  # noqa: E402
from packets import mopeka_packet  # noqa: E402


def section(text):
    cp = configparser.ConfigParser()
    cp.read_string('[s]\n' + text)
    return cp['s']


# ------------------------------------------------------------------ tests

class TableTests(unittest.TestCase):
    def test_interpolate(self):
        t = venus_tank_base.parse_table('0:0, 100:10, 300:50')
        self.assertEqual(venus_tank_base.interpolate(t, -5), 0)
        self.assertEqual(venus_tank_base.interpolate(t, 50), 5)
        self.assertEqual(venus_tank_base.interpolate(t, 200), 30)
        self.assertEqual(venus_tank_base.interpolate(t, 999), 50)

    def test_table_is_sorted(self):
        self.assertEqual(venus_tank_base.parse_table('5:1;0:0'), [(0, 0), (5, 1)])


class MopekaParserTests(unittest.TestCase):
    PAIRS = [(19, 0), (9, 12)] + [(0, 0)] * 10

    def test_decodes_reading(self):
        r = mopeka_std_parser.parse(mopeka_packet(self.PAIRS))
        self.assertEqual(r.sensor_type, 'Standard')
        self.assertEqual(r.temperature, 19)               # (36-25)*1.777 truncated
        self.assertAlmostEqual(r.battery_voltage, 2.5)
        self.assertEqual(r.battery_percent, 46)
        self.assertEqual(r.best_time, 30)                  # (19+1)+(9+1) accumulated
        self.assertEqual(r.best_amplitude, 12)
        sos = mopeka_std_parser.lpg_speed_of_sound(19, 1.0)
        self.assertAlmostEqual(r.distance_mm, sos * 30 / 100)
        self.assertAlmostEqual(r.distance_mm, 232.75, places=1)

    def test_strongest_echo_wins(self):
        pairs = [(4, 3), (9, 20), (2, 5)] + [(0, 0)] * 9
        r = mopeka_std_parser.parse(mopeka_packet(pairs))
        self.assertEqual((r.best_time, r.best_amplitude, r.usable_values), (10, 20, 3))

    def test_poor_quality(self):
        r = mopeka_std_parser.parse(mopeka_packet([(0, 0)] * 12))
        self.assertFalse(r.quality_ok)

    def test_rejects_other_packets(self):
        self.assertIsNone(mopeka_std_parser.parse(b'\x00' * 10))
        self.assertIsNone(mopeka_std_parser.parse(mopeka_packet(self.PAIRS, sensor_type=0x10)))

    def test_cold_sensor(self):
        r = mopeka_std_parser.parse(mopeka_packet(self.PAIRS, raw_temp=0))
        self.assertEqual(r.temperature, -40)


class HydroProtocolTests(unittest.TestCase):
    def test_roundtrip(self):
        data = proto.build(3, 42.37, 31.8, 75.0, 187, temperature=-4.25,
                           filling=True, sloshing=True, sequence=300)
        self.assertEqual(len(data), 16)
        r = proto.parse(data)
        self.assertEqual((r.device_id, r.sequence, r.height_mm), (3, 44, 187))
        self.assertAlmostEqual(r.level, 42.37)
        self.assertAlmostEqual(r.volume_l, 31.8)
        self.assertAlmostEqual(r.capacity_l, 75.0)
        self.assertAlmostEqual(r.temperature, -4.25)
        self.assertTrue(r.filling and r.sloshing)
        self.assertFalse(r.draining or r.fault)

    def test_no_temperature(self):
        self.assertIsNone(proto.parse(proto.build(1, 0, 0, 75, 0)).temperature)

    def test_firmware_byte_layout(self):
        # Bytes exactly as buildPayload() in the .ino writes them
        fw = bytes([0x56, 0x48, 1, 1, 0x03, 7,
                    0x88, 0x13,     # 5000 -> 50.00 %
                    0x77, 0x01,     # 375  -> 37.5 L
                    0xEE, 0x02,     # 750  -> 75.0 L
                    0xD2, 0x00,     # 210 mm
                    0x18, 0xFC])    # -1000 -> -10.00 C
        r = proto.parse(fw)
        self.assertEqual((r.level, r.volume_l, r.capacity_l, r.height_mm, r.temperature),
                         (50.0, 37.5, 75.0, 210, -10.0))
        self.assertTrue(r.filling)

    def test_rejects_garbage(self):
        self.assertIsNone(proto.parse(b'XX' + bytes(14)))
        self.assertIsNone(proto.parse(b'VH'))


class DriverTests(unittest.TestCase):
    def test_mopeka_driver_publishes(self):
        d = MopekaStandardDriver('s', section(
            'mac = aa:bb:cc:dd:ee:ff\ncapacity_l = 27\nempty_mm = 38\nfull_mm = 254\n'))
        self.assertEqual(d.service.name, 'com.victronenergy.tank.mopeka_standard')
        self.assertEqual(d.service['/FluidType'], 8)           # LPG
        pkt = mopeka_packet(MopekaParserTests.PAIRS)
        d.handle_advertisement('AA:BB:CC:DD:EE:FF', {0x000D: pkt}, -70)
        s = d.service
        self.assertEqual(s['/Connected'], 1)
        expected = 100 * (232.75 - 38) / (254 - 38)
        self.assertAlmostEqual(s['/Level'], expected, places=0)
        self.assertAlmostEqual(s['/Remaining'], 0.027 * s['/Level'] / 100, places=4)
        self.assertEqual(s['/Temperature'], 19.0)
        self.assertEqual(s['/Rssi'], -70)

        # A poor read keeps the previous level
        bad = mopeka_packet([(0, 0)] * 12)
        level = s['/Level']
        d.handle_advertisement('AA:BB:CC:DD:EE:FF', {0x000D: bad}, -70)
        self.assertEqual(s['/Level'], level)
        self.assertIsNone(s['/Mopeka/DistanceMm'])

    def test_mopeka_requires_mac(self):
        with self.assertRaises(SystemExit):
            MopekaStandardDriver('s', section('capacity_l = 27\n'))

    def test_hydro_driver_and_timeout(self):
        d = Esp32HydroDriver('s', section('device_id = 2\ntimeout = 60\n'))
        s = d.service
        self.assertEqual(s['/FluidType'], 1)                   # fresh water
        d.handle_advertisement('M', {0xFFFF: proto.build(1, 50, 37.5, 75, 200)}, -60)
        self.assertIsNone(s['/Level'])                         # other device id ignored

        d.handle_advertisement('M', {0xFFFF: proto.build(2, 50, 37.5, 75, 200, sequence=1)}, -60)
        self.assertEqual((s['/Level'], s['/Remaining'], s['/Capacity']), (50.0, 0.0375, 0.075))
        self.assertEqual(s['/Status'], venus_tank_base.STATUS_OK)

        d.handle_advertisement('M', {0xFFFF: proto.build(2, 0, 0, 75, 0, fault=True, sequence=2)}, -60)
        self.assertEqual(s['/Status'], venus_tank_base.STATUS_ERROR)
        self.assertIsNone(s['/Level'])

        CLOCK['us'] += 61 * 1000000
        d._check_timeout()
        self.assertEqual((s['/Connected'], s['/Status']), (0, venus_tank_base.STATUS_DISCONNECTED))


if __name__ == '__main__':
    unittest.main()
