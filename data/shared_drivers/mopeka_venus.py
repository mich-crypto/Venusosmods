#!/usr/bin/env python3
"""
Venus OS driver for the original Mopeka Check Standard / XL LPG sensor.

Publishes com.victronenergy.tank.<service> with FluidType LPG.
Configure it in config.ini, see config.ini.example.
"""

import collections
import logging
import statistics

import mopeka_std_parser
from venus_tank_base import (VenusTankBaseDriver, FLUID_LPG, interpolate,
                             parse_table)

log = logging.getLogger('mopeka_standard')


class MopekaStandardDriver(VenusTankBaseDriver):
    PRODUCT_NAME = 'Mopeka Standard'
    FLUID_TYPE = FLUID_LPG
    DEFAULT_SERVICE = 'mopeka_standard'
    DEFAULT_TIMEOUT_S = 900   # the sensor slows down to ~1/min when idle

    def __init__(self, section, config):
        super(MopekaStandardDriver, self).__init__(section, config)
        if not self.mac:
            raise SystemExit('[%s] mac = ... is required for Mopeka sensors' % section)
        self.propane_ratio = config.getfloat('propane_ratio', 1.0)
        self.empty_mm = config.getfloat('empty_mm', 38.0)
        self.full_mm = config.getfloat('full_mm', 254.0)
        table = config.get('level_table', '').strip()
        self.level_table = parse_table(table) if table else None
        # The ultrasonic echo jumps around a bit - median over the last N reads
        self._distances = collections.deque(maxlen=config.getint('median_window', 5))
        self._level = None

    def add_extra_paths(self, service):
        service.add_path('/Mopeka/DistanceMm', None)
        service.add_path('/Mopeka/EchoQuality', None)
        service.add_path('/Mopeka/BatteryPercent', None)
        service.add_path('/Mopeka/SyncPressed', None)

    def distance_to_level(self, distance_mm):
        if self.level_table:
            return interpolate(self.level_table, distance_mm)
        if distance_mm <= self.empty_mm:
            return 0.0
        if distance_mm >= self.full_mm:
            return 100.0
        return 100.0 * (distance_mm - self.empty_mm) / (self.full_mm - self.empty_mm)

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        payload = manufacturer_data.get(mopeka_std_parser.MANUFACTURER_ID)
        r = mopeka_std_parser.parse(payload, self.propane_ratio)
        if r is None:
            return
        log.debug('%s rssi=%s %r', mac, rssi, r)

        if r.quality_ok:
            self._distances.append(r.distance_mm)
            distance = statistics.median(self._distances)
            self._level = self.distance_to_level(distance)
        else:
            # Poor echo (bottle moved, sensor not coupled...). Keep the last
            # good level instead of reporting an empty tank.
            distance = None
            log.debug('poor read quality, keeping last level %s', self._level)

        self.publish(
            level=self._level,
            temperature=float(r.temperature),
            battery_voltage=r.battery_voltage,
            raw_value=None if distance is None else round(distance, 1),
            raw_unit='mm',
            rssi=rssi,
            extra={
                '/Mopeka/DistanceMm': None if distance is None else round(distance, 1),
                '/Mopeka/EchoQuality': r.best_amplitude,
                '/Mopeka/BatteryPercent': r.battery_percent,
                '/Mopeka/SyncPressed': int(r.sync_pressed),
            })


if __name__ == '__main__':
    MopekaStandardDriver.main()
