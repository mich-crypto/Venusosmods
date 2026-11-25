#!/usr/bin/env python3
"""
Venus OS driver for the ESP32-S3 hydrostatic water tank sender
(firmware/esp32_hydro_tank). All filtering and volume calculation happens on
the ESP32; this side only decodes and publishes.

Publishes com.victronenergy.tank.<service> and, with DS18B20 probes fitted,
com.victronenergy.temperature.<service>_temp (probe 1, temperature_service = yes)
and <service>_temp2 (probe 2, temperature2_service = yes), each with its own
name (the tank's own /Temperature path is only shown by the GUI for Mopeka tanks).
"""

import json
import logging
import os
import re
import struct
import subprocess
import time

import esp32_hydro_protocol as proto
import rawlog
from gi.repository import GLib
from venus_service import monotonic_s
from venus_service import (BleDriver, TankDevice, TemperatureDevice, interpolate, parse_table,
                           FLUID_FRESH_WATER, TEMPERATURE_TYPES)

log = logging.getLogger('esp32_hydro')


def wifi_checksum(*parts):
    """A short checksum of the Wi-Fi setting (FNV-1a), so the password itself is not stored again."""
    h = 0x811c9dc5
    for b in '\x1f'.join(p or '' for p in parts).encode('utf-8'):
        h = ((h ^ b) * 0x01000193) & 0xffffffff
    return '%08x' % h


class Esp32HydroDriver(BleDriver):
    DEFAULT_SECTION = 'esp32_hydro'

    def __init__(self, section, config):
        super(Esp32HydroDriver, self).__init__(section, config)
        self.device_id = config.getint('device_id', 1)
        # Calibration done on the settings page (height_mm:litres pairs) replaces the sender's own table
        table = config.get('level_table', '').strip()
        self.level_table = parse_table(table) if table else None
        self.capacity_l = config.getfloat('capacity_l', 0.0) or (self.level_table[-1][1] if self.level_table else 0.0)
        self._last_seq = None
        uid = config.get('service', 'esp32_hydro')
        connection = 'Bluetooth LE %s' % (self.mac or 'device id %d' % self.device_id)
        timeout = config.getint('timeout', 120)   # the ESP32 sends every 2 s

        self.tank = TankDevice(
            self.bus, uid, product_name='ESP32 Hydrostatic Tank',
            connection=connection,
            fluid_type=config.getint('fluid_type', FLUID_FRESH_WATER),
            default_instance=config.getint('device_instance', 20),
            custom_name=config.get('custom_name', ''),
            timeout_s=timeout,
            extra_paths=('/Hydro/HeightMm', '/Hydro/Filling', '/Hydro/Draining',
                         '/Hydro/Sloshing', '/Hydro/Fault', '/Hydro/Temperature2',
                         # Bluetooth link (firmware 2.7+)
                         '/Hydro/WifiMode', '/Hydro/AccessPoint', '/Hydro/WifiIp',
                         '/Hydro/WifiSsid', '/Hydro/WifiKnown',
                         '/Hydro/LinkStatus', '/Hydro/LinkTime', '/Hydro/TempNames', '/Hydro/Mac',
                         # live view: raw reading over the Bluetooth link
                         '/Hydro/Signal', '/Hydro/SignalUnit', '/Hydro/RawHeightMm',
                         '/Hydro/LiveHeightMm', '/Hydro/LiveLitres', '/Hydro/LiveTime',
                         # firmware update: the page's command, the sensor's answer and last error
                         '/Hydro/CommandResult', '/Hydro/FwError', '/Hydro/OtaProgress'),
            setup=self._writable_paths)
        self.devices.append(self.tank)
        self.raw = rawlog.RawLog('water')

        # Bluetooth link: check in every link_interval s (keeps the sender's own
        # Wi-Fi off in auto mode), push the Wi-Fi settings, read firmware and state
        self.link = config.getboolean('link', True)
        self.link_interval = max(60, config.getint('link_interval', 300))
        self.sender_password = config.get('sender_password', 'watertank') or 'watertank'
        self.wifi_mode = config.get('wifi_mode', 'auto').strip().lower()
        self.wifi_ssid = config.get('wifi_ssid', '')
        self.wifi_password = config.get('wifi_password', '')
        self.adapter = config.get('adapter', 'hci0')
        # The Wi-Fi setting can also be changed on the sensor's own page. The Pi sends its own
        # only when it changed here (last change wins), not at every check-in. What was sent is
        # kept as a checksum, so a restart or reboot of the Pi doesn't send it again.
        self.wifi_state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state',
                                            '%s.wifi-sent' % config.get('service', 'esp32_hydro'))
        self._wifi_sum = wifi_checksum(self.wifi_mode, self.wifi_ssid, self.wifi_password)
        self._wifi_pending = None
        self.seen_mac = self.mac
        self.gatt = None
        self._collector = None
        self._expected = 0
        self._mode = None              # what the running exchange is: checkin | live | stop
        self._link_quiet_until = 0     # old firmware without the link: try only once an hour
        self._command = None           # waiting to be sent to the sensor
        self._last_cmd = ''
        self.ota = None                # a firmware upload over Bluetooth, see _ota_step
        self.live_until = 0            # monotonic end of the live view (0 = off)
        self._last_adv = None          # monotonic time of the sensor's last broadcast
        self._last_temp = None         # probe 1, from the last broadcast
        self._silent_kicks = 0         # scan restarts since the broadcasts stopped

        self.temp = self._temperature(config, uid, connection, timeout, '', 20, 'Temperature 1')
        self.temp2 = self._temperature(config, uid, connection, timeout, '2', 21, 'Temperature 2')

    def _temperature(self, config, uid, connection, timeout, n, instance, name):
        """Probe 1 uses temperature_*, probe 2 temperature2_* in config.ini."""
        if not config.getboolean('temperature%s_service' % n, False):
            return None
        dev = TemperatureDevice(
            self.bus, uid + '_temp' + n, product_name='ESP32 Tank Temperature' + (' ' + n if n else ''),
            connection=connection,
            temperature_type=TEMPERATURE_TYPES[config.get('temperature%s_type' % n, 'generic')],
            default_instance=config.getint('temperature%s_instance' % n, instance),
            custom_name=config.get('temperature%s_name' % n, name),
            timeout_s=timeout)
        self.devices.append(dev)
        return dev

    # ------------------------------------------------------- Bluetooth link

    def start(self):
        if self.link:
            if not self.seen_mac:
                self.tank['/Hydro/LinkStatus'] = 'no broadcast received from the sensor yet'
            GLib.timeout_add_seconds(20, self._first_link)

    def _first_link(self):
        self._link_tick()
        GLib.timeout_add_seconds(self.link_interval, self._link_tick)
        GLib.timeout_add_seconds(self.SCAN_WATCH_S, self._scan_watch)
        return False

    # The sensor broadcasts every 2 s. If nothing arrives, look first at the other Bluetooth
    # devices (gas sensor, fridge, Victron, phones): if they are heard, the scan works and the
    # sensor is off or out of range, so nothing is touched. Only if nothing at all is heard is
    # this driver's own scan session restarted (the others keep theirs), and as a last resort
    # the shared adapter power-cycled: see BlueZScanner.power_cycle_if_dead for its safeguards.
    SCAN_WATCH_S = 30
    SILENT_S = 60

    def _scan_watch(self):
        scanner = getattr(self, 'scanner', None)
        if scanner is None or self._last_adv is None:
            return True
        if self.live_until or self.ota or (self.gatt is not None and self.gatt.busy):
            return True                        # the live connection itself carries the data
        silent = monotonic_s() - self._last_adv
        if silent < self.SILENT_S:
            self._silent_kicks = 0
            return True
        self._silent_kicks += 1
        if scanner.heard_any_within(self.SILENT_S):
            if self._silent_kicks == 1:
                log.info('no broadcast from the sensor for %d s, other Bluetooth devices are heard: '
                         'sensor off or out of range', silent)
            return True
        if self._silent_kicks <= 3 or self._silent_kicks % 10 == 0:
            log.warning('nothing heard over Bluetooth for %d s: restarting this driver\'s scan', silent)
            scanner.restart()
        if self._silent_kicks >= 6:            # 3 minutes without any device
            why = scanner.power_cycle_if_dead()
            if why and self._silent_kicks == 6:
                log.warning('Bluetooth adapter not reset: %s', why)
        return True

    def _writable_paths(self, svc):
        svc.add_path('/Hydro/Live', 0, writeable=True, onchangecallback=self._on_live_set)
        svc.add_path('/Hydro/Command', '', writeable=True, onchangecallback=self._on_command_set)

    # A command from the settings page for the sensor (the firmware update: "fwget <url>").
    # The van Wi-Fi name and password are added here, from config.ini, so they never pass the page.
    def _on_command_set(self, path, value):
        cmd = str(value or '').strip()
        if not cmd or not self.seen_mac:
            return False
        if cmd.startswith('ota '):
            return self._ota_start(cmd[4:].strip())
        if cmd.startswith('fwget ') and self.wifi_ssid:
            cmd += '\t%s\t%s' % (self.wifi_ssid, self.wifi_password)   # else the sensor uses its own network (4.5+)
        self._command = cmd
        self.tank['/Hydro/CommandResult'] = None
        GLib.timeout_add(0, self._send_command)
        return True

    def _send_command(self):
        if self.gatt is not None and self.gatt.busy:
            GLib.timeout_add(1000, self._send_command)    # a check-in or live reading is running
            return False
        cmd, self._command = self._command, None
        if cmd:
            self._last_cmd = cmd
            self._send('command', [proto.link_commands(self.sender_password)[0], b'C' + cmd.encode('utf-8')])
        return False

    # ---------------------------------------------------- firmware over Bluetooth
    # Over the same connection as the live reading, in rounds: auth, OTA_BATCH data packets
    # ('D' + offset + bytes, each fitting one Bluetooth frame), then "ota status". The answer says
    # how much the sensor has, and the next round starts there, so a refused packet or a dropped
    # connection only costs a retry. "ota end" lets the sensor check the image and restart.
    OTA_BATCH = 40
    OTA_MAX_ERRORS = 40

    def _ota_start(self, path):
        if self.ota:
            return False
        try:
            with open(path, 'rb') as f:
                data = f.read()
            md5 = subprocess.check_output(['md5sum', path]).decode().split()[0]
        except (OSError, subprocess.CalledProcessError, IndexError):
            self.tank['/Hydro/CommandResult'] = 'error ota: could not read %s' % path
            return True
        if len(data) < 1000 or data[0] != 0xE9 or data[27] != 0x3C:
            self.tank['/Hydro/CommandResult'] = 'error ota: not a sensor firmware file'
            return True
        self.ota = {'data': data, 'md5': md5, 'phase': 'begin', 'pos': 0, 'chunk': 180, 'errors': 0, 'restarts': 0}
        self.tank['/Hydro/OtaProgress'] = 0
        self.tank['/Hydro/CommandResult'] = 'ota: connecting'
        GLib.timeout_add(0, self._ota_step)
        return True

    def _ota_step(self):
        o = self.ota
        if not o:
            return False
        if self.gatt is not None and self.gatt.busy:
            GLib.timeout_add(500, self._ota_step)         # a check-in or live poll is finishing
            return False
        auth = proto.link_commands(self.sender_password)[0]
        self._client().keep_connected = True
        if o['phase'] == 'begin':
            frames = [auth, ('Cota begin %d %s' % (len(o['data']), o['md5'])).encode()]
        elif o['phase'] == 'data':
            frames = [auth]
            pos = o['pos']
            for _ in range(self.OTA_BATCH):
                if pos >= len(o['data']):
                    break
                part = o['data'][pos:pos + o['chunk']]
                frames.append(b'D' + struct.pack('<I', pos) + part)
                pos += len(part)
            frames.append(b'Cota status')
        else:
            frames = [auth, b'Cota end']
        self._send('ota', frames)
        return False

    def _ota_fail(self, text):
        self.ota = None
        self._client().keep_connected = False
        self.tank['/Hydro/OtaProgress'] = None
        self.tank['/Hydro/CommandResult'] = 'error ota: %s' % text
        log.warning('firmware over Bluetooth: %s', text)

    def _ota_answer(self, ok, error, replies):
        o = self.ota
        if not o:
            return
        answer = replies[-1].strip() if replies else ''
        if not ok and not answer:
            o['errors'] += 1                   # dropped or refused: try the same round again
            if o['errors'] > self.OTA_MAX_ERRORS:
                self._ota_fail('too many connection errors (last: %s)' % error)
                return
            self.tank['/Hydro/CommandResult'] = 'ota: connection trouble (%s), trying again' % error
            GLib.timeout_add_seconds(3, self._ota_step)
            return
        if answer.startswith('denied'):
            self._ota_fail('the sensor refused the password: set Sensor password')
            return
        if o['phase'] == 'begin':
            m = re.match(r'ok ota begin mtu=(\d+)', answer)
            if not m:
                if not answer or answer.lower().startswith('unknown command') or answer.startswith('ota begin <size>'):
                    answer = ('the sensor firmware is older than 4.2 and can\'t take updates over Bluetooth: '
                              'update it once over Wi-Fi or on its own page')
                self._ota_fail(answer)
                return
            mtu = int(m.group(1))
            o['chunk'] = max(15, min(mtu - 3, 244) - 5)   # one frame: MTU - 3, minus 'D' and the offset
            o['phase'], o['pos'] = 'data', 0
            self.tank['/Hydro/CommandResult'] = 'ota: sending %d kB in %d-byte packets' % (len(o['data']) // 1024, o['chunk'])
        elif o['phase'] == 'data':
            m = re.match(r'ota (active|failed) (\d+)/(\d+)', answer)
            if not m:
                if answer.startswith('ota idle') and o['restarts'] < 2:
                    o['restarts'] += 1         # the sensor lost the upload (restart): start again
                    o['phase'] = 'begin'
                    GLib.timeout_add_seconds(2, self._ota_step)
                    return
                self._ota_fail(answer or 'no status from the sensor')
                return
            if m.group(1) == 'failed':
                self._ota_fail('the sensor could not write the firmware')
                return
            o['pos'] = int(m.group(2))
            self.tank['/Hydro/OtaProgress'] = int(100 * o['pos'] / len(o['data']))
            if o['pos'] >= len(o['data']):
                o['phase'] = 'end'
        else:
            if not answer.startswith('ok'):
                self._ota_fail(answer)
                return
            self.ota = None
            self._client().keep_connected = False
            self.tank['/Hydro/OtaProgress'] = 100
            self.tank['/Hydro/CommandResult'] = answer
            for s in (20, 45, 90):             # the sensor restarts: ask for its version soon
                GLib.timeout_add_seconds(s, lambda: (self._link_tick(), False)[1])
            return
        GLib.timeout_add(0, self._ota_step)

    def _client(self):
        if self.gatt is None or self.gatt.mac != self.seen_mac.upper():
            from ble_gatt import GattClient
            self.gatt = GattClient(self.bus, self.seen_mac, proto.LINK_CMD_UUID, proto.LINK_REPLY_UUID,
                                   self._on_link_notify, adapter=self.adapter, timeout_s=40,
                                   scanner=getattr(self, 'scanner', None), connect_attempts=2)
        return self.gatt

    def _send(self, mode, frames):
        """One exchange; every frame but auth gets a notified reply."""
        self._mode = mode
        self._collector = proto.ReplyCollector()
        self._expected = sum(1 for f in frames if f.startswith(b'C') and not f.startswith(b'Cauth '))   # data: no answer
        self._client().exchange(frames, self._on_link_done)

    def _retry_soon(self):
        if getattr(self, '_retry_pending', False):
            return
        self._retry_pending = True

        def again():
            self._retry_pending = False
            self._link_tick()
            return False
        GLib.timeout_add_seconds(60, again)

    def _link_tick(self):
        if self.ota:
            return True                        # a firmware upload uses the link
        if not self.seen_mac:
            # without a broadcast the Pi doesn't know where to connect: say so on the page
            self.tank['/Hydro/LinkStatus'] = 'no broadcast received from the sensor yet'
            return True
        if self.gatt is not None and self.gatt.busy:
            return True
        if monotonic_s() < self._link_quiet_until:
            return True                        # each try pauses the other drivers' scanning: not every 5 min
        push = self._wifi_sum != self._wifi_sent()
        self._wifi_pending = self._wifi_sum if push else None
        if push:
            frames = proto.link_commands(self.sender_password, self.wifi_mode, self.wifi_ssid, self.wifi_password)
        else:
            frames = proto.link_commands(self.sender_password)          # auth, hb, info
        self._send('checkin', frames)
        return True

    def _wifi_sent(self):
        try:
            with open(self.wifi_state_file) as f:
                return f.read().strip()
        except OSError:
            return None

    def _wifi_mark_sent(self, value):
        try:
            os.makedirs(os.path.dirname(self.wifi_state_file), exist_ok=True)
            with open(self.wifi_state_file, 'w') as f:
                f.write(value + '\n')
        except OSError as e:
            log.warning('could not remember the Wi-Fi setting sent: %s', e)

    # Live view (settings page, Water tab): stay connected, switch the sender's
    # smoothing off and read the raw signal about twice a second, for 15 minutes.
    LIVE_S = 15 * 60

    def _on_live_set(self, path, value):
        try:
            on = bool(int(value))
        except (TypeError, ValueError):
            return False
        if on:
            if not self.seen_mac:
                return False                   # nothing received yet: no address to connect to
            first = not self.live_until
            self.live_until = monotonic_s() + self.LIVE_S
            if first:
                GLib.timeout_add(500, self._live_poll)
        else:
            self.live_until = monotonic_s() - 1   # the next poll stops it
        return True

    def _live_poll(self):
        if not self.live_until:
            return False
        if self.ota:
            return True                        # paused while firmware goes over the link
        if self.gatt is not None and self.gatt.busy:
            return True                        # a check-in or the last poll is still running
        if monotonic_s() > self.live_until:
            self.live_until = 0
            self.tank['/Hydro/Live'] = 0
            client = self._client()
            client.keep_connected = False      # disconnects after this exchange
            self._send('stop', [proto.link_commands(self.sender_password)[0], b'Cfast off'])
            return False
        self._client().keep_connected = True
        self._send('live', [proto.link_commands(self.sender_password)[0], b'Chb', b'Cfast', b'Clive'])
        return True

    def apply_live(self, text):
        try:
            d = json.loads(text)
        except ValueError:
            return
        t = self.tank
        t['/Hydro/Signal'] = d.get('s')
        t['/Hydro/SignalUnit'] = d.get('u')
        t['/Hydro/RawHeightMm'] = d.get('raw')
        t['/Hydro/LiveHeightMm'] = d.get('h')
        t['/Hydro/LiveLitres'] = d.get('l')
        t['/Hydro/LiveTime'] = time.time()
        self._publish_live(d)

    def _publish_live(self, d):
        """While the live view holds the connection the Pi hears no broadcasts: the live reading
        keeps the tank (level, litres) current and connected instead."""
        h, litres = d.get('h'), d.get('l')
        if h is None or litres is None or d.get('fault'):
            return
        cap_l = self.tank.capacity_m3 * 1000.0 if self.tank.capacity_m3 else None
        if self.level_table and self.capacity_l:
            litres, cap_l = max(0.0, interpolate(self.level_table, h)), self.capacity_l
        if not cap_l:
            return
        self.tank.publish(level=min(100.0, 100.0 * litres / cap_l), remaining_m3=litres / 1000.0,
                          capacity_m3=cap_l / 1000.0, temperature=self._last_temp,
                          raw_value=int(round(h)), raw_unit='mm', extra={'/Hydro/HeightMm': int(round(h))})
        for dev in (self.temp, self.temp2):
            if dev is not None and dev['/Temperature'] is not None:
                dev.mark_seen()

    def _on_link_notify(self, data):
        if self._collector is None:
            return
        replies = self._collector.feed(data)
        if len(replies) >= self._expected and self.gatt is not None:
            self.gatt.finish()

    def _on_link_done(self, ok, error):
        replies = self._collector.replies if self._collector else []
        self._collector = None
        mode, self._mode = self._mode, None
        if mode == 'ota':
            self._ota_answer(ok, error, replies)
            return
        if mode == 'command':
            answer = replies[-1].strip() if replies else 'no answer: %s' % (error or 'link failed')
            if any(r.startswith('denied') for r in replies):
                answer = 'wrong sensor password'
            self.tank['/Hydro/CommandResult'] = answer
            # Wi-Fi changes: read the new state back soon; a firmware download: the new version later
            for s in ((3, 25) if self._last_cmd.startswith('wifi ') else (45, 90)):
                GLib.timeout_add_seconds(s, lambda: (self._link_tick(), False)[1])
            return
        if mode in ('live', 'stop'):
            if replies and replies[-1].startswith('{'):
                self.apply_live(replies[-1])
                self.tank['/Hydro/LinkStatus'] = 'ok'
            elif not ok:
                self.tank['/Hydro/LinkStatus'] = 'live view: %s' % (error or 'failed')
            if any(r.startswith('denied') for r in replies):
                self.tank['/Hydro/LinkStatus'] = 'wrong sensor password'
                self.live_until = monotonic_s() - 1
            return
        if not ok and len(replies) < self._expected:
            text = str(error or 'failed')
            if 'not found' in text:
                text = 'sensor firmware without Bluetooth link (older than 2.7)'
                self._link_quiet_until = monotonic_s() + 3600
            else:
                # a dropped or refused connection is often a one-off: try again in a minute
                self._retry_soon()
            log.info('link: %s', text)
            self.tank['/Hydro/LinkStatus'] = text
            return
        if any(r.startswith('denied') for r in replies):
            log.warning('link: sender refused the password - set sender_password = the sender\'s Wi-Fi password')
            self.tank['/Hydro/LinkStatus'] = 'wrong sensor password'
            return
        self.apply_info(replies[-1] if replies else '')

    def apply_info(self, text):
        """Publish what the sender reports (JSON from its 'info' command)."""
        try:
            info = json.loads(text)
        except ValueError:
            self.tank['/Hydro/LinkStatus'] = 'unexpected answer'
            return
        self.tank['/FirmwareVersion'] = info.get('fw')
        self.tank['/Hydro/WifiMode'] = info.get('mode')
        if self._wifi_pending:
            self._wifi_mark_sent(self._wifi_pending)   # the sensor has the Pi's setting now
            self._wifi_pending = None
        self.tank['/Hydro/AccessPoint'] = int(bool(info.get('ap')))
        self.tank['/Hydro/WifiIp'] = info.get('ip') or ''
        self.tank['/Hydro/WifiSsid'] = info.get('ssid') or ''
        if 'kn' in info:
            self.tank['/Hydro/WifiKnown'] = json.dumps(info.get('kn') or [])
        self.tank['/Hydro/TempNames'] = json.dumps(info.get('tn') or [])
        self.tank['/Hydro/FwError'] = info.get('fw_err') or ''
        self.tank['/Hydro/LinkStatus'] = 'ok'
        self.tank['/Hydro/LinkTime'] = int(time.time())

    def shutdown(self):
        if self.gatt is not None:
            self.gatt.close()

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        r = proto.parse(manufacturer_data.get(proto.COMPANY_ID))
        if r is None or r.device_id != self.device_id:
            return
        if mac != self.seen_mac or self.tank['/Hydro/Mac'] is None:
            self.seen_mac = mac
            self.tank['/Hydro/Mac'] = mac
        self._last_adv = monotonic_s()
        self._last_temp = r.temperature
        if r.sequence == self._last_seq:
            return  # same packet seen again, nothing new
        self._last_seq = r.sequence
        log.debug('%s rssi=%s %r', mac, rssi, r)
        self.raw.write('adv', '%s rssi=%s data=%s -> %r' % (mac, rssi, rawlog.hexs(manufacturer_data.get(proto.COMPANY_ID)), r))

        level, volume_l, capacity_l = r.level, r.volume_l, r.capacity_l
        if self.level_table and self.capacity_l and r.height_mm is not None:
            volume_l = max(0.0, interpolate(self.level_table, r.height_mm))
            capacity_l = self.capacity_l
            level = min(100.0, 100.0 * volume_l / capacity_l)
        self.tank.publish(
            level=None if r.fault else level,
            remaining_m3=None if r.fault else volume_l / 1000.0,
            capacity_m3=capacity_l / 1000.0 if capacity_l else None,
            temperature=r.temperature,
            raw_value=r.height_mm,
            raw_unit='mm',
            rssi=rssi,
            status=TankDevice.STATUS_ERROR if r.fault else TankDevice.STATUS_OK,
            extra={
                '/Hydro/HeightMm': r.height_mm,
                '/Hydro/Filling': int(r.filling),
                '/Hydro/Draining': int(r.draining),
                '/Hydro/Sloshing': int(r.sloshing),
                '/Hydro/Fault': int(r.fault),
                '/Hydro/Temperature2': r.temperature2,
            })

        # Only refresh the temperature service with a valid reading, so a
        # dead probe ends in "disconnected" via the timeout instead of
        # showing a stale value as current.
        if self.temp is not None and r.temperature is not None:
            self.temp.publish(r.temperature)
        if self.temp2 is not None and r.temperature2 is not None:
            self.temp2.publish(r.temperature2)


if __name__ == '__main__':
    Esp32HydroDriver.main()
