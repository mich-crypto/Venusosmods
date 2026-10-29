"""
Off-device tests: run with  python3 -m unittest discover tests
D-Bus, GLib and velib_python are replaced by small fakes, so this runs on any PC.
"""

import configparser
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import fakes  # noqa: E402  (must come first: replaces dbus, gi, vedbus in sys.modules)
from fakes import FakeBus, FakeService, FakeSettings  # noqa: E402

CLOCK = fakes.CLOCK

import alpicool_protocol as fridge  # noqa: E402
import esp32_hydro_protocol as proto  # noqa: E402
import mopeka_std_parser  # noqa: E402
import venus_service  # noqa: E402
from esp32_hydro_tank import Esp32HydroDriver  # noqa: E402
from fridge_venus import FridgeDriver  # noqa: E402
from mopeka_venus import MopekaStandardDriver  # noqa: E402
from packets import mopeka_packet  # noqa: E402


def section(text):
    cp = configparser.ConfigParser()
    cp.read_string('[s]\n' + text)
    return cp['s']


def hexbytes(text):
    return bytes.fromhex(text.replace(' ', ''))


# ------------------------------------------------------------------ tests

class TableTests(unittest.TestCase):
    def test_interpolate(self):
        t = venus_service.parse_table('0:0, 100:10, 300:50')
        self.assertEqual(venus_service.interpolate(t, -5), 0)
        self.assertEqual(venus_service.interpolate(t, 50), 5)
        self.assertEqual(venus_service.interpolate(t, 200), 30)
        self.assertEqual(venus_service.interpolate(t, 999), 50)

    def test_table_is_sorted(self):
        self.assertEqual(venus_service.parse_table('5:1;0:0'), [(0, 0), (5, 1)])


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


# Frames captured from real fridges (klightspeed/BrassMonkeyFridgeMonitor and
# hairydood-debug/Alpicool50l12vfridgefreezer)
CAPTURED_QUERY = 'fe fe 03 01 02 00'
CAPTURED_STATUS_1 = 'fe fe 15 01 00 01 00 00 f1 14 ec 02 00 00 00 00 00 00 f3 64 0c 03 05 6c'
CAPTURED_STATUS_2 = 'fe fe 15 01 00 01 00 02 ec 14 ec 02 00 00 00 00 00 00 f7 7f 0b 01 05 85'
CAPTURED_SET_LEFT_3C = 'fe fe 04 05 03 02 08'
CAPTURED_SET_DUAL = ('fe fe 1c 02 00 00 01 02 04 08 00 02 00 00 00 00 01 00 f0 f4',
                     'ec 02 00 00 01 00 00 03 00 05 02')


def dual_zone_status(left_current=4, right_current=-17, right_target=-18, unit=0):
    data = bytearray(30)
    data[1] = 1                        # powered on
    data[2] = 1                        # eco
    data[4] = 3                        # left target
    data[9] = unit
    data[14] = left_current & 0xFF
    data[15] = 0x7F                    # battery % unknown
    data[16], data[17] = 12, 8         # 12.8 V
    data[18] = right_target & 0xFF
    data[19], data[20] = (-12) & 0xFF, (-22) & 0xFF   # freezer range -22..-12
    data[5], data[6] = 8, 0                          # fridge range 0..8
    data[26] = right_current & 0xFF
    data[28] = 3                       # model
    return fridge.build_frame(fridge.CMD_QUERY, bytes(data))


class FridgeProtocolTests(unittest.TestCase):
    def test_query_frame_matches_capture(self):
        self.assertEqual(fridge.query_frame(), hexbytes(CAPTURED_QUERY))
        self.assertEqual(fridge.build_frame(fridge.CMD_SET_LEFT, b'\x03'),
                         hexbytes(CAPTURED_SET_LEFT_3C))

    def test_captured_single_zone_status(self):
        frames = fridge.FrameAssembler().feed(hexbytes(CAPTURED_STATUS_1))
        self.assertEqual(len(frames), 1)
        cmd, data = frames[0]
        self.assertEqual(cmd, fridge.CMD_QUERY)
        st = fridge.parse_status(data)
        self.assertEqual(len(st.zones), 1)
        self.assertEqual((st.zones[0].target, st.zones[0].current), (-15.0, -13.0))
        self.assertEqual((st.battery_percent, st.supply_voltage), (100, 12.3))
        self.assertTrue(st.powered_on)
        self.assertFalse(st.eco_mode or st.locked or st.fahrenheit)

    def test_second_capture(self):
        cmd, data = fridge.FrameAssembler().feed(hexbytes(CAPTURED_STATUS_2))[0]
        st = fridge.parse_status(data)
        self.assertEqual((st.zones[0].target, st.zones[0].current), (-20.0, -9.0))
        self.assertIsNone(st.battery_percent)          # 0x7f = unknown
        self.assertEqual((st.supply_voltage, st.battery_saver), (11.1, 2))

    def test_split_notifications_and_noise(self):
        asm = fridge.FrameAssembler()
        a, b = (hexbytes(x) for x in CAPTURED_SET_DUAL)
        self.assertEqual(asm.feed(b'\x00\x13' + a), [])            # junk + first half
        frames = asm.feed(b + hexbytes(CAPTURED_QUERY)[:3])        # second half + partial
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], fridge.CMD_SET_OTHERS)
        self.assertEqual(len(frames[0][1]), 25)
        frames = asm.feed(hexbytes(CAPTURED_QUERY)[3:])
        self.assertEqual(frames, [(fridge.CMD_QUERY, b'')])

    def test_bad_checksum_dropped(self):
        bad = bytearray(hexbytes(CAPTURED_STATUS_1))
        bad[-1] ^= 1
        self.assertEqual(fridge.FrameAssembler().feed(bytes(bad)), [])

    def test_dual_zone_and_fahrenheit(self):
        cmd, data = fridge.FrameAssembler().feed(dual_zone_status())[0]
        st = fridge.parse_status(data)
        self.assertEqual([(z.target, z.current) for z in st.zones], [(3, 4), (-18, -17)])
        self.assertEqual((st.supply_voltage, st.model), (12.8, 3))
        # unit=1: the fridge sends Fahrenheit
        cmd, data = fridge.FrameAssembler().feed(dual_zone_status(41, 5, 0, unit=1))[0]
        st = fridge.parse_status(data)
        self.assertEqual(st.zones[0].current, 5.0)
        self.assertEqual(st.zones[1].current, -15.0)

    def test_captured_hyckes_dual_zone(self):
        # The owner's Hyckes fridge, two notifications as received on the Pi
        asm = fridge.FrameAssembler()
        self.assertEqual(asm.feed(hexbytes(
            'fe fe 21 01 00 01 01 02 08 08 00 02 00 00 00 00 fe 00 06 64')), [])
        cmd, data = asm.feed(hexbytes(
            '0c 04 ec f4 ec 02 00 00 fe 00 f8 00 03 00 08 73'))[0]
        st = fridge.parse_status(data)
        self.assertEqual([(z.current, z.target) for z in st.zones], [(6, 8), (-8, -20)])
        self.assertEqual((st.supply_voltage, st.battery_percent, st.battery_saver,
                          st.eco_mode, st.powered_on, st.model), (12.4, 100, 2, True, True, 3))

    def test_missing_second_sensor(self):
        cmd, data = fridge.FrameAssembler().feed(dual_zone_status(right_current=-128))[0]
        self.assertEqual(len(fridge.parse_status(data).zones), 1)

    def test_chunks(self):
        frame = dual_zone_status()
        parts = fridge.chunks(frame)
        self.assertTrue(all(len(p) <= 20 for p in parts))
        self.assertEqual(b''.join(parts), frame)


class DriverTests(unittest.TestCase):
    def test_mopeka_driver_publishes(self):
        d = MopekaStandardDriver('s', section(
            'mac = aa:bb:cc:dd:ee:ff\ncapacity_l = 27\nempty_mm = 38\nfull_mm = 254\n'))
        s = d.tank.service
        self.assertEqual(s.name, 'com.victronenergy.tank.mopeka_standard')
        self.assertEqual(s['/FluidType'], 8)                   # LPG
        self.assertIn('/Mopeka/DistanceMm', s.paths_at_register)
        self.assertEqual(s['/Capacity'], 0.027)                # m3 on D-Bus
        pkt = mopeka_packet(MopekaParserTests.PAIRS)
        d.handle_advertisement('AA:BB:CC:DD:EE:FF', {0x000D: pkt}, -70)
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

    def test_mopeka_level_curve(self):
        d = MopekaStandardDriver('s', section(
            'mac = aa:bb:cc:dd:ee:ff\nempty_mm = 0\nfull_mm = 400\n'
            'level_curve = 0:0, 0.1:4.6, 0.194:13.9, 1:100\n'))
        self.assertEqual(d.distance_to_level(0), 0)
        self.assertAlmostEqual(d.distance_to_level(40), 4.6)        # 0.1 of full
        self.assertAlmostEqual(d.distance_to_level(400), 100)
        self.assertAlmostEqual(d.distance_to_level(500), 100)       # above full: clamped
        mid = d.distance_to_level(200)                              # straight part
        self.assertAlmostEqual(mid, 13.9 + (0.5 - 0.194) / (1 - 0.194) * 86.1, places=3)

    def test_mopeka_pairs_on_sync_button(self):
        d = MopekaStandardDriver('s', section('capacity_l = 27\n'))
        s = d.tank.service
        self.assertIsNone(d.mac)
        normal = mopeka_packet(MopekaParserTests.PAIRS)
        synced = mopeka_packet(MopekaParserTests.PAIRS, sync=True)

        # a neighbour's sensor without sync press is ignored
        d.handle_advertisement('11:11:11:11:11:11', {0x000D: normal}, -80)
        self.assertIsNone(s['/Level'])
        # our sensor with the sync button pressed pairs and publishes
        d.handle_advertisement('AA:BB:CC:DD:EE:FF', {0x000D: synced}, -60)
        self.assertEqual(d.mac, 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(d.paired['mac'], 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(s['/Mgmt/Connection'], 'Bluetooth LE AA:BB:CC:DD:EE:FF')
        self.assertIsNotNone(s['/Level'])
        # afterwards other sensors are ignored, even with sync pressed
        level = s['/Level']
        d.handle_advertisement('11:11:11:11:11:11', {0x000D: mopeka_packet(
            [(5, 9)] + [(0, 0)] * 11, sync=True)}, -80)
        self.assertEqual((d.mac, s['/Level']), ('AA:BB:CC:DD:EE:FF', level))

    def test_mopeka_uses_stored_pairing(self):
        orig = FakeSettings.__init__

        def stored(self, bus, supported, cb, timeout=0):
            orig(self, bus, supported, cb, timeout)
            if 'mac' in self:
                self['mac'] = 'aa:bb:cc:dd:ee:01'
        FakeSettings.__init__ = stored
        try:
            d = MopekaStandardDriver('s', section(''))
        finally:
            FakeSettings.__init__ = orig
        self.assertEqual(d.mac, 'AA:BB:CC:DD:EE:01')

    def test_sync_bit_parsed(self):
        r = mopeka_std_parser.parse(mopeka_packet(MopekaParserTests.PAIRS, sync=True))
        self.assertTrue(r.sync_pressed)
        self.assertEqual(r.temperature, 19)

    def test_hydro_driver_and_timeout(self):
        d = Esp32HydroDriver('s', section('device_id = 2\ntimeout = 60\n'))
        s = d.tank.service
        self.assertIsNone(d.temp)
        self.assertEqual(s['/FluidType'], 1)                   # fresh water
        d.handle_advertisement('M', {0xFFFF: proto.build(1, 50, 37.5, 75, 200)}, -60)
        self.assertIsNone(s['/Level'])                         # other device id ignored

        d.handle_advertisement('M', {0xFFFF: proto.build(2, 50, 37.5, 75, 200, sequence=1)}, -60)
        self.assertEqual((s['/Level'], s['/Remaining'], s['/Capacity']), (50.0, 0.0375, 0.075))
        self.assertEqual(s['/Status'], venus_service.TankDevice.STATUS_OK)

        d.handle_advertisement('M', {0xFFFF: proto.build(2, 0, 0, 75, 0, fault=True, sequence=2)}, -60)
        self.assertEqual(s['/Status'], venus_service.TankDevice.STATUS_ERROR)
        self.assertIsNone(s['/Level'])

        CLOCK['us'] += 61 * 1000000
        d._check_timeouts()
        self.assertEqual((s['/Connected'], s['/Status']),
                         (0, venus_service.TankDevice.STATUS_DISCONNECTED))

        # and it recovers on the next packet
        d.handle_advertisement('M', {0xFFFF: proto.build(2, 20, 15, 75, 90, sequence=3)}, -60)
        self.assertEqual((s['/Connected'], s['/Level']), (1, 20.0))

    def test_hydro_temperature_service(self):
        d = Esp32HydroDriver('s', section('temperature_service = yes\ntimeout = 60\n'))
        t = d.temp.service
        self.assertEqual(t.name, 'com.victronenergy.temperature.esp32_hydro_temp')
        self.assertEqual(t['/TemperatureType'], venus_service.TEMP_GENERIC)
        d.handle_advertisement('M', {0xFFFF: proto.build(1, 50, 37, 75, 200, temperature=9.5,
                                                         sequence=1)}, -60)
        self.assertEqual((t['/Temperature'], t['/Connected']), (9.5, 1))

        # probe unplugged: the tank keeps updating, the temperature ages out
        CLOCK['us'] += 30 * 1000000
        d.handle_advertisement('M', {0xFFFF: proto.build(1, 50, 37, 75, 200, sequence=2)}, -60)
        CLOCK['us'] += 40 * 1000000
        d._check_timeouts()
        self.assertEqual(d.tank.service['/Connected'], 1)
        self.assertEqual((t['/Connected'], t['/Temperature']), (0, None))

    def test_fridge_driver(self):
        d = FridgeDriver('s', section('mac = 11:22:33:44:55:66\nservice = van_fridge\n'))
        self.assertEqual(len(d.zones), 1)
        z1 = d.zones[0].service
        self.assertEqual(z1.name, 'com.victronenergy.temperature.van_fridge_zone1')
        self.assertIn('/Fridge/SupplyVoltage', z1.paths_at_register)

        for part in fridge.chunks(dual_zone_status()):          # arrives in pieces
            d._on_notify(part)
        self.assertEqual(len(d.zones), 2)
        z2 = d.zones[1].service
        self.assertEqual(z2.name, 'com.victronenergy.temperature.van_fridge_zone2')
        self.assertEqual((z1['/Temperature'], z1['/TemperatureType']), (4.0, venus_service.TEMP_FRIDGE))
        self.assertEqual((z2['/Temperature'], z2['/TemperatureType']), (-17.0, venus_service.TEMP_FREEZER))
        self.assertEqual((z2['/CustomName'], z1['/Fridge/SupplyVoltage']), ('Freezer', 12.8))
        self.assertEqual(z1['/Fridge/EcoMode'], 1)

    def test_fridge_setpoint_range(self):
        d = FridgeDriver('s', section('mac = 11:22:33:44:55:66\nzone2_min = -18\nzone2_max = -10\n'))
        for part in fridge.chunks(dual_zone_status()):
            d._on_notify(part)
        sw = d.switch.service
        self.assertEqual((sw['/SwitchableOutput/setpoint/Settings/DimmingMin'],
                          sw['/SwitchableOutput/setpoint/Settings/DimmingMax']), (0.0, 8.0))   # from the fridge
        self.assertEqual((sw['/SwitchableOutput/setpoint2/Settings/DimmingMin'],
                          sw['/SwitchableOutput/setpoint2/Settings/DimmingMax']), (-18.0, -10.0))  # config wins
        zone = fridge.Zone(3.0, 4.0, None, None)                  # a fridge that reports no range
        self.assertEqual(d._range(0, zone), (-20.0, 20.0))
        self.assertEqual(d._range(0, fridge.Zone(3.0, 4.0, 9.0, 2.0)), (-20.0, 20.0))   # min above max

    def test_fridge_controls(self):
        d = FridgeDriver('s', section('mac = 11:22:33:44:55:66\n'))
        self.assertIsNone(d.switch)                           # made once the zones are known
        self.assertFalse(d._request('powered_on', False))     # nothing read yet

        for part in fridge.chunks(dual_zone_status()):
            d._on_notify(part)
        # the GUI's switch pane only lists com.victronenergy.switch services
        sw = d.switch.service
        self.assertEqual(sw.name, 'com.victronenergy.switch.fridge_controls')
        self.assertEqual(sw['/SwitchableOutput/power/Settings/Type'], 1)      # toggle
        self.assertEqual(sw['/SwitchableOutput/setpoint/Settings/Type'], 3)   # temp setpoint
        self.assertEqual(sw['/SwitchableOutput/setpoint2/Name'], 'Freezer set point')
        self.assertIn('/SwitchableOutput/eco/State', sw.paths_at_register)
        self.assertNotIn('/SwitchableOutput/power/State', d.zones[0].service.paths_at_register)
        self.assertEqual((sw['/SwitchableOutput/setpoint2/Settings/DimmingMin'],
                          sw['/SwitchableOutput/setpoint2/Settings/DimmingMax']), (-22.0, -12.0))
        self.assertEqual((sw['/SwitchableOutput/setpoint2/Dimming'],
                          sw['/SwitchableOutput/setpoint2/Measurement']), (-18.0, -17.0))

        sent = []
        d.gatt.exchange = lambda chunks, cb: sent.append(b''.join(chunks))
        # freezer set point below the fridge's range is clamped to its minimum
        d.zones[1].service  # created by the status above
        status = d._status
        self.assertTrue(d._request_setpoint(1, -40))
        self.assertTrue(d._request('powered_on', False))
        d._send()
        self.assertEqual(sent[-1], fridge.set_others_frame(status.raw, powered_on=False) +
                         fridge.set_target_frame(1, status.zones[1].min) + fridge.query_frame())
        self.assertEqual(d._want, {})

    def test_fridge_control_off(self):
        d = FridgeDriver('s', section('mac = 11:22:33:44:55:66\ncontrol = no\n'))
        self.assertNotIn('/SwitchableOutput/power/State', d.zones[0].service)

    def test_fridge_requires_mac(self):
        with self.assertRaises(SystemExit):
            FridgeDriver('s', section('poll_interval = 30\n'))


class GlobalPauseTests(unittest.TestCase):
    def test_pause_file(self):
        import tempfile
        import ble_scanner
        orig = ble_scanner.PAUSE_FILE
        ble_scanner.PAUSE_FILE = os.path.join(tempfile.mkdtemp(), 'pause')
        try:
            self.assertFalse(ble_scanner.global_pause_active())
            ble_scanner.request_global_pause(30)
            self.assertTrue(ble_scanner.global_pause_active())
            ble_scanner.clear_global_pause()
            self.assertFalse(ble_scanner.global_pause_active())
            ble_scanner.request_global_pause(-1)          # expired = no pause
            self.assertFalse(ble_scanner.global_pause_active())
        finally:
            ble_scanner.PAUSE_FILE = orig


class VenusPythonTests(unittest.TestCase):
    """Venus OS ships a trimmed Python: only import modules known to exist there."""
    AVAILABLE = {
        # checked on Venus OS 3.79 (Raspberry Pi) by running the diagnostics
        'argparse', 'collections', 'configparser', 'functools', 'glob', 'inspect',
        'logging', 'math', 'os', 're', 'signal', 'struct', 'subprocess', 'sys', 'time',
        'dbus', 'gi',
        # not verified on a Venus OS image, but every Victron Python service (MQTT, Modbus,
        # VRM logger) needs it; the diagnostics check that it imports before web_ui is enabled
        'socket',
    }

    def test_only_available_modules(self):
        import ast
        drivers = os.path.join(HERE, '..', 'data', 'shared_drivers')
        local = {f[:-3] for f in os.listdir(drivers) if f.endswith('.py')}
        local |= {'vedbus', 'settingsdevice', 've_utils'}
        files = [os.path.join(drivers, f) for f in os.listdir(drivers) if f.endswith('.py')]
        diag = os.path.join(drivers, 'diagnostics')
        files += [os.path.join(diag, f) for f in os.listdir(diag) if f.endswith('.py')]
        for path in files:
            with open(path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    top = name.split('.')[0]
                    self.assertTrue(top in self.AVAILABLE or top in local,
                                    '%s imports %r, which Venus OS may not have'
                                    % (os.path.basename(path), name))

    def test_median(self):
        from mopeka_venus import median
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)


if __name__ == '__main__':
    unittest.main()
