"""
BLE advertisement format of the ESP32-S3 hydrostatic tank sender.

Must match firmware/esp32_hydro_tank/esp32_hydro_tank.ino (buildPayload()).

Manufacturer data, company id 0xFFFF (reserved for testing / unregistered),
payload little-endian, 16 bytes (18 with a second temperature probe):

  off size field
    0   2  magic 'V','H'
    2   1  protocol version (1)
    3   1  device id (lets you run several tanks)
    4   1  flags  bit0 temperature valid
                  bit1 filling
                  bit2 draining
                  bit3 sensor fault (open loop / out of range)
                  bit4 sloshing (filter in slow mode)
                  bit5 temperature 2 valid
    5   1  sequence counter (wraps)
    6   2  level, 0.01 %            (0..10000)
    8   2  volume, 0.1 L
   10   2  capacity, 0.1 L
   12   2  water height, mm
   14   2  temperature, 0.01 C, int16 (only if bit0 set)
   16   2  temperature 2, 0.01 C, int16 (only if bit5 set; optional, added
           later without a version change: shorter packets have no probe 2)
"""

import struct

COMPANY_ID = 0xFFFF
MAGIC = b'VH'
VERSION = 1
_FMT = '<2sBBBBHHHHh'
LENGTH = struct.calcsize(_FMT)

FLAG_TEMP_VALID = 0x01
FLAG_FILLING = 0x02
FLAG_DRAINING = 0x04
FLAG_FAULT = 0x08
FLAG_SLOSHING = 0x10
FLAG_TEMP2_VALID = 0x20


class HydroReading(object):
    __slots__ = ('device_id', 'sequence', 'level', 'volume_l', 'capacity_l',
                 'height_mm', 'temperature', 'temperature2', 'filling', 'draining',
                 'fault', 'sloshing')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return 'HydroReading(%s)' % ', '.join(
            '%s=%r' % (k, getattr(self, k)) for k in self.__slots__)


def parse(data):
    if data is None or len(data) < LENGTH:
        return None
    (magic, version, device_id, flags, seq, level, volume, capacity,
     height, temp) = struct.unpack_from(_FMT, data)
    if magic != MAGIC or version != VERSION:
        return None
    temp2 = None
    if len(data) >= LENGTH + 2 and flags & FLAG_TEMP2_VALID:
        temp2 = struct.unpack_from('<h', data, LENGTH)[0] / 100.0
    return HydroReading(
        device_id=device_id,
        sequence=seq,
        level=level / 100.0,
        volume_l=volume / 10.0,
        capacity_l=capacity / 10.0,
        height_mm=height,
        temperature=temp / 100.0 if flags & FLAG_TEMP_VALID else None,
        temperature2=temp2,
        filling=bool(flags & FLAG_FILLING),
        draining=bool(flags & FLAG_DRAINING),
        fault=bool(flags & FLAG_FAULT),
        sloshing=bool(flags & FLAG_SLOSHING))


def build(device_id, level, volume_l, capacity_l, height_mm, temperature=None,
          filling=False, draining=False, fault=False, sloshing=False, sequence=0,
          temperature2=None, long=False):
    """Reference encoder - used by the tests and handy for a fake sender."""
    flags = ((FLAG_TEMP_VALID if temperature is not None else 0) |
             (FLAG_FILLING if filling else 0) |
             (FLAG_DRAINING if draining else 0) |
             (FLAG_FAULT if fault else 0) |
             (FLAG_SLOSHING if sloshing else 0) |
             (FLAG_TEMP2_VALID if temperature2 is not None else 0))
    data = struct.pack(_FMT, MAGIC, VERSION, device_id, flags, sequence & 0xFF,
                       int(round(level * 100)), int(round(volume_l * 10)),
                       int(round(capacity_l * 10)), int(height_mm),
                       int(round((temperature or 0) * 100)))
    if temperature2 is not None or long:      # the 18-byte form the current firmware sends
        data += struct.pack('<h', int(round((temperature2 or 0) * 100)))
    return data
