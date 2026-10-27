#!/usr/bin/env python3
"""
Venus OS driver for the ESP32-S3 hydrostatic water tank sender
(firmware/esp32_hydro_tank). All filtering and volume calculation happens on
the ESP32; this side only decodes and publishes.
"""

import logging

import esp32_hydro_protocol as proto
from venus_tank_base import (VenusTankBaseDriver, FLUID_FRESH_WATER,
                             STATUS_OK, STATUS_ERROR)

log = logging.getLogger('esp32_hydro')


class Esp32HydroDriver(VenusTankBaseDriver):
    PRODUCT_NAME = 'ESP32 Hydrostatic Tank'
    FLUID_TYPE = FLUID_FRESH_WATER
    DEFAULT_SERVICE = 'esp32_hydro'
    DEFAULT_TIMEOUT_S = 120   # the ESP32 advertises every 0.5 s

    def __init__(self, section, config):
        super(Esp32HydroDriver, self).__init__(section, config)
        self.device_id = config.getint('device_id', 1)
        self._last_seq = None

    def add_extra_paths(self, service):
        service.add_path('/Hydro/HeightMm', None)
        service.add_path('/Hydro/Filling', None)
        service.add_path('/Hydro/Draining', None)
        service.add_path('/Hydro/Sloshing', None)
        service.add_path('/Hydro/Fault', None)

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        r = proto.parse(manufacturer_data.get(proto.COMPANY_ID))
        if r is None or r.device_id != self.device_id:
            return
        if r.sequence == self._last_seq:
            return  # same packet seen again, nothing new
        self._last_seq = r.sequence
        log.debug('%s rssi=%s %r', mac, rssi, r)

        capacity_m3 = r.capacity_l / 1000.0 if r.capacity_l else None
        self.publish(
            level=None if r.fault else r.level,
            remaining_m3=None if r.fault else r.volume_l / 1000.0,
            capacity_m3=capacity_m3,
            temperature=r.temperature,
            raw_value=r.height_mm,
            raw_unit='mm',
            rssi=rssi,
            status=STATUS_ERROR if r.fault else STATUS_OK,
            extra={
                '/Hydro/HeightMm': r.height_mm,
                '/Hydro/Filling': int(r.filling),
                '/Hydro/Draining': int(r.draining),
                '/Hydro/Sloshing': int(r.sloshing),
                '/Hydro/Fault': int(r.fault),
            })


if __name__ == '__main__':
    Esp32HydroDriver.main()
