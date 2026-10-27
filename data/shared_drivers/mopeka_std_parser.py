"""
Decoder for the *original* Mopeka Check Standard / XL BLE advertisement.

The mopeka-iot-ble library only understands the newer Pro series (company id
0x0059), so this is a port of ESPHome's mopeka_std_check component
(esphome/components/mopeka_std_check). Pure Python, no Venus OS dependencies,
so it can be unit-tested on any PC.
"""

MANUFACTURER_ID = 0x000D        # payload is advertised with the TI company id
DATA_LENGTH = 23                # manufacturer payload length, company id excluded

SENSOR_TYPES = {
    0x02: 'Standard',
    0x03: 'XL',
    0x44: 'Standard',
    0x46: 'eTrailer',
}


class MopekaStdReading(object):
    __slots__ = ('sensor_type', 'battery_voltage', 'battery_percent',
                 'temperature', 'slow_update', 'sync_pressed',
                 'best_time', 'best_amplitude', 'usable_values', 'distance_mm')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def quality_ok(self):
        return self.distance_mm is not None

    def __repr__(self):
        return 'MopekaStdReading(%s)' % ', '.join(
            '%s=%r' % (k, getattr(self, k)) for k in self.__slots__)


def lpg_speed_of_sound(temperature_c, propane_ratio):
    """Speed of sound in liquid LPG (m/s). propane_ratio 1.0 = pure propane, 0.0 = pure butane."""
    t = temperature_c
    m = propane_ratio
    return 1040.71 - 4.87 * t - 137.5 * m - 0.0107 * t * t - 1.63 * t * m


def _temperature(raw):
    if raw == 0:
        return -40
    return int((raw - 25.0) * 1.776964)   # int() truncates toward zero like the C cast


def _battery(raw):
    voltage = raw / 256.0 * 2.0 + 1.5
    percent = (voltage - 2.2) / 0.65 * 100.0   # CR2032 curve
    return voltage, max(0, min(100, int(percent)))


def _measurements(data):
    """Unpack the 12 (time, amplitude) pairs: 3 groups of 5 bytes, 4 x (5+5 bit), LSB first."""
    pairs = []
    for g in range(3):
        word = int.from_bytes(data[4 + 5 * g:9 + 5 * g], 'little')
        for i in range(4):
            t = (word >> (10 * i)) & 0x1F
            v = (word >> (10 * i + 5)) & 0x1F
            pairs.append((t + 1, v))
    return pairs


def parse(data, propane_ratio=1.0):
    """
    Decode the manufacturer payload (bytes, company id already stripped).
    Returns MopekaStdReading or None if this is not a Mopeka Standard packet.
    """
    if data is None or len(data) != DATA_LENGTH:
        return None
    sensor_type = data[1] & 0xCF
    if sensor_type not in SENSOR_TYPES:
        return None

    battery_voltage, battery_percent = _battery(data[2])
    temperature = _temperature(data[3] & 0x3F)

    # Time is accumulated until an echo (non-zero amplitude) is reported,
    # which allows echo times longer than 5 bits.
    usable = 0
    best_amp = 0
    best_time = 0
    acc = 0
    for t, v in _measurements(data):
        acc += t
        if v != 0:
            usable += 1
            if v > best_amp:
                best_amp = v
                best_time = acc
            acc = 0

    distance = None
    if usable >= 1 and best_amp >= 2 and best_time >= 2:
        # best_time is in 10 us ticks -> mm
        distance = lpg_speed_of_sound(temperature, propane_ratio) * best_time / 100.0

    return MopekaStdReading(
        sensor_type=SENSOR_TYPES[sensor_type],
        battery_voltage=round(battery_voltage, 3),
        battery_percent=battery_percent,
        temperature=temperature,
        slow_update=bool(data[3] & 0x40),
        sync_pressed=bool(data[3] & 0x80),
        best_time=best_time,
        best_amplitude=best_amp,
        usable_values=usable,
        distance_mm=distance)
