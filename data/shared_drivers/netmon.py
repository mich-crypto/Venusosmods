"""
Numbers and bookkeeping for the Starlink views: ping/trace parsing, a small JSON
store (energy, data usage, outages, speed tests, WAN IP) and the summaries the page shows.

Pure Python without network or D-Bus: the page's server runs the commands and feeds the
results in here. The store is a plain JSON file written atomically, at most every few
minutes, so it costs next to no flash wear and survives reboots.
"""

import os
import re
import time

try:
    import json
except ImportError:         # not verified on every Venus OS image: the books then live in memory only
    json = None

SAMPLE_S = 1.0                  # the dish's history has one value per second
KEEP_DAYS = 400
MAX_TESTS = 500
MAX_OUTAGES = 1000
MAX_WAN = 200
MAX_UNREACHABLE = 500


# ------------------------------------------------------------------ parsing

_LOSS_RE = re.compile(r'(\d+(?:\.\d+)?)% packet loss')
_RTT_RE = re.compile(r'(?:round-trip|rtt)[^=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)')


def parse_ping(text):
    """BusyBox or iputils ping output -> {'loss': %, 'min': ms, 'avg': ms, 'max': ms} (times None if all lost)."""
    loss = _LOSS_RE.search(text or '')
    if not loss:
        return None
    out = {'loss': float(loss.group(1)), 'min': None, 'avg': None, 'max': None}
    rtt = _RTT_RE.search(text)
    if rtt:
        out['min'], out['avg'], out['max'] = (float(x) for x in rtt.groups())
    return out


def parse_trace(text):
    """Cloudflare's /cdn-cgi/trace (key=value lines) -> dict."""
    out = {}
    for line in (text or '').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def rtt_state(result, good_ms=40, warn_ms=100):
    """Colour of an ICMP tile: loss first, then latency."""
    if result is None or result.get('avg') is None or result.get('loss', 0) >= 100:
        return 'down'
    if result['loss'] >= 20:
        return 'bad'
    if result['loss'] > 0 and result['loss'] >= 5:
        return 'warn'
    if result['avg'] <= good_ms:
        return 'good'
    return 'warn' if result['avg'] <= warn_ms else 'bad'


def jitter(values):
    """Mean difference between consecutive latencies (ms)."""
    if len(values) < 2:
        return None
    return sum(abs(b - a) for a, b in zip(values, values[1:])) / (len(values) - 1)


# -------------------------------------------------------------------- store

def day_key(ts):
    return time.strftime('%Y-%m-%d', time.localtime(ts))


class Store(object):
    def __init__(self, path, clock=time.time, save_every=300):
        self.path = path
        self.clock = clock
        self.save_every = save_every
        self.dirty = False
        self.saved_at = 0
        self.data = {'version': 1, 'days': {}, 'tests': [], 'outages': [], 'unreachable': [], 'wan': [],
                     'dish': {'current': None}, 'first_seen': None}
        try:
            if json is None:
                raise OSError('no json module')
            with open(path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for k in self.data:
                    if k in loaded:
                        self.data[k] = loaded[k]
        except (OSError, ValueError):
            pass
        if self.data['first_seen'] is None:
            self.data['first_seen'] = int(clock())

    # -- persistence
    def save(self, force=False):
        if not self.dirty:
            return False
        now = self.clock()
        if json is None or (not force and now - self.saved_at < self.save_every):
            return False
        try:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.data, f, separators=(',', ':'))
            os.replace(tmp, self.path)
        except OSError:
            return False
        self.dirty = False
        self.saved_at = now
        return True

    # -- recording
    def day(self, ts):
        key = day_key(ts)
        d = self.data['days'].setdefault(key, {'wh': 0.0, 'seconds': 0, 'down': 0.0, 'up': 0.0, 'test': 0.0,
                                               'peak_w': 0.0, 'min_w': None})
        return d

    def add_dish_samples(self, ts, power, down, up):
        """Per-second samples since the previous look (watts, bits/s, bits/s)."""
        n = max(len(power), len(down), len(up))
        if not n:
            return
        d = self.day(ts)
        watts = [w for w in power if w is not None and w >= 0]
        d['wh'] += sum(watts) * SAMPLE_S / 3600.0
        d['seconds'] += len(watts)
        if watts:
            d['peak_w'] = max(d['peak_w'], max(watts))
            d['min_w'] = min(watts) if d['min_w'] is None else min(d['min_w'], min(watts))
        d['down'] += sum(x for x in down if x and x > 0) * SAMPLE_S / 8.0        # bytes
        d['up'] += sum(x for x in up if x and x > 0) * SAMPLE_S / 8.0
        self._trim()
        self.dirty = True

    def add_test_bytes(self, ts, n):
        self.day(ts)['test'] += n
        self.dirty = True

    def add_test(self, result):
        self.data['tests'].append(result)
        del self.data['tests'][:-MAX_TESTS]
        self.dirty = True

    def add_outages(self, outages):
        """From the dish history; the same outage is seen on every look, so match on its start."""
        have = set(o['start_ns'] for o in self.data['outages'] if o.get('start_ns'))
        added = 0
        for o in outages:
            if o.get('start_ns') and o['start_ns'] not in have:
                self.data['outages'].append(dict(o, start=o['start_ns'] / 1e9))
                have.add(o['start_ns'])
                added += 1
        if added:
            self.data['outages'].sort(key=lambda o: o['start'])
            del self.data['outages'][:-MAX_OUTAGES]
            self.dirty = True
        return added

    def add_unreachable(self, start, end):
        self.data['unreachable'].append({'start': start, 'end': end})
        del self.data['unreachable'][:-MAX_UNREACHABLE]
        self.dirty = True

    def add_wan(self, ip, extra=None):
        """Remember the public address when it changes. Returns the previous one on a change."""
        wan = self.data['wan']
        if wan and wan[-1]['ip'] == ip:
            return None
        previous = wan[-1]['ip'] if wan else None
        wan.append(dict(extra or {}, ip=ip, time=int(self.clock())))
        del wan[:-MAX_WAN]
        self.dirty = True
        return previous or ''

    def _trim(self):
        days = self.data['days']
        if len(days) > KEEP_DAYS:
            for k in sorted(days)[:-KEEP_DAYS]:
                del days[k]


# ---------------------------------------------------------------- summaries

def energy_summary(store, price=0.0, now=None):
    now = store.clock() if now is None else now
    days = store.data['days']
    today, month, year = day_key(now), time.strftime('%Y-%m', time.localtime(now)), time.strftime('%Y', time.localtime(now))
    t = days.get(today, {})
    wh_today = t.get('wh', 0.0)
    wh_month = sum(d['wh'] for k, d in days.items() if k.startswith(month))
    wh_year = sum(d['wh'] for k, d in days.items() if k.startswith(year))
    secs = sum(d['seconds'] for d in days.values())
    wh_all = sum(d['wh'] for d in days.values())
    avg_w = wh_all * 3600.0 / secs if secs else None
    lt = time.localtime(now)
    # projection: this month's average day so far, over the days of the month
    month_days = 31 if lt.tm_mon in (1, 3, 5, 7, 8, 10, 12) else (30 if lt.tm_mon != 2 else
                                                                     (29 if lt.tm_year % 4 == 0 and (lt.tm_year % 100 or lt.tm_year % 400 == 0) else 28))
    seen = [d for k, d in days.items() if k.startswith(month) and d['seconds']]
    projected = (sum(d['wh'] for d in seen) / len(seen)) * month_days if seen else None
    daily = [{'day': k, 'wh': round(d['wh'], 2)} for k, d in sorted(days.items())[-60:]]
    out = {'now_w': None, 'avg_w': avg_w, 'today_wh': wh_today, 'month_wh': wh_month, 'year_wh': wh_year,
           'projected_month_wh': projected, 'peak_w': max([d['peak_w'] for d in days.values()] or [0]) or None,
           'idle_w': min([d['min_w'] for d in days.values() if d.get('min_w') is not None] or [0]) or None,
           'daily': daily, 'tracked_days': len([d for d in days.values() if d['seconds']])}
    if price:
        out['cost'] = {'today': wh_today / 1000.0 * price, 'month': wh_month / 1000.0 * price, 'year': wh_year / 1000.0 * price,
                       'projected_month': None if projected is None else projected / 1000.0 * price}
    return out


def cycle_start(now, cycle_day):
    """Start (epoch) of the billing cycle that contains `now`; cycle_day 1-28."""
    lt = time.localtime(now)
    year, month = lt.tm_year, lt.tm_mon
    if lt.tm_mday < cycle_day:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return time.mktime((year, month, cycle_day, 0, 0, 0, 0, 0, -1))


def usage_summary(store, cycle_day=1, cap_gb=0.0, now=None):
    now = store.clock() if now is None else now
    cycle_day = max(1, min(28, int(cycle_day or 1)))
    start = cycle_start(now, cycle_day)
    days = store.data['days']
    first = day_key(start)
    rows = [(k, d) for k, d in sorted(days.items()) if k >= first]
    down = sum(d['down'] for _, d in rows)
    up = sum(d['up'] for _, d in rows)
    test = sum(d['test'] for _, d in rows)
    elapsed_days = max(1.0, (now - start) / 86400.0)
    nxt = cycle_start(start + 32 * 86400, cycle_day)
    cycle_days = max(1.0, (nxt - start) / 86400.0)
    projected = (down + up) / elapsed_days * cycle_days
    out = {'cycle_start': int(start), 'cycle_end': int(nxt), 'down': down, 'up': up, 'total': down + up, 'test_bytes': test,
           'projected': projected, 'cap_bytes': cap_gb * 1e9 if cap_gb else None,
           'daily': [{'day': k, 'down': round(d['down']), 'up': round(d['up']), 'test': round(d['test'])} for k, d in rows[-62:]]}
    if cap_gb:
        out['cap_used'] = (down + up) / (cap_gb * 1e9)
    return out


def availability(store, window_s, now=None):
    """Outage statistics over the window. Time the Pi could not reach the dish is not counted as an outage."""
    now = store.clock() if now is None else now
    since = max(now - window_s, store.data['first_seen'] or 0)
    observed = max(1.0, now - since)
    gone = 0.0
    for u in store.data['unreachable']:
        gone += max(0.0, min(u['end'], now) - max(u['start'], since))
    rows = [o for o in store.data['outages'] if o['start'] + o['duration_s'] >= since]
    down = 0.0
    causes = {}
    for o in rows:
        d = max(0.0, min(o['start'] + o['duration_s'], now) - max(o['start'], since))
        down += d
        causes[o['cause']] = causes.get(o['cause'], 0.0) + d
    seen = max(1.0, observed - gone)
    return {'observed_s': observed, 'outage_s': down, 'unreachable_s': gone, 'count': len(rows),
            'availability': max(0.0, 1.0 - down / seen), 'causes': causes, 'since': int(since)}
