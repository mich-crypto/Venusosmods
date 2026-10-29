"""
Battery debug data for the settings page: turns what dbus-serialbattery
publishes (com.victronenergy.battery.*) into a short history for graphs.

Pure Python, no D-Bus: the page's server feeds it the values it reads. A sample
is taken every `interval` seconds and kept for `keep_s` seconds in memory, so
the history starts again when the page's service restarts.
"""

import collections
import math
import time

# sample key -> D-Bus path
SCALARS = {
    'v': '/Dc/0/Voltage', 'i': '/Dc/0/Current', 'p': '/Dc/0/Power', 'soc': '/Soc',
    'temp': '/Dc/0/Temperature', 'mos': '/System/MOSTemperature',
    't1': '/System/Temperature1', 't2': '/System/Temperature2',
    't3': '/System/Temperature3', 't4': '/System/Temperature4',
    'ccl': '/Info/MaxChargeCurrent', 'dcl': '/Info/MaxDischargeCurrent',
    'cvl': '/Info/MaxChargeVoltage', 'cmin': '/System/MinCellVoltage',
    'cmax': '/System/MaxCellVoltage',
}
# shown as a table in "all values" and used for the findings
DETAIL_PREFIXES = ('/Dc/0/', '/Info/', '/System/', '/Voltages/', '/Balances/', '/Alarms/', '/Io/',
                   '/History/', '/Cell/')
DETAIL_KEYS = ('/Soc', '/Soh', '/Capacity', '/InstalledCapacity', '/ConsumedAmphours', '/TimeToGo',
               '/ProductName', '/CustomName', '/Connected', '/Mgmt/Connection', '/FirmwareVersion',
               '/DeviceInstance')
MAX_CELLS = 64


def cell_paths(n):
    """The two formats BATTERY_CELL_DATA_FORMAT can publish."""
    return (('/Voltages/Cell%d' % n, '/Balances/Cell%d' % n),
            ('/Cell/%d/Volts' % n, '/Cell/%d/Balance' % n))


def _num(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return v
    return None


def extract(values):
    """One sample from a dump of a battery service, or None if it has no battery data."""
    if _num(values.get('/Dc/0/Voltage')) is None and _num(values.get('/Soc')) is None:
        return None
    s = {}
    for key, path in SCALARS.items():
        v = _num(values.get(path))
        if v is not None:
            s[key] = round(v, 3)
    cells, balance = [], []
    for n in range(1, MAX_CELLS + 1):
        volts = bal = None
        for vp, bp in cell_paths(n):
            volts = _num(values.get(vp))
            if volts is not None:
                bal = _num(values.get(bp))
                break
        if volts is None:
            if n > 1 and n > (_num(values.get('/System/NrOfCellsPerBattery')) or 0):
                break
            cells.append(None)
            balance.append(None)
            continue
        cells.append(round(volts, 3))
        balance.append(None if bal is None else int(bool(bal)))
    while cells and cells[-1] is None:
        cells.pop()
        balance.pop()
    if cells:
        s['cells'] = cells
        s['bal'] = balance
        known = [c for c in cells if c is not None]
        if known:
            s['diff'] = round((max(known) - min(known)) * 1000, 1)       # mV between best and worst cell
    return s


def details(values):
    """The values worth showing in a table: everything the page understands, nothing else."""
    out = {}
    for k, v in values.items():
        if k in DETAIL_KEYS or k.startswith(DETAIL_PREFIXES):
            out[k] = v
    return out


class Recorder(object):
    def __init__(self, interval=10, keep_s=6 * 3600, clock=time.time):
        self.interval = interval
        self.keep = int(keep_s / interval) + 1
        self.clock = clock
        self.started = int(clock())
        self.data = {}            # service -> deque of (t, sample)
        self.latest = {}          # service -> (t, details dict)

    def add(self, service, values, now=None):
        sample = extract(values)
        if sample is None:
            return False
        t = int(self.clock() if now is None else now)
        q = self.data.setdefault(service, collections.deque(maxlen=self.keep))
        q.append((t, sample))
        self.latest[service] = (t, details(values))
        return True

    def forget_missing(self, present):
        """Drop services that are gone from the bus (a battery that was removed)."""
        for name in [n for n in self.data if n not in present]:
            t = self.latest.get(name, (0,))[0]
            if self.clock() - t > 600:
                del self.data[name]
                self.latest.pop(name, None)

    def services(self):
        return sorted(self.data)

    def history(self, service, window_s=3600, max_points=360):
        q = self.data.get(service)
        if not q:
            return {'t': [], 'series': {}, 'cells': [], 'bal': []}
        now = self.clock()
        rows = [(t, s) for t, s in q if t >= now - window_s]
        if not rows:
            return {'t': [], 'series': {}, 'cells': [], 'bal': []}
        stride = max(1, int(math.ceil(len(rows) / float(max_points))))
        buckets = [rows[i:i + stride] for i in range(0, len(rows), stride)]
        ncells = max([len(s.get('cells', ())) for _, s in rows] or [0])

        def mean(items):
            items = [x for x in items if x is not None]
            return round(sum(items) / len(items), 3) if items else None

        out = {'t': [int(sum(t for t, _ in b) / len(b)) for b in buckets], 'series': {},
               'cells': [[] for _ in range(ncells)], 'bal': [[] for _ in range(ncells)]}
        keys = list(SCALARS) + ['diff']
        for key in keys:
            col = [mean([s.get(key) for _, s in b]) for b in buckets]
            if any(v is not None for v in col):
                out['series'][key] = col
        for c in range(ncells):
            for b in buckets:
                out['cells'][c].append(mean([s['cells'][c] if c < len(s.get('cells', ())) else None
                                             for _, s in b]))
                flags = [s['bal'][c] for _, s in b if c < len(s.get('bal', ())) and s['bal'][c] is not None]
                out['bal'][c].append(max(flags) if flags else None)
        return out

    def info(self, service):
        t, d = self.latest.get(service, (None, {}))
        return {'time': t, 'age': None if t is None else int(self.clock() - t), 'values': d,
                'samples': len(self.data.get(service, ())), 'since': self.started}
