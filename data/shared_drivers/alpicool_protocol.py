"""
BLE protocol of the "Alpicool" compressor-fridge controller.

The same controller is sold under many names (Alpicool, Brass Monkey,
BougeRV, Vevor, ...) and is controlled by the "CAR FRIDGE FREEZER" app. Whether
a Hyckes fridge uses it can be checked with diagnostics/fridge_probe.py.

Sources (reverse engineered from the app plus captured traffic):
  https://github.com/klightspeed/BrassMonkeyFridgeMonitor
  https://github.com/hairydood-debug/Alpicool50l12vfridgefreezer (PROTOCOL.md)

GATT: service 0x1234, write 0x1235, notify 0x1236. No pairing or PIN.
Frame (both directions): FE FE | LEN | CMD | data... | SUM_hi SUM_lo
  LEN = 1 (CMD) + len(data) + 2 = frame length - 3
  SUM = 16-bit big-endian sum of all bytes before it
Pure Python, no D-Bus: unit-testable on any PC.
"""

SERVICE_UUID = '00001234-0000-1000-8000-00805f9b34fb'
WRITE_UUID = '00001235-0000-1000-8000-00805f9b34fb'
NOTIFY_UUID = '00001236-0000-1000-8000-00805f9b34fb'

# Advertised names the official app accepts
NAME_PREFIXES = ('A1-', 'AK1-', 'AK2-', 'AK3-', 'WT-', 'MF-', 'KGS-', 'KGD-', 'W-', 'WH-')

CMD_BIND = 0x00
CMD_QUERY = 0x01
CMD_SET_OTHERS = 0x02
CMD_SET_LEFT = 0x05
CMD_SET_RIGHT = 0x06

NO_SENSOR = -128
MAX_WRITE = 20          # default ATT MTU 23 -> 20 bytes per write


def checksum(data):
    return sum(data) & 0xFFFF


def build_frame(cmd, data=b''):
    body = bytes([0xFE, 0xFE, len(data) + 3, cmd]) + bytes(data)
    s = checksum(body)
    return body + bytes([s >> 8, s & 0xFF])


def query_frame():
    return build_frame(CMD_QUERY)       # FE FE 03 01 02 00


def chunks(frame, size=MAX_WRITE):
    return [frame[i:i + size] for i in range(0, len(frame), size)]


class FrameAssembler(object):
    """Reassembles notifications (long frames arrive split in 20-byte parts)."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        """Add received bytes, return a list of (cmd, data) for every complete valid frame."""
        self._buf += data
        frames = []
        while True:
            start = self._buf.find(b'\xfe\xfe')
            if start < 0:
                # keep a trailing 0xFE, it may be the first header byte
                self._buf = self._buf[-1:] if self._buf[-1:] == b'\xfe' else bytearray()
                return frames
            del self._buf[:start]
            if len(self._buf) < 3:
                return frames
            total = self._buf[2] + 3
            if total < 6:
                del self._buf[:2]      # impossible length, resync
                continue
            if len(self._buf) < total:
                return frames
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            if checksum(frame[:-2]) == (frame[-2] << 8 | frame[-1]):
                frames.append((frame[3], frame[4:-2]))


def _s8(b):
    return b - 256 if b & 0x80 else b


def _f_to_c(t):
    return (t - 32) * 5.0 / 9.0


class Zone(object):
    __slots__ = ('target', 'current', 'min', 'max')

    def __init__(self, target, current, min=None, max=None):
        self.target = target
        self.current = current
        self.min = min          # selectable set point range reported by the fridge
        self.max = max

    def __repr__(self):
        return 'Zone(target=%r, current=%r, range=%r..%r)' % (
            self.target, self.current, self.min, self.max)


class FridgeStatus(object):
    __slots__ = ('locked', 'powered_on', 'eco_mode', 'battery_saver',
                 'fahrenheit', 'zones', 'battery_percent', 'supply_voltage',
                 'running_status', 'model', 'raw')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return 'FridgeStatus(%s)' % ', '.join(
            '%s=%r' % (k, getattr(self, k)) for k in self.__slots__)


def parse_status(data):
    """
    Decode the data of a CMD_QUERY / CMD_SET_OTHERS response.
    Temperatures are converted to Celsius. Returns None if too short.
    """
    if len(data) < 18:
        return None
    fahrenheit = data[9] == 1

    def temp(b):
        t = _s8(b)
        if t == NO_SENSOR:
            return None
        return round(_f_to_c(t), 1) if fahrenheit else float(t)

    zones = [Zone(temp(data[4]), temp(data[14]), temp(data[6]), temp(data[5]))]
    if len(data) >= 27:
        right = Zone(temp(data[18]), temp(data[26]), temp(data[20]), temp(data[19]))
        if right.current is not None:
            zones.append(right)

    battery = data[15]
    return FridgeStatus(
        locked=bool(data[0]),
        powered_on=bool(data[1]),
        eco_mode=data[2] == 1,
        battery_saver=data[3],
        fahrenheit=fahrenheit,
        zones=zones,
        battery_percent=None if battery == 0x7F else battery,
        supply_voltage=round(data[16] + data[17] / 10.0, 1),
        running_status=data[27] if len(data) > 27 else None,
        model=_s8(data[28]) if len(data) > 28 else None,
        raw=bytes(data))


# ---------------------------------------------------------------- commands
# Set points are sent in the fridge's display unit; the driver works in C.

def _to_wire(temp_c, fahrenheit):
    t = int(round(temp_c * 9.0 / 5.0 + 32 if fahrenheit else temp_c))
    return t & 0xFF


def set_target_frame(zone, temp_c, fahrenheit=False):
    """Set point of zone 0 (left / fridge) or 1 (right / freezer)."""
    cmd = CMD_SET_LEFT if zone == 0 else CMD_SET_RIGHT
    return build_frame(cmd, bytes([_to_wire(temp_c, fahrenheit)]))


def set_others_frame(raw, powered_on=None, eco_mode=None, locked=None, battery_saver=None):
    """
    The fridge has no per-option command: power, eco, lock and battery saver
    are changed by sending the whole settings block, built from the last
    status (`raw` = FridgeStatus.raw) with the requested fields changed.
    Layout per the decompiled app (encodeSetOthers): status idx 0-13; for
    dual-zone models also idx 18-25, hotTarget (29), model (28) and a tail.
    """
    block = bytearray(raw[0:14])
    if powered_on is not None:
        block[1] = 1 if powered_on else 0
    if eco_mode is not None:
        block[2] = 1 if eco_mode else 0
    if locked is not None:
        block[0] = 1 if locked else 0
    if battery_saver is not None:
        block[3] = int(battery_saver)
    model = _s8(raw[28]) if len(raw) > 28 else 1
    if model != 1 and len(raw) >= 30:
        block += raw[18:26] + bytes([raw[29], raw[28]])
        if model >= 9:
            # runningStatus, sleep (idx 31), disinfect (idx 33), 6 zero bytes
            at = lambda i: raw[i] if len(raw) > i else 0
            block += bytes([at(27), at(31), at(33)]) + bytes(6)
        else:
            block += b'\x00'
    return build_frame(CMD_SET_OTHERS, bytes(block))
