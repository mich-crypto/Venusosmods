"""
BLE advertisement format of the ESP32-S3 hydrostatic tank sender.

Must match firmware/esp32_hydro_tank/esp32_hydro_tank.ino (buildPayload()).

Manufacturer data, company id 0xFFFF (reserved for testing / unregistered),
payload little-endian, 16 bytes (18 with a second temperature probe, 19 with the grey tank):

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
   18   1  grey water tank, rod sender (firmware 4.17+, optional like temperature 2):
           bits 0-3 wet rods (bit 0 = lowest), bit 4 fitted, bit 5 fault
           (a wet rod above a dry one), bits 6-7 number of rods - 1
"""

import struct

# Bluetooth link (firmware 2.7+): short GATT connections for the check-in, the
# Wi-Fi setting and short commands (the firmware update only sends "fwget <url>":
# the file itself goes over Wi-Fi, firmware 4.1+). Write: b'C' + command text.
# Reply: read, or notified in chunks ended by EOT (0x04).
LINK_SERVICE_UUID = '5f6d0001-8d4b-4bd1-9a1c-7ad0a8e3c101'
LINK_CMD_UUID = '5f6d0002-8d4b-4bd1-9a1c-7ad0a8e3c101'
LINK_REPLY_UUID = '5f6d0003-8d4b-4bd1-9a1c-7ad0a8e3c101'
EOT = b'\x04'
WIFI_MODES = ('auto', 'van', 'ap', 'off')


def link_commands(password, wifi_mode=None, ssid=None, wifi_password=None):
    """The frames of one check-in: auth, heartbeat, Wi-Fi settings, info."""
    frames = [b'Cauth ' + password.encode('utf-8'), b'Chb']
    if wifi_mode in WIFI_MODES:
        frames.append(b'Cwifi mode ' + wifi_mode.encode())
        if wifi_mode == 'van':
            frames.append(b'Cwifi ssid ' + (ssid or '-').encode('utf-8'))
            frames.append(b'Cwifi pass ' + (wifi_password or '').encode('utf-8'))
    frames.append(b'Cinfo')
    return frames


class ReplyCollector(object):
    """Joins notified chunks into replies (each ended by EOT)."""

    def __init__(self):
        self.buf = b''
        self.replies = []

    def feed(self, data):
        self.buf += data
        while EOT in self.buf:
            part, self.buf = self.buf.split(EOT, 1)
            self.replies.append(part.decode('utf-8', 'replace'))
        return self.replies

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


GREY_FITTED = 0x10
GREY_FAULT = 0x20


class GreyReading(object):
    """The grey tank's rods: mask of the wet ones (bit 0 = lowest), how many rods, and a fault
    (a wet rod above a dry one: a dirty rod or a broken wire)."""
    __slots__ = ('mask', 'rods', 'fault')

    def __init__(self, mask=0, rods=4, fault=False):
        self.mask, self.rods, self.fault = mask, rods, fault

    @property
    def wet(self):
        """Wet rods counted by the highest wet one (a lower rod that reads dry is the fault)."""
        return self.mask.bit_length()

    def __eq__(self, other):
        return isinstance(other, GreyReading) and (self.mask, self.rods, self.fault) == (other.mask, other.rods, other.fault)

    def __repr__(self):
        return 'GreyReading(mask=%s, rods=%d%s)' % (bin(self.mask), self.rods, ', FAULT' if self.fault else '')


class HydroReading(object):
    __slots__ = ('device_id', 'sequence', 'level', 'volume_l', 'capacity_l',
                 'height_mm', 'temperature', 'temperature2', 'filling', 'draining',
                 'fault', 'sloshing', 'grey')

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
    grey = None
    if len(data) >= LENGTH + 3 and data[LENGTH + 2] & GREY_FITTED:
        g = data[LENGTH + 2]
        grey = GreyReading(mask=g & 0x0F, rods=((g >> 6) & 3) + 1, fault=bool(g & GREY_FAULT))
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
        sloshing=bool(flags & FLAG_SLOSHING),
        grey=grey)


def build(device_id, level, volume_l, capacity_l, height_mm, temperature=None,
          filling=False, draining=False, fault=False, sloshing=False, sequence=0,
          temperature2=None, long=False, grey=None):
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
    if temperature2 is not None or long or grey is not None:      # the 18-byte form
        data += struct.pack('<h', int(round((temperature2 or 0) * 100)))
    if grey is not None:                      # (mask, rods, fault): the 19-byte form of firmware 4.17+
        mask, rods, fault = grey
        data += struct.pack('<B', (mask & 0x0F) | GREY_FITTED | (GREY_FAULT if fault else 0) | (((rods - 1) & 3) << 6))
    return data
