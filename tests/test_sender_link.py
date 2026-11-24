"""
Bluetooth link to the ESP32 water sender (firmware 2.7+): check-in frames,
reply assembly, what the driver publishes, the firmware updater's checks and
the settings page's sender API.
"""

import configparser
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import fakes  # noqa: E402,F401
import esp32_hydro_protocol as proto  # noqa: E402
import esp32_ota  # noqa: E402
from esp32_hydro_tank import Esp32HydroDriver  # noqa: E402

ROOT = os.path.join(HERE, '..')


def section(text):
    cp = configparser.ConfigParser()
    cp.read_string('[s]\n' + text)
    return cp['s']


class ProtocolTests(unittest.TestCase):
    def test_check_in_frames(self):
        self.assertEqual(proto.link_commands('pw', 'auto'),
                         [b'Cauth pw', b'Chb', b'Cwifi mode auto', b'Cinfo'])
        self.assertEqual(proto.link_commands('pw', 'van', 'Camper', 's3cret'),
                         [b'Cauth pw', b'Chb', b'Cwifi mode van', b'Cwifi ssid Camper', b'Cwifi pass s3cret', b'Cinfo'])
        self.assertEqual(proto.link_commands('pw', 'nonsense'), [b'Cauth pw', b'Chb', b'Cinfo'])
        self.assertEqual(proto.link_commands('pw', 'van')[3], b'Cwifi ssid -')      # empty name clears it

    def test_replies_are_joined_across_chunks(self):
        c = proto.ReplyCollector()
        self.assertEqual(c.feed(b'ok\x04{"fw":"2'), ['ok'])
        self.assertEqual(c.feed(b'.7"}'), ['ok'])
        self.assertEqual(c.feed(b'\x04'), ['ok', '{"fw":"2.7"}'])


class DriverLinkTests(unittest.TestCase):
    INFO = {'fw': '2.7', 'id': 1, 'mode': 'van', 'ap': 0, 'sta': 1, 'ip': '192.168.1.60',
            'ssid': 'Camper', 'up': 100, 'pi_ago': 3, 'tn': ['Inside', '']}

    def test_info_is_published(self):
        d = Esp32HydroDriver('s', section('wifi_mode = van\nwifi_ssid = Camper\n'))
        d.apply_info(json.dumps(self.INFO))
        s = d.tank.service
        self.assertEqual((s['/FirmwareVersion'], s['/Hydro/WifiMode'], s['/Hydro/AccessPoint'], s['/Hydro/WifiIp']),
                         ('2.7', 'van', 0, '192.168.1.60'))
        self.assertEqual((s['/Hydro/LinkStatus'], json.loads(s['/Hydro/TempNames'])), ('ok', ['Inside', '']))

    def test_check_in_outcomes(self):
        d = Esp32HydroDriver('s', section(''))
        s = d.tank.service
        d._collector = proto.ReplyCollector()
        d._expected = 3
        d._collector.feed(b'denied: auth first\x04denied: auth first\x04{"fw":"2.7"}\x04')
        d._on_link_done(True, None)
        self.assertEqual(s['/Hydro/LinkStatus'], 'wrong sender password')
        d._collector = proto.ReplyCollector()
        d._on_link_done(False, 'GATT characteristics 0002 / 0003 not found - different fridge protocol?')
        self.assertIn('older than 2.7', s['/Hydro/LinkStatus'])
        d._collector = proto.ReplyCollector()
        d._collector.feed(b'ok\x04ok wifi mode auto\n\x04' + json.dumps(self.INFO).encode() + b'\x04')
        d._on_link_done(True, None)
        self.assertEqual((s['/Hydro/LinkStatus'], s['/FirmwareVersion']), ('ok', '2.7'))

    def test_the_sender_address_is_learned_from_its_broadcast(self):
        d = Esp32HydroDriver('s', section(''))
        d.handle_advertisement('AA:BB:CC:DD:EE:01', {0xFFFF: proto.build(1, 50, 37, 75, 200, sequence=1)}, -60)
        self.assertEqual((d.seen_mac, d.tank.service['/Hydro/Mac']), ('AA:BB:CC:DD:EE:01', 'AA:BB:CC:DD:EE:01'))
        d.handle_advertisement('AA:BB:CC:DD:EE:02', {0xFFFF: proto.build(9, 50, 37, 75, 200, sequence=2)}, -60)
        self.assertEqual(d.seen_mac, 'AA:BB:CC:DD:EE:01')                  # other sender id ignored


class LiveViewTests(unittest.TestCase):
    class FakeClient(object):
        def __init__(self):
            self.busy, self.keep_connected, self.sent = False, False, []

        def exchange(self, frames, done):
            self.sent.append((list(frames), self.keep_connected))
            self.done = done

        def finish(self):
            self.done(True, None)

    def driver(self):
        d = Esp32HydroDriver('s', section(''))
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        d.gatt = self.FakeClient()
        d._client = lambda: d.gatt
        return d

    def test_live_view_reads_the_raw_signal_and_stops_by_itself(self):
        d = self.driver()
        self.assertTrue(d._on_live_set('/Hydro/Live', 1))
        self.assertTrue(d._live_poll())
        frames, keep = d.gatt.sent[-1]
        self.assertEqual(frames, [b'Cauth watertank', b'Chb', b'Cfast', b'Clive'])
        self.assertTrue(keep)                                              # stays connected between polls
        d._on_link_notify(b'ok\x04Test mode on\x04{"s":0.512,"u":"V","raw":310.4,"h":305.2,"l":15.1,"fault":0,"fast":899}\x04')
        s = d.tank.service
        self.assertEqual((s['/Hydro/Signal'], s['/Hydro/SignalUnit'], s['/Hydro/RawHeightMm'], s['/Hydro/LiveLitres']),
                         (0.512, 'V', 310.4, 15.1))
        self.assertIsNotNone(s['/Hydro/LiveTime'])
        d.live_until = fakes_now() - 1                                     # 15 minutes later
        self.assertFalse(d._live_poll())
        frames, keep = d.gatt.sent[-1]
        self.assertEqual((frames[-1], keep, s['/Hydro/Live'], d.live_until), (b'Cfast off', False, 0, 0))

    def test_live_view_needs_a_sender(self):
        d = Esp32HydroDriver('s', section(''))
        self.assertFalse(d._on_live_set('/Hydro/Live', 1))                 # nothing received yet
        self.assertFalse(self.driver()._on_live_set('/Hydro/Live', 'x'))


def fakes_now():
    import venus_service
    return venus_service.monotonic_s()


class LockTests(unittest.TestCase):
    def test_lock_only_counts_while_the_updater_lives(self):
        d = tempfile.mkdtemp()
        lock = os.path.join(d, 'lock')
        self.assertIsNone(proto.ota_running(lock))                        # no file
        with open(lock, 'w') as f:
            f.write('%d esp32_hydro\n' % os.getpid())
        self.assertEqual(proto.ota_running(lock), os.getpid())
        with open(lock, 'w') as f:
            f.write('999999\n')                                           # dead pid: stale
        self.assertIsNone(proto.ota_running(lock))
        with open(lock, 'w') as f:
            f.write('garbage')
        self.assertIsNone(proto.ota_running(lock))

    def test_the_driver_waits_for_a_running_update(self):
        import esp32_hydro_tank
        calls = {'n': 0}

        def running(path=None):
            calls['n'] += 1
            return 123 if calls['n'] < 3 else None                      # finishes after two checks
        orig, proto.ota_running = proto.ota_running, running
        sleep, esp32_hydro_tank.time.sleep = esp32_hydro_tank.time.sleep, lambda s: None
        try:
            esp32_hydro_tank.wait_for_sender_update()
            self.assertEqual(calls['n'], 3)
        finally:
            proto.ota_running, esp32_hydro_tank.time.sleep = orig, sleep


class UpdaterTests(unittest.TestCase):
    def image(self, b27=0x3C):
        data = bytearray(8192)
        data[0], data[27] = 0xE9, b27
        return bytes(data)

    def test_image_check(self):
        self.assertIsNone(esp32_ota.check_image(self.image()))
        self.assertIn('FLASH-AT-0x0', esp32_ota.check_image(self.image(0x3F)))     # bootloader first = full image
        self.assertIn('not an ESP32', esp32_ota.check_image(b'\x00' * 8192))
        self.assertIn('too small', esp32_ota.check_image(b'\xe9'))

    def test_packet_size(self):
        self.assertEqual(esp32_ota.packet_size(517), 499)
        self.assertEqual(esp32_ota.packet_size(185), 181)
        self.assertEqual(esp32_ota.packet_size(0), 499)             # unknown: BlueZ splits (long write)
        self.assertEqual(list(esp32_ota.chunks(b'abcdefg', 3)), [b'abc', b'def', b'g'])

    def test_bluetooth_update_sequence(self):
        """A simulated sender: auth, begin, data in order, end that needs a moment."""
        test = self

        class FakeLink(object):
            def __init__(self, mac, adapter):
                self.mtu, self.got, self.log, self.pending = 247, b'', [], []

            def connect(self):
                pass

            def disconnect(self):
                self.log.append('disconnect')

            def write(self, data):
                if data[:1] == b'D':
                    self.got += data[1:]
                    test.assertLessEqual(len(data), 244)          # fits one packet (MTU 247 - 3)

            def command(self, text):
                self.log.append(text.split(' ')[0] + ' ' + text.split(' ')[1] if ' ' in text else text)
                if text.startswith('auth'):
                    return 'ok auth' if text == 'auth pw' else 'denied: wrong password'
                if text.startswith('ota begin'):
                    return 'ok ota begin'
                self.pending = ['working', 'ok ota done, restarting']
                return 'working'

            def read(self):
                return self.pending.pop(0)

        made = []
        orig, esp32_ota.BleLink = esp32_ota.BleLink, lambda mac, adapter: made.append(FakeLink(mac, adapter)) or made[-1]
        sleep, esp32_ota.time.sleep = esp32_ota.time.sleep, lambda s: None
        try:
            data = self.image() * 30
            esp32_ota.ble_update('AA:BB', 'pw', data, 'm' * 32)
            self.assertEqual(made[-1].got, data)
            self.assertEqual(made[-1].log, ['auth pw', 'ota begin', 'ota end', 'disconnect'])
            with self.assertRaises(RuntimeError):
                esp32_ota.ble_update('AA:BB', 'wrong', data, 'm' * 32)
            self.assertEqual(made[-1].log[-1], 'disconnect')                  # always disconnects
        finally:
            esp32_ota.BleLink, esp32_ota.time.sleep = orig, sleep

    def test_shipped_files_match_the_sketch(self):
        with open(os.path.join(ROOT, 'firmware', 'esp32_hydro_tank', 'esp32_hydro_tank.ino')) as f:
            sketch = f.read()
        version = re.search(r'#define FW_VERSION\s+"([^"]+)"', sketch).group(1)
        with open(os.path.join(ROOT, 'firmware', 'bin', 'VERSION')) as f:
            self.assertEqual(f.read().strip(), version)
        with open(os.path.join(ROOT, 'firmware', 'bin', 'WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin'), 'rb') as f:
            app = f.read()
        self.assertIsNone(esp32_ota.check_image(app))
        self.assertIn(version.encode(), app)                                 # the binary is built from this sketch
        for uuid in (proto.LINK_SERVICE_UUID, proto.LINK_CMD_UUID, proto.LINK_REPLY_UUID):
            self.assertIn(uuid, sketch)


if __name__ == '__main__':
    unittest.main()
