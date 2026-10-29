#!/usr/bin/env python3
"""
Venus OS driver for the original Mopeka Check Standard / XL LPG sensor.

Publishes com.victronenergy.tank.<service> with FluidType LPG.
Configure it in config.ini, see config.ini.example.

MAC address: either set `mac` in config.ini, or leave it empty and press the
sensor's sync button once. The driver then pairs with the sensor that sent
the sync press and stores its MAC in localsettings
(/Settings/Devices/<service>/Mac), so it survives restarts. Sensors from
neighbours at a campsite are ignored.
"""

import collections
import logging

import mopeka_std_parser
from venus_service import (BleDriver, SettingsDevice, TankDevice, FLUID_LPG,
                           interpolate, parse_table)

log = logging.getLogger('mopeka_standard')


def median(values):
    # Venus OS's Python has no 'statistics' module
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


class MopekaStandardDriver(BleDriver):
    DEFAULT_SECTION = 'mopeka_standard'

    def __init__(self, section, config):
        super(MopekaStandardDriver, self).__init__(section, config)
        uid = config.get('service', 'mopeka_standard')
        self.paired = SettingsDevice(self.bus, {
            'mac': ['/Settings/Devices/%s/Mac' % uid, '', 0, 0],
        }, None, timeout=60)
        if not self.mac:
            self.mac = str(self.paired['mac'] or '').upper() or None
            if self.mac:
                log.info('Using paired sensor %s', self.mac)
            else:
                log.info('No MAC configured: press the sync button on the Mopeka to pair')
        self._announced = set()
        self.propane_ratio = config.getfloat('propane_ratio', 1.0)
        self.empty_mm = config.getfloat('empty_mm', 38.0)
        self.full_mm = config.getfloat('full_mm', 254.0)
        table = config.get('level_table', '').strip()
        self.level_table = parse_table(table) if table else None
        # Bottle shape relative to the calibration: fraction of the way from
        # empty_mm to full_mm -> percent. Only full_mm needs measuring.
        curve = config.get('level_curve', '').strip()
        self.level_curve = parse_table(curve) if curve else None
        # The ultrasonic echo jumps around a bit - median over the last N reads
        self._distances = collections.deque(maxlen=config.getint('median_window', 5))
        self._level = None

        self.tank = TankDevice(
            self.bus, uid,
            product_name='Mopeka Standard',
            connection='Bluetooth LE %s' % (self.mac or '(press sync to pair)'),
            fluid_type=config.getint('fluid_type', FLUID_LPG),
            capacity_l=config.getfloat('capacity_l', 0.0),
            default_instance=config.getint('device_instance', 20),
            custom_name=config.get('custom_name', ''),
            # the sensor slows down to ~1 reading/min when idle
            timeout_s=config.getint('timeout', 900),
            extra_paths=('/Mopeka/DistanceMm', '/Mopeka/EchoQuality',
                         '/Mopeka/BatteryPercent', '/Mopeka/SyncPressed'))
        self.devices.append(self.tank)

    def distance_to_level(self, distance_mm):
        if self.level_table:
            return interpolate(self.level_table, distance_mm)
        if self.level_curve:
            x = (distance_mm - self.empty_mm) / (self.full_mm - self.empty_mm)
            return interpolate(self.level_curve, max(0.0, min(1.0, x)))
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
        if self.mac is None:
            if not r.sync_pressed:
                if mac not in self._announced:
                    self._announced.add(mac)
                    log.info('Heard Mopeka %s (rssi %s) - press its sync button to pair', mac, rssi)
                return
            self._pair(mac)
        elif mac != self.mac:
            return
        log.debug('%s rssi=%s %r', mac, rssi, r)

        if r.quality_ok:
            self._distances.append(r.distance_mm)
            distance = median(self._distances)
            self._level = self.distance_to_level(distance)
        else:
            # Poor echo (bottle moved, sensor not coupled...). Keep the last
            # good level instead of reporting an empty tank.
            distance = None
            log.debug('poor read quality, keeping last level %s', self._level)

        self.tank.publish(
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


    def _pair(self, mac):
        self.mac = mac
        try:
            self.paired['mac'] = mac
        except Exception as e:   # still works until the next restart
            log.warning('Could not store the paired MAC: %s', e)
        self.tank['/Mgmt/Connection'] = 'Bluetooth LE %s' % mac
        log.info('Paired with Mopeka %s (stored in /Settings/Devices/%s/Mac)', mac, self.tank.uid)


if __name__ == '__main__':
    MopekaStandardDriver.main()
