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

    def test_wifi_off_is_sent(self):
        self.assertEqual(proto.link_commands('pw', 'off'), [b'Cauth pw', b'Chb', b'Cwifi mode off', b'Cinfo'])

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
        self.assertEqual(s['/Hydro/LinkStatus'], 'wrong sensor password')
        d._collector = proto.ReplyCollector()
        d._on_link_done(False, 'GATT characteristics 0002 / 0003 not found - different fridge protocol?')
        self.assertIn('older than 2.7', s['/Hydro/LinkStatus'])
        d._collector = proto.ReplyCollector()
        d._collector.feed(b'ok\x04ok wifi mode auto\n\x04' + json.dumps(self.INFO).encode() + b'\x04')
        d._on_link_done(True, None)
        self.assertEqual((s['/Hydro/LinkStatus'], s['/FirmwareVersion']), ('ok', '2.7'))

    def test_status_says_when_nothing_was_received(self):
        d = Esp32HydroDriver('s', section(''))
        d.start = Esp32HydroDriver.start.__get__(d)
        d.start()
        self.assertIn('no broadcast', d.tank.service['/Hydro/LinkStatus'])
        d._link_tick()
        self.assertIn('no broadcast', d.tank.service['/Hydro/LinkStatus'])

    def test_old_firmware_is_tried_only_once_an_hour(self):
        d = Esp32HydroDriver('s', section(''))
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        sent = []

        class Client(object):
            busy = False

            def exchange(inner, frames, done):
                sent.append(frames)
                done(False, 'GATT characteristics 0002 / 0003 not found')
        d._client = lambda: Client()
        d.gatt = Client()
        d._link_tick()
        d._link_tick()                                          # 5 minutes later: not again
        self.assertEqual(len(sent), 1)
        self.assertIn('older than 2.7', d.tank.service['/Hydro/LinkStatus'])

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

    class FakeSender(object):
        """Behaves like the firmware: v2 chunks with offsets, or v1 (before 3.0)."""

        def __init__(self, v2=True, fail_every=0, drop_at=None):
            self.v2, self.fail_every, self.drop_at = v2, fail_every, drop_at
            self.got, self.active, self.authed, self.connected = b'', False, False, False
            self.writes, self.log, self.reply, self.mtu = 0, [], '', 0

        # BleLink interface
        def connect(self):
            self.connected, self.authed = True, False
            self.log.append('connect')

        def disconnect(self):
            self.connected = False
            self.log.append('disconnect')

        def write(self, data):
            import struct
            if not self.connected:
                raise RuntimeError('Not connected')
            self.writes += 1
            if data[:1] == b'C':
                self.reply = self.handle(data[1:].decode())
                return
            if self.drop_at is not None and len(self.got) >= self.drop_at:
                self.drop_at = None
                self.connected = False
                raise RuntimeError('org.bluez.Error.Failed: Not connected')
            if self.v2:
                off = struct.unpack('<I', data[1:5])[0]
                part = data[5:]
                if off + len(part) > len(self.got) and off == len(self.got):
                    self.got += part
            else:
                self.got += data[1:]
            if self.fail_every and self.writes % self.fail_every == 0:
                raise RuntimeError('org.bluez.Error.Failed: Operation failed with ATT error: 0x0e')   # kept, but reported

        def read(self):
            return self.reply

        def command(self, text):
            self.write(b'C' + text.encode())
            return self.read()

        def handle(self, cmd):
            self.log.append(cmd.split(' ')[0] + (' ' + cmd.split(' ')[1] if ' ' in cmd else ''))
            if cmd.startswith('auth'):
                self.authed = cmd == 'auth pw'
                return 'ok auth' if self.authed else 'denied: wrong password'
            if cmd.startswith('ota begin2'):
                if not self.v2:
                    return 'ok auth'                 # old firmware: queued, the reply is still the last one
                self.active = True
                return 'ok ota begin v2 mtu=247'
            if cmd.startswith('ota begin'):
                self.active = True
                return 'ok ota begin'
            if cmd == 'ota status':
                return 'ota active %d/0' % len(self.got) if self.active else 'ota idle 0/0'
            return 'ok ota done, restarting'

    def run_update(self, sender, password='pw'):
        orig, esp32_ota.BleLink = esp32_ota.BleLink, lambda mac, adapter: sender
        sleep, esp32_ota.time.sleep = esp32_ota.time.sleep, lambda s: None
        say, esp32_ota.say = esp32_ota.say, lambda text: None
        try:
            data = bytes(range(256)) * 200
            data = self.image()[:28] + data[28:]
            esp32_ota.ble_update('AA:BB', password, data, 'm' * 32)
            return data
        finally:
            esp32_ota.BleLink, esp32_ota.time.sleep, esp32_ota.say = orig, sleep, say

    def test_update_v2_survives_refused_packets_and_a_dropped_connection(self):
        sender = self.FakeSender(fail_every=37, drop_at=20000)
        data = self.run_update(sender)
        self.assertEqual(sender.got, data)                                  # nothing lost, nothing twice
        self.assertGreaterEqual(sender.log.count('connect'), 2)             # reconnected once
        self.assertIn('ota status', sender.log)
        self.assertEqual(sender.log[-2:], ['ota end', 'disconnect'])

    def test_update_old_firmware_uses_one_frame_packets(self):
        sender = self.FakeSender(v2=False)
        data = self.run_update(sender)
        self.assertEqual(sender.got, data)
        self.assertIn('ota begin', sender.log)
        self.assertEqual(esp32_ota.v2_packet_size(247), 239)
        self.assertEqual(esp32_ota.v2_packet_size(None), 16)
        self.assertEqual((esp32_ota.parse_status('ota active 5/9'), esp32_ota.parse_status('ota idle 0/0')), (5, None))

    def test_update_wrong_password_stops_at_once(self):
        sender = self.FakeSender()
        with self.assertRaises(RuntimeError):
            self.run_update(sender, password='nope')
        self.assertEqual(sender.log[-1], 'disconnect')
        self.assertEqual(sender.got, b'')

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
