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
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import fakes  # noqa: E402,F401
import esp32_hydro_protocol as proto  # noqa: E402
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

    def test_a_dropped_link_is_tried_again_in_a_minute(self):
        import esp32_hydro_tank
        later = []
        orig = esp32_hydro_tank.GLib.timeout_add_seconds
        esp32_hydro_tank.GLib.timeout_add_seconds = lambda s, f, *a: later.append((s, f)) or 1
        try:
            d = Esp32HydroDriver('s', section(''))
            d._collector = proto.ReplyCollector()
            d._expected = 3
            d._on_link_done(False, 'device disconnected')
            d._on_link_done(False, 'device disconnected')               # only one retry waiting
        finally:
            esp32_hydro_tank.GLib.timeout_add_seconds = orig
        self.assertEqual([s for s, f in later], [60])
        self.assertEqual(d.tank.service['/Hydro/LinkStatus'], 'device disconnected')

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

    def test_live_reading_keeps_the_tank_connected(self):
        # while the live connection is open the Pi hears no broadcasts: the live reading carries the level
        d = self.driver()
        d.tank.capacity_m3 = 0.1
        d._on_live_set('/Hydro/Live', 1)
        d._live_poll()
        d._on_link_notify(b'ok\x04Test mode on\x04{"s":0.512,"u":"V","raw":310.4,"h":305.2,"l":15.1,"fault":0,"fast":899}\x04')
        s = d.tank.service
        self.assertEqual((s['/Connected'], s['/Level'], s['/Hydro/HeightMm']), (1, 15.1, 305))

    def test_silent_sensor_with_other_devices_heard_touches_nothing(self):
        import venus_service
        d = self.driver()
        calls = []

        class Scanner(object):
            others = True

            def heard_any_within(inner, s):
                return inner.others

            def restart(inner):
                calls.append('restart')

            def power_cycle_if_dead(inner):
                calls.append('power')
                return 'connected to the fridge'
        d.scanner = Scanner()
        d._scan_watch()
        self.assertEqual(calls, [])                                        # never heard: nothing to do
        d._last_adv = venus_service.monotonic_s() - 120
        for _ in range(6):
            d._scan_watch()
        self.assertEqual(calls, [])                                        # others heard: sensor off / out of range
        d.scanner.others = False                                           # nothing at all heard: the scan is dead
        d._silent_kicks = 0
        for _ in range(6):
            d._scan_watch()
        self.assertEqual(calls, ['restart', 'restart', 'restart', 'power'])  # own scan first, adapter last (guarded)
        import rawlog
        self.assertIn('Bluetooth adapter not reset: connected to the fridge', '\n'.join(rawlog.read_tail('link')))   # why, in the log
        d.live_until = venus_service.monotonic_s() + 60
        d._scan_watch()
        self.assertEqual(len(calls), 4)                                    # not while live holds the connection

    def test_failing_live_view_waits_and_gives_up(self):
        d = self.driver()
        d._on_live_set('/Hydro/Live', 1)
        d._live_poll()
        d.gatt.done(False, 'device not known to BlueZ yet')
        n = len(d.gatt.sent)
        d._live_poll()
        self.assertEqual(len(d.gatt.sent), n)                              # waits after a failure, no flood
        for _ in range(d.LIVE_MAX_FAILS - 1):                               # 20 in a row with the first
            d._live_pause_until = 0
            d._live_poll()
            d.gatt.done(False, 'device not known to BlueZ yet')
        self.assertEqual((d.live_until, d.tank.service['/Hydro/Live']), (0, 0))   # stopped by itself
        self.assertIn('live view stopped', d.tank.service['/Hydro/LinkStatus'])

    def test_live_view_needs_a_sender(self):
        d = Esp32HydroDriver('s', section(''))
        self.assertFalse(d._on_live_set('/Hydro/Live', 1))                 # nothing received yet
        self.assertFalse(self.driver()._on_live_set('/Hydro/Live', 'x'))


def fakes_now():
    import venus_service
    return venus_service.monotonic_s()


class CommandTests(unittest.TestCase):
    """The firmware update: the page asks the driver, the driver sends "fwget" over Bluetooth."""

    class Client(object):
        busy, keep_connected = False, False

        def __init__(self):
            self.sent = []

        def exchange(self, frames, done):
            self.sent.append(list(frames))
            self.done = done

        def finish(self):
            self.done(True, None)

    def driver(self, cfg='wifi_ssid = Camper\nwifi_password = s3cret\n'):
        d = Esp32HydroDriver('s', section(cfg))
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        d.gatt = self.Client()
        d._client = lambda: d.gatt
        return d

    def test_fwget_carries_the_van_wifi_from_the_settings(self):
        d = self.driver()
        self.assertTrue(d._on_command_set('/Hydro/Command', 'fwget http://192.168.1.5:8088/fw/abc.bin'))
        d._send_command()
        self.assertEqual(d.gatt.sent[-1], [b'Cauth watertank', b'Cfwget http://192.168.1.5:8088/fw/abc.bin\tCamper\ts3cret'])
        d._on_link_notify(b'ok fwget: joining the Wi-Fi and downloading\x04')
        self.assertTrue(d.tank.service['/Hydro/CommandResult'].startswith('ok fwget'))

    def test_sensor_dropping_the_upload_ends_it_without_starting_over(self):
        d = self.driver()
        d.ota = {'data': b'x' * 2000, 'md5': '', 'phase': 'data', 'pos': 0, 'chunk': 180, 'errors': 0, 'restarts': 0}
        d._ota_answer(True, None, ['ota idle'])
        self.assertIsNone(d.ota)
        self.assertIn('dropped the upload', d.tank.service['/Hydro/CommandResult'])

    def test_cancel_stops_the_bluetooth_upload_and_tells_the_sensor(self):
        d = self.driver()
        d.ota = {'data': b'x', 'md5': '', 'phase': 'data', 'pos': 0, 'chunk': 180, 'errors': 0, 'restarts': 0}
        self.assertTrue(d._on_command_set('/Hydro/Command', 'ota cancel'))
        self.assertIsNone(d.ota)
        d._send_command()
        self.assertEqual(d.gatt.sent[-1][-1], b'Cota abort')

    def test_fwget_without_wifi_settings_uses_the_sensors_own_network(self):
        d = self.driver(cfg='')
        d._on_command_set('/Hydro/Command', 'fwget http://192.168.1.5:8088/fw/abc.bin')
        d._send_command()
        self.assertEqual(d.gatt.sent[-1][-1], b'Cfwget http://192.168.1.5:8088/fw/abc.bin')

    def test_wifi_command_and_the_sensors_networks(self):
        d = self.driver(cfg='')
        d._on_command_set('/Hydro/Command', 'wifi join MOS\tpw')
        d._send_command()
        self.assertEqual(d.gatt.sent[-1][-1], b'Cwifi join MOS\tpw')
        d._on_link_notify(b"ok joining 'MOS'\x04")
        self.assertEqual(d.tank.service['/Hydro/CommandResult'], "ok joining 'MOS'")
        d.apply_info('{"fw":"4.5","mode":"van","ap":0,"ip":"192.168.1.60","ssid":"MOS","kn":["MOS","Home"]}')
        self.assertEqual(d.tank.service['/Hydro/WifiSsid'], 'MOS')
        self.assertEqual(json.loads(d.tank.service['/Hydro/WifiKnown']), ['MOS', 'Home'])

    def test_no_bluetooth_while_the_sensor_downloads_over_wifi(self):
        d = self.driver()
        d._on_command_set('/Hydro/Command', 'fwget http://192.168.1.5:8088/fw/abc.bin')
        d._send_command()
        d._on_link_notify(b'ok fwget: joining the Wi-Fi and downloading\x04')
        n = len(d.gatt.sent)
        d._link_tick()
        self.assertEqual(len(d.gatt.sent), n)                              # no check-in during the download

    def test_command_retried_when_the_connection_broke_before_it_went_out(self):
        d = self.driver(cfg='')
        d._on_command_set('/Hydro/Command', 'reboot')
        d._send_command()
        d.gatt.frames_sent = 1                                             # only auth went out
        d.gatt.done(False, 'StartNotify failed: Not Connected')
        self.assertEqual(d._command, 'reboot')                             # queued again
        self.assertIsNone(d.tank.service['/Hydro/CommandResult'])
        d._send_command()
        d.gatt.frames_sent = 0
        d.gatt.done(False, 'device not known to BlueZ yet')                # second try
        d._send_command()
        d.gatt.done(False, 'device not known to BlueZ yet')                # third and last try
        self.assertIsNone(d._command)
        self.assertIn('device not known', d.tank.service['/Hydro/CommandResult'])

    def test_sensor_log_lines_and_hourly_stats_go_to_the_persistent_log(self):
        import rawlog
        d = self.driver(cfg='')
        rawlog.clear('link')
        d._link_tick()
        self.assertIn(b'Cdlog new', d.gatt.sent[-1])
        d._on_link_notify(b'ok\x04ok wifi mode auto\x04dlog new\n00:01:02 BT connected\n00:01:03 auth ok\x04{"fw":"4.9","mode":"auto"}\x04')
        d._log_stats()
        text = '\n'.join(rawlog.read_tail('link'))
        self.assertIn('sensor 00:01:02 BT connected', text.replace('  ', ' '))
        self.assertIn('last hour', text)
        self.assertEqual(d.tank.service['/FirmwareVersion'], '4.9')        # info still read from the last answer
        self.assertTrue(rawlog.path_of('link').startswith(rawlog.PERSIST_DIR))   # kept across reboots

    def test_total_silence_records_the_system_state(self):
        import rawlog
        import venus_service
        d = self.driver()
        d.SNAPSHOT_CMDS = (('echo', ['echo', 'hci0: UP RUNNING']), ('none', ['no-such-command-here']))
        d.scanner = type('S', (), {'heard_any_within': lambda inner, s: False, 'restart': lambda inner: None,
                                   'power_cycle_if_dead': lambda inner: 'test'})()
        d._last_adv = venus_service.monotonic_s() - 120
        d._scan_watch()
        text = '\n'.join(rawlog.read_tail('link'))
        self.assertIn('nothing heard for 120 s: system state', text)
        self.assertIn('hci0: UP RUNNING', text)
        self.assertIn('not available', text)
        n = len(rawlog.read_tail('link'))
        d._scan_watch()
        self.assertEqual(len([l for l in rawlog.read_tail('link')[n:] if 'system state' in l]), 0)   # once per silence

    def test_link_log_hides_passwords(self):
        t = Esp32HydroDriver.frames_text([b'Cauth secret', b'Cwifi pass hunter2', b'Cwifi join MOS\tpw', b'D\x00\x00\x00\x00ab', b'Cinfo'])
        self.assertNotIn('secret', t)
        self.assertNotIn('hunter2', t)
        self.assertNotIn('pw', t.replace('password', ''))
        self.assertIn('1 data packets from byte 0', t)

    def test_command_answers_and_refusals(self):
        d = self.driver()
        self.assertFalse(Esp32HydroDriver('s', section(''))._on_command_set('/Hydro/Command', 'fwget http://x'))   # no sensor heard yet
        d._on_command_set('/Hydro/Command', 'fwget http://x/fw/1.bin')
        d._send_command()
        d._on_link_notify(b'denied: auth first\x04')
        self.assertEqual(d.tank.service['/Hydro/CommandResult'], 'wrong sensor password')
        d._on_command_set('/Hydro/Command', 'fwget http://x/fw/1.bin')
        d._send_command()
        d.gatt.done(False, 'device disconnected')
        self.assertIn('device disconnected', d.tank.service['/Hydro/CommandResult'])

    def test_firmware_error_from_the_check_in(self):
        d = self.driver()
        d.apply_info(json.dumps({'fw': '4.1', 'mode': 'auto', 'ap': 0, 'sta': 0, 'ip': '', 'fw_err': "could not join the Wi-Fi 'Camper'"}))
        self.assertEqual(d.tank.service['/Hydro/FwError'], "could not join the Wi-Fi 'Camper'")


class BluetoothOtaTests(unittest.TestCase):
    """The driver's firmware upload against a simulated sensor that behaves like firmware 4.2."""

    class Sensor(object):
        def __init__(self, fail_every=0, keep_on_fail=True, drop_round=None, mtu=185):
            self.busy, self.keep_connected = False, False
            self.got, self.active, self.size, self.rounds = b'', False, 0, 0
            self.fail_every, self.keep_on_fail, self.drop_round, self.mtu = fail_every, keep_on_fail, drop_round, mtu
            self.packet_sizes, self.installed = set(), None

        def exchange(self, frames, done):
            import struct
            self.rounds += 1
            replies = []
            authed = False
            for i, f in enumerate(frames):
                if self.drop_round == self.rounds and i == len(frames) // 2:
                    done(False, 'device disconnected')                  # connection lost half-way
                    return
                if f.startswith(b'Cauth '):
                    authed = f == b'Cauth watertank'
                elif f.startswith(b'D'):
                    self.packet_sizes.add(len(f))
                    off = struct.unpack('<I', f[1:5])[0]
                    if self.active and authed and off == len(self.got):
                        self.got += f[5:]
                else:
                    cmd = f[1:].decode()
                    if not authed:
                        replies.append('denied: auth first')
                    elif cmd.startswith('ota begin'):
                        self.active, self.got, self.size = True, b'', int(cmd.split()[2])
                        replies.append('ok ota begin mtu=%d' % self.mtu)
                    elif cmd == 'ota status':
                        replies.append('ota active %d/%d' % (len(self.got), self.size))
                    elif cmd == 'ota end':
                        self.installed = self.got
                        replies.append('ok ota done, restarting')
            if self.fail_every and self.rounds % self.fail_every == 0:
                done(False, 'Operation failed with ATT error: 0x0e')      # an answer that never came
                return
            self.reply_queue = replies
            self.done = done
            for r in replies:
                self.notify(r.encode() + b'\x04')

        def finish(self):
            self.done(True, None)

    def run_ota(self, sensor, size=60000):
        import esp32_hydro_tank, tempfile
        d = Esp32HydroDriver('s', section(''))
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        d.gatt = sensor
        d._client = lambda: sensor
        sensor.notify = d._on_link_notify
        queue = []
        saved = (esp32_hydro_tank.GLib.timeout_add, esp32_hydro_tank.GLib.timeout_add_seconds)
        esp32_hydro_tank.GLib.timeout_add = lambda ms, f, *a: queue.append(f) or 1
        esp32_hydro_tank.GLib.timeout_add_seconds = lambda s, f, *a: queue.append(f) or 1
        try:
            data = bytearray(os.urandom(size))
            data[0], data[27] = 0xE9, 0x3C
            path = os.path.join(tempfile.mkdtemp(), 'fw.bin')
            with open(path, 'wb') as f:
                f.write(data)
            self.assertTrue(d._on_command_set('/Hydro/Command', 'ota ' + path))
            for _ in range(5000):                                   # run the timers until it settles
                if not queue:
                    break
                f = queue.pop(0)
                if getattr(f, '__name__', '') == '<lambda>':
                    continue                                         # the version check-ins after the end
                f()
        finally:
            esp32_hydro_tank.GLib.timeout_add, esp32_hydro_tank.GLib.timeout_add_seconds = saved
        return d, bytes(data)

    def test_upload_survives_refused_rounds_and_a_dropped_connection(self):
        sensor = self.Sensor(fail_every=7, drop_round=12)
        d, data = self.run_ota(sensor)
        self.assertEqual(sensor.installed, data)                     # complete, nothing twice
        self.assertEqual(d.tank.service['/Hydro/CommandResult'], 'ok ota done, restarting')
        self.assertEqual(d.tank.service['/Hydro/OtaProgress'], 100)
        self.assertLessEqual(max(sensor.packet_sizes), 185 - 3)     # every packet fits one frame
        self.assertIsNone(d.ota)

    def test_old_sensor_firmware_gets_a_clear_message(self):
        class Old(BluetoothOtaTests.Sensor):
            def exchange(self, frames, done):
                self.done = done
                self.notify(b"Unknown command 'ota' - type help\x04")
        d, data = self.run_ota(Old())
        self.assertIn('older than 4.2', d.tank.service['/Hydro/CommandResult'])

    def test_small_mtu_still_works(self):
        sensor = self.Sensor(mtu=23)
        d, data = self.run_ota(sensor, size=3000)
        self.assertEqual(sensor.installed, data)
        self.assertEqual(max(sensor.packet_sizes), 20)               # 15 data bytes + 'D' + offset


class WifiPushTests(unittest.TestCase):
    """The Pi sends its Wi-Fi setting only when it changed there: the sensor's own page can change it too."""

    class Client(object):
        busy, keep_connected = False, False

        def __init__(self):
            self.sent = []

        def exchange(self, frames, done):
            self.sent.append(list(frames))

    def driver(self, state, cfg):
        d = Esp32HydroDriver('s', section(cfg))
        d.wifi_state_file = state
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        d.gatt = self.Client()
        d._client = lambda: d.gatt
        return d

    def test_sent_once_then_only_after_a_change(self):
        state = os.path.join(tempfile.mkdtemp(), 'state', 'esp32_hydro.wifi-sent')
        d = self.driver(state, 'wifi_mode = van\nwifi_ssid = Camper\nwifi_password = s3cret\n')
        d._link_tick()
        self.assertIn(b'Cwifi mode van', d.gatt.sent[-1])
        d.apply_info(json.dumps({'fw': '4.3', 'mode': 'van'}))           # the sensor confirmed
        with open(state) as f:
            self.assertNotIn('s3cret', f.read())                          # a checksum, not the password
        d._link_tick()
        self.assertEqual(d.gatt.sent[-1], [b'Cauth watertank', b'Chb', b'Cdlog new', b'Cinfo'])   # left alone
        d2 = self.driver(state, 'wifi_mode = van\nwifi_ssid = Camper\nwifi_password = s3cret\n')
        d2._link_tick()
        self.assertNotIn(b'Cwifi mode van', d2.gatt.sent[-1])             # a Pi restart doesn't resend it
        d3 = self.driver(state, 'wifi_mode = ap\n')                       # changed on the Venus OS page
        d3._link_tick()
        self.assertIn(b'Cwifi mode ap', d3.gatt.sent[-1])

    def test_not_confirmed_is_sent_again(self):
        state = os.path.join(tempfile.mkdtemp(), 'wifi-sent')
        d = self.driver(state, 'wifi_mode = ap\n')
        d._link_tick()
        d._link_tick()                                                    # the first check-in got no answer
        self.assertIn(b'Cwifi mode ap', d.gatt.sent[-1])


class ShippedFilesTests(unittest.TestCase):
    def test_shipped_files_match_the_sketch(self):
        with open(os.path.join(ROOT, 'firmware', 'esp32_hydro_tank', 'esp32_hydro_tank.ino')) as f:
            sketch = f.read()
        version = re.search(r'#define FW_VERSION\s+"([^"]+)"', sketch).group(1)
        with open(os.path.join(ROOT, 'firmware', 'bin', 'VERSION')) as f:
            self.assertEqual(f.read().strip(), version)
        with open(os.path.join(ROOT, 'firmware', 'bin', 'WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin'), 'rb') as f:
            app = f.read()
        self.assertEqual((app[0], app[27]), (0xE9, 0x3C))                    # an app image, not the full flash
        self.assertIn(version.encode(), app)                                 # the binary is built from this sketch
        self.assertIn(b'fwget', app)
        for uuid in (proto.LINK_SERVICE_UUID, proto.LINK_CMD_UUID, proto.LINK_REPLY_UUID):
            self.assertIn(uuid, sketch)


if __name__ == '__main__':
    unittest.main()


class TrumaHeaterTests(unittest.TestCase):
    """Truma heater through the water sensor (in preparation): commands, check-in, Venus OS."""

    def driver(self, cfg='heater = yes\nheater_frost_guard = yes\nheater_frost_c = 5\n'):
        d = Esp32HydroDriver('s', section(cfg))
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        d.gatt = CommandTests.Client()
        d._client = lambda: d.gatt
        return d

    def test_commands(self):
        import truma_heater as th
        self.assertEqual(th.set_command(heat=True, room=21.4, water='eco'), 'truma set heat=on room=21 water=eco')
        self.assertEqual(th.set_command(room=99), 'truma set room=30')
        self.assertIsNone(th.set_command())
        self.assertIsNone(th.parse('{"fw":"4.11"}'))
        self.assertEqual(th.parse('truma {"en":1,"lin":"ok"}')['lin'], 'ok')

    def test_checkin_reads_the_heater_and_publishes_it(self):
        d = self.driver()
        d._link_tick()
        self.assertIn(b'Ctruma', d.gatt.sent[-1])
        self.assertEqual(d.gatt.sent[-1][-1], b'Cinfo')                     # info stays last
        d._on_link_notify(b'ok\x04ok wifi mode auto\x04dlog new\x04truma {"en":1,"lin":"ok","ago":1,"room":3.5,"set":10,'
                          b'"heat":0,"water":"eco","wt":40,"err":0,"pend":0}\x04{"fw":"4.11","mode":"auto"}\x04')
        self.assertEqual(d.heater.room.service['/Temperature'], 3.5)
        sw = d.heater.switch.service
        self.assertEqual((sw['/SwitchableOutput/room/Dimming'], sw['/SwitchableOutput/water/Dimming']), (10, 1))
        self.assertEqual(json.loads(d.tank.service['/Hydro/Truma'])['room'], 3.5)
        self.assertEqual(d._command, 'truma set heat=on room=7')          # frost guard: 3.5 C < 5 C, heating off

    def test_no_lin_data_publishes_nothing_as_measured(self):
        d = self.driver()
        d.heater.apply({'en': 0, 'lin': 'off', 'room': None})
        self.assertIsNone(d.heater.room.service['/Temperature'])
        self.assertEqual(d.heater.switch.service['/SwitchableOutput/heat/Status'], 0x20)

    def test_switch_pane_change_goes_to_the_sensor(self):
        d = self.driver()
        d.heater.request(water='boost')
        d._send_command()
        self.assertEqual(d.gatt.sent[-1][-1], b'Ctruma set water=boost')

    def test_off_by_default(self):
        d = Esp32HydroDriver('s', section(''))
        self.assertIsNone(d.heater)
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        d.gatt = CommandTests.Client()
        d._client = lambda: d.gatt
        d._link_tick()
        self.assertNotIn(b'Ctruma', d.gatt.sent[-1])


class WifiTransportTests(unittest.TestCase):
    """Connection over the Wi-Fi network: a fake sensor web server, the real curl requests."""

    def setUp(self):
        import http.server
        import threading
        test = self
        self.cmds, self.uploads = [], []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, text, ctype='text/plain'):
                b = text.encode()
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                test.assertIn('pi=1', self.path)
                self._send(json.dumps({'version': '4.12', 'height_mm': 305.2, 'litres': 15.1, 'capacity': 100.0,
                                       'level': 15.1, 'fault': False, 'filling': True,
                                       'temps': [{'n': 1, 'c': 7.5}, {'n': 2, 'c': 3.25}]}), 'application/json')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                if self.path.startswith('/update'):
                    test.uploads.append(body)
                    return self._send('Update OK. The sender restarts; reload the page in ~10 s.')
                import urllib.parse
                c = urllib.parse.parse_qs(body.decode())['c'][0]
                test.cmds.append(c)
                self._send('{"fw":"4.12","mode":"van","ip":"127.0.0.1","ssid":"Moss","up":5,"bt":0}' if c == 'info'
                           else 'dlog new' if c == 'dlog new' else 'ok ' + c)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = '127.0.0.1:%d' % self.server.server_address[1]

    def tearDown(self):
        try:
            self.server.shutdown()
        except Exception:
            pass

    def run_loop(self, until, seconds=8):
        """A tiny main loop: runs the timers the driver and run_async set, until until() or time is up."""
        import esp32_hydro_tank
        import starlink_hub
        if not hasattr(self, '_timers'):
            self._timers = []                  # kept across calls: the driver's repeating timers live on
        timers = self._timers

        class Loop(object):
            @staticmethod
            def timeout_add(ms, fn, *args):
                timers.append([time.time() + ms / 1000.0, ms / 1000.0, fn, args])
                return len(timers)

            @staticmethod
            def timeout_add_seconds(sec, fn, *args):
                return Loop.timeout_add(sec * 1000, fn, *args)

            @staticmethod
            def idle_add(fn, *args):
                return Loop.timeout_add(0, fn, *args)

            source_remove = staticmethod(lambda *a: None)
        old = (esp32_hydro_tank.GLib, starlink_hub.GLib)
        esp32_hydro_tank.GLib = starlink_hub.GLib = Loop
        self._loop = Loop
        try:
            for fn in self._pending_start:
                fn()
            self._pending_start = []
            deadline = time.time() + seconds
            while not until() and time.time() < deadline:
                now = time.time()
                for t in [t for t in timers if t[0] <= now]:
                    timers.remove(t)
                    if t[2](*t[3]) and t[1] > 0:
                        t[0] = now + t[1]
                        timers.append(t)
                time.sleep(0.02)
        finally:
            esp32_hydro_tank.GLib, starlink_hub.GLib = old

    _pending_start = []

    def test_reading_checkin_command_and_update_over_wifi(self):
        d = Esp32HydroDriver('s', section('connection = wifi\nsensor_host = %s\ntemperature_service = yes\n' % self.host))
        self.assertFalse(d.USES_SCANNER)                                   # no Bluetooth at all
        self._pending_start = [d.start]
        self.run_loop(lambda: d.tank.service['/Level'] is not None)
        s = d.tank.service
        self.assertEqual((s['/Level'], s['/Hydro/HeightMm'], s['/Hydro/Filling'], s['/Hydro/Transport']), (15.1, 305, 1, 'wifi'))
        self.assertEqual(d.temp.service['/Temperature'], 7.5)
        self._pending_start = [d._link_tick]                               # check-in: hb, wifi push, log, info
        self.run_loop(lambda: s.get('/FirmwareVersion') == '4.12')
        self.assertEqual(s['/FirmwareVersion'], '4.12')
        self.assertEqual(s['/Hydro/LinkStatus'], 'ok')
        self.assertIn('hb', self.cmds)
        self.assertNotIn('auth watertank', ' '.join(self.cmds))             # no password over the web server
        self.assertFalse([c for c in self.cmds if c.startswith('wifi mode')])   # never pushes "Bluetooth only" over Wi-Fi
        self._pending_start = [lambda: d._on_command_set('/Hydro/Command', 'reboot')]
        self.run_loop(lambda: s.get('/Hydro/CommandResult') is not None)
        self.assertEqual(s['/Hydro/CommandResult'], 'ok reboot')
        path = os.path.join(tempfile.mkdtemp(), 'fw.bin')
        image = bytearray(2000)
        image[0], image[27] = 0xE9, 0x3C
        with open(path, 'wb') as f:
            f.write(image)
        self._pending_start = [lambda: d._on_command_set('/Hydro/Command', 'ota ' + path)]
        self.run_loop(lambda: s.get('/Hydro/OtaProgress') == 100)
        self.assertEqual(s['/Hydro/CommandResult'], 'ok ota done, restarting')
        self.assertIn(bytes(image), self.uploads[0])                        # the whole file in one multipart body

    def test_auto_learns_the_address_uses_wifi_and_falls_back_to_bluetooth(self):
        d = Esp32HydroDriver('s', section('device_id = 1\n'))             # default: connection = auto
        d.ip_file = os.path.join(tempfile.mkdtemp(), 'ip')                 # no address known yet
        d.sensor_host = ''
        self.assertEqual((d.conn_mode, d.transport, d.USES_SCANNER), ('auto', 'bluetooth', True))
        d.seen_mac = 'AA:BB:CC:DD:EE:01'
        self._pending_start = [lambda: d.apply_info('{"fw":"4.12","mode":"van","ip":"%s","ssid":"Moss"}' % self.host)]
        self.run_loop(lambda: d.transport == 'wifi')                       # address from a Bluetooth check-in
        self.assertEqual(d.sensor_host, self.host)
        self.assertEqual(d.transport, 'wifi')
        self.assertEqual(d.tank.service['/Hydro/Transport'], 'wifi')
        self.assertEqual(d.tank.service['/Level'], 15.1)
        self.assertEqual(type(d._client()).__name__, 'WifiClient')           # commands go over Wi-Fi too
        before = d.tank.service['/Level']
        d.handle_advertisement('AA:BB:CC:DD:EE:01', {proto.COMPANY_ID: proto.build(1, 90.0, 90.0, 100.0, 600)}, -60)
        self.assertEqual(d.tank.service['/Level'], before)                  # one source: Bluetooth not published
        self.server.shutdown()                                             # the sensor leaves the network
        self.server.server_close()
        self.run_loop(lambda: d.transport == 'bluetooth', 25)
        self.assertEqual(d.transport, 'bluetooth')                         # Bluetooth takes over
        self.assertEqual(d.tank.service['/Hydro/Transport'], 'bluetooth')

    def test_sensor_not_answering(self):
        d = Esp32HydroDriver('s', section('connection = wifi\nsensor_host = 127.0.0.1:9\n'))
        self._pending_start = [d.start]
        self.run_loop(lambda: (d.tank.service['/Hydro/LinkStatus'] or '').startswith('no answer'), 10)
        self.assertIn('no answer over Wi-Fi', d.tank.service['/Hydro/LinkStatus'])


class AlarmTests(unittest.TestCase):
    """Venus OS alarms: frost in the water tank, and the Truma heater's W/E codes."""

    def test_frost_alarm_with_water_at_or_below_3_c_and_hysteresis(self):
        d = Esp32HydroDriver('s', section('custom_name = Fresh water\n'))
        a = d.frost.service
        d._frost_check(40.0, {1: 5.0})
        self.assertEqual(a['/Alarm'], 0)
        d._frost_check(40.0, {1: 3.0})                                     # at the limit: alarm
        self.assertEqual((a['/Alarm'], a['/State']), (2, 9))
        self.assertIn('Fresh water: water 3.0', a['/CustomName'])
        d._frost_check(40.0, {1: 3.8})                                     # within 1 C above: still on
        self.assertEqual(a['/Alarm'], 2)
        d._frost_check(40.0, {1: 4.2})
        self.assertEqual(a['/Alarm'], 0)
        d._frost_check(1.0, {1: 0.5})                                      # (almost) empty tank: no alarm
        self.assertEqual(a['/Alarm'], 0)

    def test_frost_alarm_can_be_switched_off_and_uses_the_chosen_probe(self):
        self.assertIsNone(Esp32HydroDriver('s', section('frost_alarm = no\n')).frost)
        d = Esp32HydroDriver('s', section('frost_alarm_probe = 2\nfrost_alarm_c = 1\n'))
        d._frost_check(40.0, {1: -5.0, 2: 2.0})
        self.assertEqual(d.frost.service['/Alarm'], 0)
        d._frost_check(40.0, {1: 9.0, 2: 0.5})
        self.assertEqual(d.frost.service['/Alarm'], 2)

    def test_heater_code_becomes_a_warning_or_alarm_with_its_meaning(self):
        import truma_heater
        d = Esp32HydroDriver('s', section('heater = yes\n'))
        h = d.heater
        h.apply({'lin': 'ok', 'room': 18, 'set': 20, 'heat': 1, 'water': 'eco', 'err': 42, 'ecls': 'W'})
        self.assertEqual(h.alarm.service['/Alarm'], 1)                      # W = warning
        self.assertIn('W 42 H: Window above the flue open', h.alarm.service['/CustomName'])
        h.apply({'lin': 'ok', 'room': 18, 'set': 20, 'heat': 0, 'water': 'eco', 'err': 112, 'ecls': 'E'})
        self.assertEqual(h.alarm.service['/Alarm'], 2)                      # E = alarm
        self.assertIn('E 112 H: Flame out or not detected', h.alarm.service['/CustomName'])   # short meaning only
        import truma_heater as th
        self.assertEqual(set(th.BUILTIN_CODES), set(th.BUILTIN_HELP))     # every code has its "what to do"
        self.assertIn('E517', th.BUILTIN_CODES)
        h.apply({'lin': 'ok', 'room': 18, 'set': 20, 'heat': 0, 'water': 'eco', 'err': 0, 'ecls': 'W'})
        self.assertEqual(h.alarm.service['/Alarm'], 0)
        path = os.path.join(tempfile.mkdtemp(), 'codes.json')
        with open(path, 'w') as f:
            json.dump({'E112': 'no flame'}, f)
        self.assertEqual(truma_heater.code_text({'err': 112, 'ecls': 'E'}, truma_heater.load_codes(path)), 'E 112 H: no flame')
