#!/usr/bin/env python3
"""
Venus OS driver for the ESP32-S3 hydrostatic water tank sender
(firmware/esp32_hydro_tank). All filtering and volume calculation happens on
the ESP32; this side only decodes and publishes.

Publishes com.victronenergy.tank.<service> and, if a DS18B20 is fitted and
temperature_service = yes, com.victronenergy.temperature.<service>_temp
(the tank's own /Temperature path is only shown by the GUI for Mopeka tanks).
"""

import logging

import esp32_hydro_protocol as proto
import rawlog
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
                         '/Hydro/Sloshing', '/Hydro/Fault'))
        self.devices.append(self.tank)
        self.raw = rawlog.RawLog('water')

        self.temp = None
        if config.getboolean('temperature_service', False):
            self.temp = TemperatureDevice(
                self.bus, uid + '_temp', product_name='ESP32 Tank Temperature',
                connection=connection,
                temperature_type=TEMPERATURE_TYPES[config.get('temperature_type', 'generic')],
                default_instance=config.getint('temperature_instance', 20),
                custom_name=config.get('temperature_name', 'Water temperature'),
                timeout_s=timeout)
            self.devices.append(self.temp)

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        r = proto.parse(manufacturer_data.get(proto.COMPANY_ID))
        if r is None or r.device_id != self.device_id:
            return
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
            })

        # Only refresh the temperature service with a valid reading, so a
        # dead probe ends in "disconnected" via the timeout instead of
        # showing a stale value as current.
        if self.temp is not None and r.temperature is not None:
            self.temp.publish(r.temperature)


if __name__ == '__main__':
    Esp32HydroDriver.main()
