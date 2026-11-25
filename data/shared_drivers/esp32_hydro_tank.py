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
import esp32_wifi_link as wifilink
import rawlog
import truma_heater
from gi.repository import GLib
from venus_service import monotonic_s
from venus_service import (BleDriver, DigitalInputDevice, TankDevice, TemperatureDevice, interpolate, parse_table,
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
                         '/Hydro/WifiSsid', '/Hydro/WifiKnown', '/Hydro/Uptime',
                         '/Hydro/Truma',   # the heater's last status as JSON, for the settings page
                         '/Hydro/Transport',   # 'bluetooth' or 'wifi': how the Pi reaches the sensor
                         '/Hydro/LinkStatus', '/Hydro/LinkTime', '/Hydro/TempNames', '/Hydro/Mac',
                         # live view: raw reading over the Bluetooth link
                         '/Hydro/Signal', '/Hydro/SignalUnit', '/Hydro/RawHeightMm',
                         '/Hydro/LiveHeightMm', '/Hydro/LiveLitres', '/Hydro/LiveTime',
                         # firmware update: the page's command, the sensor's answer and last error
                         '/Hydro/CommandResult', '/Hydro/FwError', '/Hydro/OtaProgress'),
            setup=self._writable_paths)
        self.devices.append(self.tank)
        self.raw = rawlog.RawLog('water')
        self.link_log = rawlog.RawLog('link')    # Debug -> Raw data -> Link: every exchange, step by step
        self._tx_at = 0
        self._live_n = 0
        self.stats = self._new_stats()
        self._cmd_tries = 0

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
        self._live_fails = 0
        self._live_pause_until = 0
        self._last_adv = None          # monotonic time of the sensor's last broadcast
        self._last_temp = None         # probe 1, from the last broadcast
        self._silent_kicks = 0         # scan restarts since the broadcasts stopped

        self.temp = self._temperature(config, uid, connection, timeout, '', 20, 'Temperature 1')
        self.temp2 = self._temperature(config, uid, connection, timeout, '2', 21, 'Temperature 2')

        # Connection to the sensor:
        #   auto       the Wi-Fi network when the sensor answers there, Bluetooth as fallback (default).
        #              The sensor's address comes from the setting, or from its own report (check-in).
        #   wifi       only the Wi-Fi network: no Bluetooth at all (no scanning, no connections)
        #   bluetooth  only Bluetooth (broadcasts + link)
        mode = config.get('connection', 'auto').strip().lower()
        self.conn_mode = mode if mode in ('auto', 'wifi', 'bluetooth') else 'auto'
        self.ip_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state', '%s.ip' % uid)
        self.sensor_host = (config.get('sensor_host', '').strip() or self._saved_ip()
                            or ('watertank.local' if self.conn_mode == 'wifi' else ''))
        self._fixed_host = bool(config.get('sensor_host', '').strip())
        self._wifi_ok = None
        self._wifi_fails = 0
        self.poller = None
        self._wclient = self._bclient = None
        self.tank['/Hydro/Transport'] = self.transport
        if self.conn_mode == 'wifi':
            self.USES_SCANNER = False
            self.seen_mac = 'WIFI'
            self.tank['/Hydro/Mac'] = 'Wi-Fi %s' % self.sensor_host
            self.link_interval = min(self.link_interval, 60)    # a request over the network costs nothing

        # Frost alarm in Venus OS: water in the tank and the water temperature at or below the limit
        self.frost = None
        if config.getboolean('frost_alarm', True):
            self.frost_c = config.getfloat('frost_alarm_c', 3.0)
            self.frost_min_l = config.getfloat('frost_alarm_min_l', 2.0)
            self.frost_probe = 2 if config.get('frost_alarm_probe', '1').strip() == '2' else 1
            self.frost_name = config.get('custom_name', '') or 'Water tank'
            self._frost_on = False
            self.frost = DigitalInputDevice(
                self.bus, uid + '_frost', product_name='Water tank frost alarm', connection=connection,
                default_instance=config.getint('frost_instance', 27),
                custom_name='%s frost' % self.frost_name, timeout_s=timeout)
            self.devices.append(self.frost)

        # Truma heater through the same sensor (in preparation, docs/truma.md)
        self.heater = None
        if config.getboolean('heater', False):
            self.heater = truma_heater.TrumaHeater(self.bus, config, uid, connection, timeout, self._heater_send)
            self.devices.extend(self.heater.devices)

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

    WIFI_FAILS = 3      # auto: after this many unanswered requests (15 s) Bluetooth takes over

    @property
    def transport(self):
        """How the Pi reaches the sensor right now: 'wifi' or 'bluetooth'."""
        if self.conn_mode == 'wifi' or (self.conn_mode == 'auto' and self._wifi_ok):
            return 'wifi'
        return 'bluetooth'

    def _reachable(self):
        return self.transport == 'wifi' or bool(self.seen_mac)

    def _start_poller(self):
        if self.poller is None and self.sensor_host and self.conn_mode != 'bluetooth':
            self.link_log.write('wifi', 'reading the sensor over the Wi-Fi network at %s%s' % (
                self.sensor_host, ' (Bluetooth not used)' if self.conn_mode == 'wifi' else ', Bluetooth as fallback'))
            self.poller = wifilink.StatusPoller(self.sensor_host, 5, self._on_wifi_status, self._on_wifi_error)
            self.poller.start(GLib)

    def _set_host(self, ip):
        """The sensor reported its address (auto, no fixed address): use it from now on."""
        if self.conn_mode != 'auto' or self._fixed_host or not ip or ip == self.sensor_host:
            return
        self.link_log.write('wifi', 'sensor address is now %s' % ip)
        self.sensor_host = ip
        if self.poller is not None:
            self.poller.host = ip
        if self._wclient is not None:
            self._wclient.host = ip
        self._start_poller()

    def _frost_check(self, litres, temps):
        """Alarm while there is water in the tank and it is at or below the limit; off again 1 C above it
        (or when the tank is empty), so it doesn't flap."""
        if self.frost is None:
            return
        t = temps.get(self.frost_probe)
        if t is None or litres is None:
            return
        if not self._frost_on and litres > self.frost_min_l and t <= self.frost_c:
            self._frost_on = True
            log.warning('frost alarm: water %.1f C with %.0f L in the tank', t, litres)
        elif self._frost_on and (t > self.frost_c + 1 or litres <= self.frost_min_l):
            self._frost_on = False
            log.info('frost alarm cleared: water %.1f C, %.0f L', t, litres)
        self.frost.publish(2 if self._frost_on else 0,
                           ('%s: water %.1f °C with %.0f L - risk of freezing' % (self.frost_name, t, litres))
                           if self._frost_on else '%s frost' % self.frost_name)

    def _saved_ip(self):
        try:
            with open(self.ip_file) as f:
                return f.read().strip()
        except OSError:
            return ''

    def start(self):
        self._start_poller()
        if self.link:
            if not self._reachable():
                self.tank['/Hydro/LinkStatus'] = 'no broadcast received from the sensor yet'
            GLib.timeout_add_seconds(20, self._first_link)

    def _heater_send(self, cmd):
        """A heater change from Venus OS: to the sensor at once."""
        return self._on_command_set('/Hydro/Command', cmd)

    def _first_link(self):
        self._link_tick()
        GLib.timeout_add_seconds(self.link_interval, self._link_tick)
        GLib.timeout_add_seconds(self.SCAN_WATCH_S, self._scan_watch)
        GLib.timeout_add_seconds(3600, self._log_stats)
        return False

    # The sensor broadcasts every 2 s. If nothing arrives, look first at the other Bluetooth
    # devices (gas sensor, fridge, Victron, phones): if they are heard, the scan works and the
    # sensor is off or out of range, so nothing is touched. Only if nothing at all is heard is
    # this driver's own scan session restarted (the others keep theirs), and as a last resort
    # the shared adapter power-cycled: see BlueZScanner.power_cycle_if_dead for its safeguards.
    SCAN_WATCH_S = 30
    SILENT_S = 60
    GAP_S = 30          # BlueZ reports a device it knows about every 10 s, not every broadcast: only longer gaps count

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
                self.link_log.write('scan', 'no broadcast for %d s, other devices heard' % silent)
                log.info('no broadcast from the sensor for %d s, other Bluetooth devices are heard: '
                         'sensor off or out of range', silent)
            return True
        if self._silent_kicks == 1:
            self._snapshot('nothing heard for %d s' % silent)
        if self._silent_kicks <= 3 or self._silent_kicks % 10 == 0:
            log.warning('nothing heard over Bluetooth for %d s: restarting this driver\'s scan', silent)
            self.link_log.write('scan', 'nothing heard for %d s: scan restarted' % silent)
            self.stats['scan_restarts'] += 1
            scanner.restart()
        if self._silent_kicks >= 6:            # 3 minutes without any device
            why = scanner.power_cycle_if_dead()
            if why is None:
                self.link_log.write('scan', 'nothing heard for %d s: Bluetooth adapter switched off and on' % silent)
            elif self._silent_kicks == 6 or self._silent_kicks % 10 == 0:
                log.warning('Bluetooth adapter not reset: %s', why)
                self.link_log.write('scan', 'Bluetooth adapter not reset: %s' % why)
        return True

    def _writable_paths(self, svc):
        svc.add_path('/Hydro/Live', 0, writeable=True, onchangecallback=self._on_live_set)
        svc.add_path('/Hydro/Command', '', writeable=True, onchangecallback=self._on_command_set)

    # A command from the settings page for the sensor (the firmware update: "fwget <url>").
    # The van Wi-Fi name and password are added here, from config.ini, so they never pass the page.
    def _on_command_set(self, path, value):
        cmd = str(value or '').strip()
        if not cmd or not self._reachable():
            return False
        if cmd == 'ota cancel':                # from the page: stop sending, the sensor drops what it got
            if self.ota:
                self._ota_fail('cancelled')
            self._command = 'ota abort'
            GLib.timeout_add(0, self._send_command)
            return True
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
        if self.transport == 'wifi':
            return self._wifi_upload(path)
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
                if answer.startswith('ota idle'):
                    # the sensor dropped the upload: restarted, or someone updated / restarted it on its
                    # own page. Never start over by ourselves, that would fight the person at the sensor.
                    self._ota_fail('the sensor dropped the upload (it restarted, or was updated on its own page); '
                                   'press Update again if needed')
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
        """The client for the next exchange: over Wi-Fi when the sensor answers there, else Bluetooth.
        One that is busy is kept (an exchange is never switched half-way)."""
        if self.gatt is not None and self.gatt.busy:
            return self.gatt
        if self.transport == 'wifi':
            if self._wclient is None:
                self._wclient = wifilink.WifiClient(self.sensor_host, self._on_link_notify)
                self._wclient.trace = self._trace
            self.gatt = self._wclient
            return self.gatt
        if self._bclient is None or self._bclient.mac != self.seen_mac.upper():
            from ble_gatt import GattClient
            self._bclient = GattClient(self.bus, self.seen_mac, proto.LINK_CMD_UUID, proto.LINK_REPLY_UUID,
                                       self._on_link_notify, adapter=self.adapter, timeout_s=40,
                                       scanner=getattr(self, 'scanner', None), connect_attempts=2)
            self._bclient.trace = self._trace
        self.gatt = self._bclient
        return self.gatt

    # ------------------------------------------------------------ debug log
    def _quiet_live(self):
        return self._mode == 'live' and self._live_n % 20 != 1     # a live poll every 0.5 s: log every 20th

    def _trace(self, text):
        if self._quiet_live() and not ('fail' in text or 'error' in text or 'disconnected' in text):
            return
        self.link_log.write('step', '%s %s' % (self._mode or '-', text))

    # What the system looks like the moment the Pi stops hearing anything: the long-term log
    # then shows it even when nobody was at the Pi (each command at most a few seconds).
    SNAPSHOT_CMDS = (
        ('chip', ['hciconfig', '{a}']),
        ('bluez', ['bluetoothctl', 'show']),
        # "Under-voltage detected" right before "hci0: Frame reassembly failed": a weak power supply
        # garbles the serial line to the Bluetooth chip, which then hears nothing until a reboot
        ('kernel', ['sh', '-c', 'dmesg | grep -iE "bluetooth|hci|brcm|uart|serial|voltage" | tail -15']),
        ('procs', ['sh', '-c', 'ps | grep -iE "blue|ble|hci|vesmart|esp32|mopeka|fridge" | grep -v grep']),
    )

    def _snapshot(self, why):
        self.link_log.write('diag', '---- %s: system state ----' % why)
        for name, argv in self.SNAPSHOT_CMDS:
            argv = [x.replace('{a}', self.adapter) for x in argv]
            try:
                out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     timeout=5).stdout.decode('utf-8', 'replace')
            except (OSError, subprocess.SubprocessError) as e:
                out = 'not available: %s' % e
            for line in [x.rstrip() for x in out.splitlines() if x.strip()][:20]:
                self.link_log.write('diag', '%-6s %s' % (name, line[:200]))

    @staticmethod
    def _new_stats():
        return {'adv': 0, 'wifi': 0, 'gaps': 0, 'longest_gap': 0, 'ok': {}, 'failed': {}, 'scan_restarts': 0, 'fallbacks': 0}

    def _log_stats(self):
        """One line an hour: how the link did (for long-term tests)."""
        st, self.stats = self.stats, self._new_stats()
        ex = ', '.join('%s %d ok / %d failed' % (m, st['ok'].get(m, 0), st['failed'].get(m, 0))
                       for m in sorted(set(st['ok']) | set(st['failed']))) or 'no exchanges'
        self.link_log.write('stats', 'last hour: %d readings over Wi-Fi (every 5 s: ~720), %d Bluetooth broadcasts (~330 when '
                            'Bluetooth is the way), %d gaps over %d s (longest %d s), %s, %d times Bluetooth took over, %d scan restarts'
                            % (st['wifi'], st['adv'], st['gaps'], self.GAP_S, st['longest_gap'], ex, st['fallbacks'], st['scan_restarts']))
        return True

    @staticmethod
    def frames_text(frames):
        """What was sent, without passwords; data packets summed up."""
        out, data, first = [], 0, None
        for f in frames:
            if f.startswith(b'D'):
                data += 1
                if first is None and len(f) >= 5:
                    first = struct.unpack('<I', f[1:5])[0]
                continue
            t = f[1:].decode('utf-8', 'replace')
            if t.startswith('auth '):
                t = 'auth ***'
            elif t.startswith('wifi pass'):
                t = 'wifi pass ***'
            elif '\t' in t:
                t = t.split('\t')[0] + ' (+Wi-Fi password)'
            out.append(t)
        if data:
            out.append('%d data packets from byte %d' % (data, first or 0))
        return ' | '.join(out)

    def _send(self, mode, frames):
        """One exchange; every frame but auth gets a notified reply."""
        self._mode = mode
        self._collector = proto.ReplyCollector()
        self._expected = sum(1 for f in frames if f.startswith(b'C') and not f.startswith(b'Cauth '))   # data: no answer
        if mode == 'live':
            self._live_n += 1
        self._tx_at = monotonic_s()
        if not self._quiet_live():
            self.link_log.write('tx', '%s (%s): %s' % (mode, 'Wi-Fi' if self.transport == 'wifi' else 'Bluetooth', self.frames_text(frames)))
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
        if not self._reachable():
            # without a broadcast (or an answer over Wi-Fi) the Pi doesn't know where to connect: say so
            self.tank['/Hydro/LinkStatus'] = 'no broadcast received from the sensor yet'
            return True
        if self.gatt is not None and self.gatt.busy:
            return True
        if monotonic_s() < self._link_quiet_until:
            return True                        # each try pauses the other drivers' scanning: not every 5 min
        # over Wi-Fi the Pi never pushes its old Wi-Fi setting: "Bluetooth only" would cut the only link
        push = self.transport == 'bluetooth' and self._wifi_sum != self._wifi_sent()
        self._wifi_pending = self._wifi_sum if push else None
        if push:
            frames = proto.link_commands(self.sender_password, self.wifi_mode, self.wifi_ssid, self.wifi_password)
        else:
            frames = proto.link_commands(self.sender_password)          # auth, hb, info
        extra = [b'Cdlog new'] + ([b'Ctruma'] if self.heater else [])   # sensor log lines (4.9+), heater (4.11+)
        frames = frames[:-1] + extra + frames[-1:]                        # info stays last
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
            if not self._reachable():
                return False                   # nothing received yet: no address to connect to
            first = not self.live_until
            self.live_until = monotonic_s() + self.LIVE_S
            if first:
                GLib.timeout_add(500, self._live_poll)
        else:
            self.live_until = monotonic_s() - 1   # the next poll stops it
        return True

    FWGET_QUIET_S = 180

    LIVE_RETRY_S = 5
    LIVE_MAX_FAILS = 20

    def _live_poll(self):
        if not self.live_until:
            return False
        if self.ota or monotonic_s() < self._link_quiet_until:
            return True                        # paused while firmware goes over the link, or over Wi-Fi
        if self.gatt is not None and self.gatt.busy:
            return True                        # a check-in or the last poll is still running
        if monotonic_s() < self._live_pause_until:
            return True                        # the last try failed: wait a little (no flood of retries)
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
        bucket = self.stats['ok' if ok else 'failed']
        bucket[self._mode or '-'] = bucket.get(self._mode or '-', 0) + 1
        if not ok or not self._quiet_live():
            self.link_log.write('rx', '%s: %s after %.1f s%s' % (
                self._mode, 'ok' if ok else 'FAILED (%s)' % error, monotonic_s() - self._tx_at,
                ''.join(' | ' + (r if len(r) <= 160 else r[:160] + '…') for r in replies)))
        self._collector = None
        mode, self._mode = self._mode, None
        if mode == 'ota':
            self._ota_answer(ok, error, replies)
            return
        if mode == 'command':
            sent = getattr(self.gatt, 'frames_sent', 2) if self.gatt is not None else 0
            if not ok and not replies and sent < 2 and self._cmd_tries < 2:
                # the connection broke before the command went out: the sensor never got it, try again
                self._cmd_tries += 1
                self.link_log.write('retry', 'command not delivered (%s), try %d of 3 in 3 s' % (error, self._cmd_tries + 1))   # 3 tries in all
                self._command = self._last_cmd
                GLib.timeout_add_seconds(3, self._send_command)
                return
            self._cmd_tries = 0
            answer = replies[-1].strip() if replies else 'no answer: %s' % (error or 'link failed')
            if any(r.startswith('denied') for r in replies):
                answer = 'wrong sensor password'
            self.tank['/Hydro/CommandResult'] = answer
            # Wi-Fi changes: read the new state back soon; a firmware download: the new version later
            d = truma_heater.parse(answer)
            if d is not None:                  # the page asked for the heater's status
                self.tank['/Hydro/Truma'] = json.dumps(d)
                if self.heater:
                    self.heater.apply(d)
            if self._last_cmd.startswith('fwget ') and answer.startswith('ok'):
                # the sensor now downloads over Wi-Fi with the whole radio: leave it alone meanwhile
                self._link_quiet_until = monotonic_s() + self.FWGET_QUIET_S
                delays = (self.FWGET_QUIET_S + 10, self.FWGET_QUIET_S + 60)
            else:
                delays = ((3, 25) if self._last_cmd.startswith(('wifi ', 'truma set', 'truma sim')) else (20, 60) if self._last_cmd == 'reboot'
                          else () if self._last_cmd in ('truma', 'dlog') else (45, 90))
            for s in delays:
                GLib.timeout_add_seconds(s, lambda: (self._link_tick(), False)[1])
            return
        if mode in ('live', 'stop'):
            if replies and replies[-1].startswith('{'):
                self.apply_live(replies[-1])
                self.tank['/Hydro/LinkStatus'] = 'ok'
                self._live_fails = 0
            elif not ok:
                self.tank['/Hydro/LinkStatus'] = 'live view: %s' % (error or 'failed')
                # failed: try again in LIVE_RETRY_S, and give up after LIVE_MAX_FAILS in a row
                self._live_fails += 1
                self._live_pause_until = monotonic_s() + self.LIVE_RETRY_S
                if mode == 'live' and self._live_fails >= self.LIVE_MAX_FAILS:
                    self.link_log.write('live', 'live view stopped: %d tries in a row failed (%s)' % (self._live_fails, error))
                    self.tank['/Hydro/LinkStatus'] = 'live view stopped: the sensor did not answer (%s)' % error
                    self.live_until = 0
                    self.tank['/Hydro/Live'] = 0
                    self._live_fails = 0
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
        for r in replies:
            d = truma_heater.parse(r)
            if d is not None:
                self.tank['/Hydro/Truma'] = json.dumps(d)
                if self.heater:
                    self.heater.apply(d)
            if r.startswith('dlog new'):
                for line in r.split('\n')[1:]:
                    if line.strip():
                        self.link_log.write('sensor', line)
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
        self._set_host(info.get('ip'))
        if info.get('ip') and info.get('ip') != self._saved_ip():
            try:
                os.makedirs(os.path.dirname(self.ip_file), exist_ok=True)
                with open(self.ip_file, 'w') as f:
                    f.write(info['ip'] + '\n')
            except OSError:
                pass
        self.tank['/Hydro/WifiSsid'] = info.get('ssid') or ''
        self.tank['/Hydro/Uptime'] = info.get('up')       # seconds since the sensor started: a restart shows
        if 'kn' in info:
            self.tank['/Hydro/WifiKnown'] = json.dumps(info.get('kn') or [])
        self.tank['/Hydro/TempNames'] = json.dumps(info.get('tn') or [])
        self.tank['/Hydro/FwError'] = info.get('fw_err') or ''
        self.tank['/Hydro/LinkStatus'] = 'ok'
        self.tank['/Hydro/LinkTime'] = int(time.time())

    def shutdown(self):
        if self.gatt is not None:
            self.gatt.close()

    # ------------------------------------------------------- over the Wi-Fi network
    def _on_wifi_status(self, d):
        """/api/status of the sensor: the same as a broadcast, plus a little more."""
        self._wifi_fails = 0
        now = monotonic_s()
        if self._last_adv is not None and now - self._last_adv > self.GAP_S:
            self.stats['gaps'] += 1
            self.stats['longest_gap'] = max(self.stats['longest_gap'], int(now - self._last_adv))
        if self._wifi_ok is not True:
            self.link_log.write('wifi', 'sensor answers at %s: reading it over the Wi-Fi network' % self.sensor_host)
            self._wifi_ok = True
            self.tank['/Hydro/Transport'] = self.transport
        self._last_adv = now
        self.stats['wifi'] += 1
        temps = dict((t.get('n'), t.get('c')) for t in (d.get('temps') or []))
        self._last_temp = temps.get(1)
        height = d.get('height_mm')
        fault = bool(d.get('fault'))
        volume_l, capacity_l, level = d.get('litres'), d.get('capacity'), d.get('level')
        if self.level_table and self.capacity_l and height is not None:
            volume_l = max(0.0, interpolate(self.level_table, height))
            capacity_l = self.capacity_l
            level = min(100.0, 100.0 * volume_l / capacity_l)
        self.tank.publish(
            level=None if fault or level is None else level,
            remaining_m3=None if fault or volume_l is None else volume_l / 1000.0,
            capacity_m3=capacity_l / 1000.0 if capacity_l else None,
            temperature=temps.get(1), raw_value=None if height is None else int(round(height)), raw_unit='mm',
            status=TankDevice.STATUS_ERROR if fault else TankDevice.STATUS_OK,
            extra={
                '/Hydro/HeightMm': None if height is None else int(round(height)),
                '/Hydro/Filling': int(bool(d.get('filling'))),
                '/Hydro/Draining': int(bool(d.get('draining'))),
                '/Hydro/Sloshing': int(bool(d.get('sloshing'))),
                '/Hydro/Fault': int(fault),
                '/Hydro/Temperature2': temps.get(2),
            })
        if self.temp is not None and temps.get(1) is not None:
            self.temp.publish(temps[1])
        if self.temp2 is not None and temps.get(2) is not None:
            self.temp2.publish(temps[2])
        self._frost_check(None if fault else volume_l, temps)
        if d.get('truma') and self.heater:
            self.tank['/Hydro/Truma'] = json.dumps(d['truma'])
            self.heater.apply(d['truma'])

    def _on_wifi_error(self, text):
        self._wifi_fails += 1
        if self.conn_mode == 'auto' and self._wifi_ok and self._wifi_fails < self.WIFI_FAILS:
            return                             # one missed answer is no reason to switch
        if self._wifi_ok is not False:
            self.link_log.write('wifi', 'no answer from %s: %s%s' % (
                self.sensor_host, text, ' - Bluetooth takes over' if self.conn_mode == 'auto' else ''))
            log.warning('sensor at %s does not answer: %s', self.sensor_host, text)
            if self._wifi_ok and self.conn_mode == 'auto':
                self.stats['fallbacks'] += 1
            self._wifi_ok = False
            self.tank['/Hydro/Transport'] = self.transport
        if self.conn_mode == 'wifi':
            self.tank['/Hydro/LinkStatus'] = 'no answer over Wi-Fi from %s' % self.sensor_host

    def _wifi_upload(self, path):
        """Firmware over Wi-Fi: the whole file to the sensor's /update, like its own page does."""
        self.ota = {'wifi': True}
        self.tank['/Hydro/OtaProgress'] = 0
        self.tank['/Hydro/CommandResult'] = 'ota: sending the file over Wi-Fi'
        self.link_log.write('tx', 'ota: firmware file to http://%s/update' % self.sensor_host)

        body = '/tmp/venusosmods-fw-upload.bin'
        try:
            ctype = wifilink.multipart_file(path, body)
        except OSError as e:
            self.ota = None
            self.tank['/Hydro/CommandResult'] = 'error ota: could not read %s: %s' % (path, e)
            return True

        def done(ok, answer):
            self.ota = None
            try:
                os.remove(body)
            except OSError:
                pass
            self.link_log.write('rx', 'ota: %s' % (answer.strip()[:160] if ok else 'FAILED (%s)' % answer))
            if ok and answer.startswith('Update OK'):
                self.tank['/Hydro/OtaProgress'] = 100
                self.tank['/Hydro/CommandResult'] = 'ok ota done, restarting'
                for s in (20, 45, 90):
                    GLib.timeout_add_seconds(s, lambda: (self._link_tick(), False)[1])
            else:
                self.tank['/Hydro/OtaProgress'] = None
                self.tank['/Hydro/CommandResult'] = 'error ota: ' + (answer.strip() or 'no answer')[:200]

        wifilink.http(done, 'http://%s/update' % self.sensor_host, timeout=180, post_file=body, content_type=ctype)
        return True

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        r = proto.parse(manufacturer_data.get(proto.COMPANY_ID))
        if r is None or r.device_id != self.device_id:
            return
        if mac != self.seen_mac or self.tank['/Hydro/Mac'] is None:
            self.seen_mac = mac
            self.tank['/Hydro/Mac'] = mac
        now = monotonic_s()
        if self._last_adv is not None and r.sequence != self._last_seq:
            self.stats['adv'] += 1
            gap = now - self._last_adv
            if gap > self.GAP_S:
                self.stats['gaps'] += 1
                self.stats['longest_gap'] = max(self.stats['longest_gap'], int(gap))
        if self._last_adv is None or now - self._last_adv > self.GAP_S:
            self.link_log.write('adv', 'broadcast %s, rssi %s' % (
                'heard (first since start)' if self._last_adv is None else 'heard again after %d s' % (now - self._last_adv), rssi))
        self._last_adv = now
        self._last_temp = r.temperature
        if r.sequence == self._last_seq:
            return  # same packet seen again, nothing new
        if self.transport == 'wifi':
            self._last_seq = r.sequence
            return  # the reading comes over Wi-Fi now (one source); Bluetooth stays the fallback
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
        self._frost_check(None if r.fault else volume_l, {1: r.temperature, 2: r.temperature2})


if __name__ == '__main__':
    Esp32HydroDriver.main()
