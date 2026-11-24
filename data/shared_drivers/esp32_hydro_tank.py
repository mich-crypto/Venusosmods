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
import time

import esp32_hydro_protocol as proto
import rawlog
from gi.repository import GLib
from venus_service import monotonic_s
from venus_service import (BleDriver, TankDevice, TemperatureDevice, interpolate, parse_table,
                           FLUID_FRESH_WATER, TEMPERATURE_TYPES)

log = logging.getLogger('esp32_hydro')


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
                         '/Hydro/LinkStatus', '/Hydro/LinkTime', '/Hydro/TempNames', '/Hydro/Mac',
                         # live view: raw reading over the Bluetooth link
                         '/Hydro/Signal', '/Hydro/SignalUnit', '/Hydro/RawHeightMm',
                         '/Hydro/LiveHeightMm', '/Hydro/LiveLitres', '/Hydro/LiveTime'),
            setup=lambda svc: svc.add_path('/Hydro/Live', 0, writeable=True, onchangecallback=self._on_live_set))
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
        self.seen_mac = self.mac
        self.gatt = None
        self._collector = None
        self._expected = 0
        self._mode = None              # what the running exchange is: checkin | live | stop
        self.live_until = 0            # monotonic end of the live view (0 = off)

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
            GLib.timeout_add_seconds(20, self._first_link)

    def _first_link(self):
        self._link_tick()
        GLib.timeout_add_seconds(self.link_interval, self._link_tick)
        return False

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
        self._expected = sum(1 for f in frames if not f.startswith(b'Cauth '))
        self._client().exchange(frames, self._on_link_done)

    def _link_tick(self):
        if not self.seen_mac or (self.gatt is not None and self.gatt.busy):
            return True
        self._send('checkin', proto.link_commands(self.sender_password, self.wifi_mode,
                                                  self.wifi_ssid, self.wifi_password))
        return True

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
        if mode in ('live', 'stop'):
            if replies and replies[-1].startswith('{'):
                self.apply_live(replies[-1])
                self.tank['/Hydro/LinkStatus'] = 'ok'
            elif not ok:
                self.tank['/Hydro/LinkStatus'] = 'live view: %s' % (error or 'failed')
            if any(r.startswith('denied') for r in replies):
                self.tank['/Hydro/LinkStatus'] = 'wrong sender password'
                self.live_until = monotonic_s() - 1
            return
        if not ok and len(replies) < self._expected:
            text = str(error or 'failed')
            if 'not found' in text:
                text = 'sender firmware without Bluetooth link (older than 2.7)'
            log.info('link: %s', text)
            self.tank['/Hydro/LinkStatus'] = text
            return
        if any(r.startswith('denied') for r in replies):
            log.warning('link: sender refused the password - set sender_password = the sender\'s Wi-Fi password')
            self.tank['/Hydro/LinkStatus'] = 'wrong sender password'
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
        self.tank['/Hydro/AccessPoint'] = int(bool(info.get('ap')))
        self.tank['/Hydro/WifiIp'] = info.get('ip') or ''
        self.tank['/Hydro/TempNames'] = json.dumps(info.get('tn') or [])
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


def wait_for_sender_update(max_s=1800):
    """Started (by hand, or by a software update) while the sender's firmware is being
    sent: wait, a second Bluetooth connection would break the upload."""
    t0, said = time.time(), -60
    while proto.ota_running() and time.time() - t0 < max_s:
        if time.time() - said >= 60:
            log.warning('sender firmware update running: waiting before starting')
            said = time.time()
        time.sleep(5)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    wait_for_sender_update()
    Esp32HydroDriver.main()
