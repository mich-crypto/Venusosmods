#!/usr/bin/env python3
"""
Venus OS driver for 12 V compressor fridges with the "Alpicool" Bluetooth
controller (Alpicool, Brass Monkey, BougeRV, Vevor and probably Hyckes - check
with diagnostics/fridge_probe.py first).

Read-only monitoring: every poll_interval seconds it connects, sends a status
query, publishes the answer and disconnects again. Each zone becomes a
com.victronenergy.temperature service of type fridge or freezer, so it shows
up with the other environment sensors in the GUI and on VRM.

The fridge accepts only one Bluetooth connection at a time. Between polls the
phone app can connect; while the phone is connected the poll fails and the
values age out after `timeout` seconds.
"""

import logging

import alpicool_protocol as proto
from ble_gatt import GattClient
from gi.repository import GLib
from venus_service import (BleDriver, TemperatureDevice, TEMP_FREEZER,
                           TEMP_FRIDGE, TEMPERATURE_TYPES)

log = logging.getLogger('fridge')

EXTRA_PATHS = ('/Fridge/TargetTemperature', '/Fridge/PoweredOn', '/Fridge/EcoMode',
               '/Fridge/Locked', '/Fridge/BatterySaver', '/Fridge/SupplyVoltage',
               '/Fridge/BatteryPercent', '/Fridge/RunningStatus')


class FridgeDriver(BleDriver):
    DEFAULT_SECTION = 'fridge'
    USES_SCANNER = True   # keeps BlueZ aware of the fridge between connections

    def __init__(self, section, config):
        super(FridgeDriver, self).__init__(section, config)
        if not self.mac:
            raise SystemExit('[%s] mac = ... is required for the fridge' % section)
        self.poll_s = max(10, config.getint('poll_interval', 60))
        persistent = config.getboolean('stay_connected', False)
        self.uid = config.get('service', 'fridge')
        self.zone_types = [config.get('zone1_type', 'auto'), config.get('zone2_type', 'auto')]
        self.zone_names = [config.get('zone1_name', 'Fridge'), config.get('zone2_name', 'Freezer')]
        self.timeout = config.getint('timeout', max(300, 4 * self.poll_s))
        self.zones = []
        self._assembler = proto.FrameAssembler()
        self._failures = 0
        self.gatt = GattClient(self.bus, self.mac, proto.WRITE_UUID, proto.NOTIFY_UUID,
                               self._on_notify, adapter=config.get('adapter', 'hci0'),
                               keep_connected=persistent)
        # Zone 1 always exists; zone 2 is created once the fridge reports it
        self._zone(0)

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        pass   # the scanner only keeps the device object alive in BlueZ

    def _zone(self, i):
        while len(self.zones) <= i:
            n = len(self.zones)
            dev = TemperatureDevice(
                self.bus, '%s_zone%d' % (self.uid, n + 1),
                product_name='Compressor fridge',
                connection='Bluetooth LE %s' % self.mac,
                temperature_type=TEMPERATURE_TYPES.get(self.zone_types[n], TEMP_FRIDGE),
                default_instance=self.cfg.getint('device_instance', 20) + n,
                custom_name=self.zone_names[n],
                timeout_s=self.timeout,
                extra_paths=EXTRA_PATHS)
            self.zones.append(dev)
            self.devices.append(dev)
        return self.zones[i]

    # ---------------------------------------------------------------- poll

    def start(self):
        GLib.timeout_add_seconds(3, self._poll_once)          # first poll soon
        GLib.timeout_add_seconds(self.poll_s, self._poll)

    def _poll_once(self):
        self._poll()
        return False

    def _poll(self):
        if not self.gatt.busy:
            self._assembler = proto.FrameAssembler()
            self.gatt.exchange(proto.chunks(proto.query_frame()), self._on_done)
        return True

    def _on_done(self, ok, error):
        if ok:
            self._failures = 0
            return
        self._failures += 1
        # The phone app being connected is the normal reason; don't spam the log
        level = logging.WARNING if self._failures in (1, 10) or self._failures % 60 == 0 else logging.DEBUG
        log.log(level, 'fridge poll failed (%d in a row): %s', self._failures, error)

    def _on_notify(self, data):
        for cmd, payload in self._assembler.feed(data):
            if cmd not in (proto.CMD_QUERY, proto.CMD_SET_OTHERS):
                continue
            status = proto.parse_status(payload)
            if status is None:
                log.warning('short status frame: %s', payload.hex())
                continue
            log.debug('%r', status)
            self._publish(status)
            self.gatt.finish()

    def _publish(self, st):
        for i, zone in enumerate(st.zones):
            dev = self._zone(i)
            ttype = None
            if self.zone_types[i] == 'auto' and zone.target is not None:
                ttype = TEMP_FREEZER if zone.target < 0 else TEMP_FRIDGE
            if zone.current is None:
                continue   # no sensor reading for this zone - let it time out
            dev.publish(zone.current, temperature_type=ttype, extra={
                '/Fridge/TargetTemperature': zone.target,
                '/Fridge/PoweredOn': int(st.powered_on),
                '/Fridge/EcoMode': int(st.eco_mode),
                '/Fridge/Locked': int(st.locked),
                '/Fridge/BatterySaver': st.battery_saver,
                '/Fridge/SupplyVoltage': st.supply_voltage,
                '/Fridge/BatteryPercent': st.battery_percent,
                '/Fridge/RunningStatus': st.running_status,
            })

    def shutdown(self):
        self.gatt.close()


if __name__ == '__main__':
    FridgeDriver.main()
