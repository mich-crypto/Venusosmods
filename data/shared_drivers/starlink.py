"""
Read the Starlink dish: status over gRPC(-web), with a minimal protobuf reader.

Venus OS has no grpc or protobuf libraries, so the one request we need
(Request{get_status}) is encoded by hand and the reply is decoded by field
number. Starlink does not publish this API; the numbers come from the
.proto files other people extracted from the dish, and fields that are missing
or that a newer firmware moved simply show as "no data".

Pure Python (no D-Bus, no network): the page's server sends the bytes with curl.
"""

import collections
import math
import struct
import time

SERVICE_PATH = '/SpaceX.API.Device.Device/Handle'

OUTAGE_CAUSES = {0: 'unknown', 1: 'booting', 2: 'stowed', 3: 'thermal shutdown', 4: 'no schedule',
                 5: 'no satellites', 6: 'obstructed', 7: 'no downlink', 8: 'no pings',
                 9: 'actuator activity', 10: 'cable test', 11: 'sleeping', 13: 'sky search',
                 14: 'RF inhibited'}
UPDATE_STATES = {0: 'unknown', 1: 'idle', 2: 'fetching', 3: 'pre-check', 4: 'writing',
                 5: 'post-check', 6: 'reboot required', 7: 'disabled', 8: 'faulted'}
# DishAlerts: field number -> name
ALERTS = {1: 'motors stuck', 2: 'thermal shutdown', 3: 'thermal throttle', 4: 'unexpected location',
          5: 'mast not vertical', 6: 'slow ethernet speeds', 7: 'roaming', 8: 'install pending',
          9: 'heating', 10: 'power supply thermal throttle', 11: 'power save idle',
          14: 'telemetry stale', 16: 'low motor current', 17: 'signal lower than predicted',
          18: 'slow ethernet (100 Mbps)', 19: 'obstruction map reset', 20: 'water detected (dish)',
          21: 'water detected (router)', 22: 'router port slow', 23: 'no ethernet link'}
# alerts that are only information, not a problem
INFO_ALERTS = ('heating', 'roaming', 'install pending', 'power save idle', 'obstruction map reset')


# ---------------------------------------------------------------- protobuf

def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field_bytes(number, payload):
    return varint((number << 3) | 2) + varint(len(payload)) + payload


def status_request():
    """gRPC message: Request { get_status = 1004 { } } in a length-prefixed frame."""
    return frame(field_bytes(1004, b''))


def history_request():
    """gRPC message: Request { get_history = 1007 { } }."""
    return frame(field_bytes(1007, b''))


def frame(message):
    return b'\x00' + struct.pack('>I', len(message)) + message


def split_frames(data):
    """-> (messages, trailers). gRPC-web puts the status in a final frame flagged 0x80."""
    messages, trailers, pos = [], {}, 0
    while pos < len(data):
        if len(data) - pos < 5:
            raise ValueError('truncated reply')
        flag, length = data[pos], struct.unpack('>I', data[pos + 1:pos + 5])[0]
        body = data[pos + 5:pos + 5 + length]
        if len(body) < length:
            raise ValueError('truncated reply')
        pos += 5 + length
        if flag & 0x80:
            for line in body.decode('ascii', 'replace').split('\r\n'):
                if ':' in line:
                    k, v = line.split(':', 1)
                    trailers[k.strip().lower()] = v.strip()
        else:
            messages.append(body)
    return messages, trailers


def parse_message(buf):
    """-> {field number: [value, ...]}; varints as int, 32/64-bit as bytes, length-delimited as bytes."""
    out = collections.defaultdict(list)
    pos, n = 0, len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            value, pos = buf[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise ValueError('unsupported protobuf wire type %d' % wire)
        if pos > n:
            raise ValueError('truncated protobuf')
        out[number].append(value)
    return out


def _read_varint(buf, pos):
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError('truncated protobuf')
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError('bad varint')


def _first(msg, number):
    vals = msg.get(number)
    return vals[0] if vals else None


def _float(msg, number):
    v = _first(msg, number)
    if isinstance(v, bytes) and len(v) == 4:
        f = struct.unpack('<f', v)[0]
        return None if math.isnan(f) or math.isinf(f) else f
    return None


def _int(msg, number):
    v = _first(msg, number)
    return v if isinstance(v, int) else None


def _sub(msg, number):
    v = _first(msg, number)
    return parse_message(v) if isinstance(v, bytes) else None


def _text(msg, number):
    v = _first(msg, number)
    return v.decode('utf-8', 'replace') if isinstance(v, bytes) else None


# ------------------------------------------------------------------- status

def decode_status(message):
    """Reply message (Response) -> plain dict. Raises ValueError if it holds no dish status."""
    resp = parse_message(message)
    inner = _first(resp, 2004)
    if not isinstance(inner, bytes):
        raise ValueError('the reply has no dish status')
    st = parse_message(inner)
    info = _sub(st, 1) or {}
    state = _sub(st, 2) or {}
    obstruction = _sub(st, 1004) or {}
    outage = _sub(st, 1014)
    gps = _sub(st, 1015) or {}
    align = _sub(st, 1027) or {}
    ready = _sub(st, 1019) or {}
    alerts_msg = _sub(st, 1005) or {}

    def num(msg, number, scale=1.0, default=0.0):
        v = _float(msg, number)
        return default if v is None and st else (None if v is None else v * scale)

    alerts = sorted(name for no, name in ALERTS.items() if _int(alerts_msg, no))
    out = {
        'id': _text(info, 1), 'hardware': _text(info, 2), 'software': _text(info, 3),
        'country': _text(info, 4), 'generation': _int(info, 12),
        'uptime_s': _int(state, 1),
        'down_bps': num(st, 1007), 'up_bps': num(st, 1008),
        'latency_ms': _float(st, 1009),
        'drop_rate': num(st, 1003),
        'obstructed_pct': num(obstruction, 1, 100.0),
        'currently_obstructed': bool(_int(obstruction, 5)),
        'obstruction_valid_s': _float(obstruction, 4),
        'outage': None if outage is None else {
            'cause': OUTAGE_CAUSES.get(_int(outage, 1) or 0, 'unknown'),
            'seconds': (_int(outage, 3) or 0) / 1e9},
        'alerts': alerts,
        'gps_valid': None if not gps else bool(_int(gps, 1)),
        'gps_sats': _int(gps, 2),
        'azimuth': _float(st, 1011) if _float(st, 1011) is not None else _float(align, 4),
        'elevation': _float(st, 1012) if _float(st, 1012) is not None else _float(align, 5),
        'tilt': _float(align, 3),
        'eth_mbps': _int(st, 1016),
        'snr_ok': None if _first(st, 1018) is None else bool(_int(st, 1018)),
        'ready': dict((name, bool(_int(ready, i))) for i, name in
                      enumerate(('cady', 'scp', 'l1l2', 'xphy', 'aap', 'rf'), 1)) if ready else None,
        'update': UPDATE_STATES.get(_int(st, 1021) or 0, 'unknown') if _first(st, 1021) is not None else None,
        'stow_requested': bool(_int(st, 1010)),
        # RateLimitReason: 0 = none; the dish names POLICY_LIMIT, OVERAGE_LIMIT, LOW_SPEED_POLICY_LIMIT
        'throttled_down': _int(st, 1044) or 0,
        'throttled_up': _int(st, 1045) or 0,
    }
    if outage is not None:
        out['state'] = 'Outage: ' + out['outage']['cause']
    elif out['currently_obstructed']:
        out['state'] = 'Obstructed'
    elif out['latency_ms'] is None and not out['down_bps'] and not out['up_bps']:
        out['state'] = 'Searching'
    else:
        out['state'] = 'Connected'
    return out


def decode_reply(data):
    """Bytes of a gRPC(-web) reply -> status dict. Raises ValueError with a readable reason."""
    messages, trailers = split_frames(data)
    code = trailers.get('grpc-status')
    if code not in (None, '0'):
        raise ValueError('the dish answered grpc-status %s %s' % (code, trailers.get('grpc-message', '')))
    if not messages:
        raise ValueError('empty reply')
    return decode_status(messages[0])


# ----------------------------------------------------- dish history (1 Hz)

HISTORY_LEN = 900          # the dish keeps the last 900 seconds in a ring buffer


def _floats(msg, number):
    """A repeated float field: packed (one length-delimited blob) or one 32-bit value per entry."""
    out = []
    for v in msg.get(number, ()):
        if isinstance(v, bytes) and len(v) % 4 == 0:
            out.extend(struct.unpack('<%df' % (len(v) // 4), v))
    return out


def decode_history(message):
    """Reply message -> {'current', 'down', 'up', 'latency', 'drop', 'power', 'outages'} (arrays of 900 floats)."""
    resp = parse_message(message)
    inner = _first(resp, 2006)
    if not isinstance(inner, bytes):
        raise ValueError('the reply has no dish history')
    h = parse_message(inner)
    outages = []
    for raw in h.get(1009, ()):
        o = parse_message(raw) if isinstance(raw, bytes) else {}
        ts = _int(o, 2)
        if ts is not None and ts > 2 ** 62:           # int64 sent as an unsigned varint
            ts -= 2 ** 64
        outages.append({'cause': OUTAGE_CAUSES.get(_int(o, 1) or 0, 'unknown'), 'start_ns': ts,
                        'duration_s': (_int(o, 3) or 0) / 1e9, 'switched': bool(_int(o, 4))})
    return {'current': _int(h, 1) or 0, 'drop': _floats(h, 1001), 'latency': _floats(h, 1002),
            'down': _floats(h, 1003), 'up': _floats(h, 1004), 'power': _floats(h, 1010), 'outages': outages}


def decode_history_reply(data):
    messages, trailers = split_frames(data)
    code = trailers.get('grpc-status')
    if code not in (None, '0'):
        raise ValueError('the dish answered grpc-status %s %s' % (code, trailers.get('grpc-message', '')))
    if not messages:
        raise ValueError('empty reply')
    return decode_history(messages[0])


def new_samples(array, current, last):
    """
    The entries of a 900-long ring buffer written since the previous look, oldest first.
    `current` counts all samples ever written; `last` is the `current` of the previous look
    (None the first time: then only the newest entry, so a restart never double counts).
    """
    n = len(array)
    if not n:
        return []
    if last is None:
        count = 1
    else:
        count = min(max(current - last, 0), n)
    return [array[k % n] for k in range(current - count, current)]


# ------------------------------------------------------------------ history

class History(object):
    """Samples of the numbers worth a graph, in memory."""
    KEYS = ('down_mbps', 'up_mbps', 'latency_ms', 'drop_pct', 'obstructed_pct')

    def __init__(self, interval=5, keep_s=2 * 3600, clock=time.time):
        self.interval = interval
        self.clock = clock
        self.rows = collections.deque(maxlen=int(keep_s / interval) + 1)
        self.started = int(clock())

    def add(self, status, now=None):
        t = int(self.clock() if now is None else now)
        down, up = status.get('down_bps'), status.get('up_bps')
        row = {'down_mbps': None if down is None else round(down / 1e6, 2),
               'up_mbps': None if up is None else round(up / 1e6, 2),
               'latency_ms': None if status.get('latency_ms') is None else round(status['latency_ms'], 1),
               'drop_pct': None if status.get('drop_rate') is None else round(status['drop_rate'] * 100, 2),
               'obstructed_pct': None if status.get('obstructed_pct') is None else round(status['obstructed_pct'], 2)}
        self.rows.append((t, row))

    def history(self, window_s=3600, max_points=360):
        now = self.clock()
        rows = [(t, r) for t, r in self.rows if t >= now - window_s]
        if not rows:
            return {'t': [], 'series': {}}
        stride = max(1, int(math.ceil(len(rows) / float(max_points))))
        buckets = [rows[i:i + stride] for i in range(0, len(rows), stride)]
        out = {'t': [int(sum(t for t, _ in b) / len(b)) for b in buckets], 'series': {}}
        for key in self.KEYS:
            col = []
            for b in buckets:
                vals = [r[key] for _, r in b if r[key] is not None]
                col.append(round(sum(vals) / len(vals), 3) if vals else None)
            if any(v is not None for v in col):
                out['series'][key] = col
        return out
