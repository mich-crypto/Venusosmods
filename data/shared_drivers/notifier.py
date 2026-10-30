"""
The camper's Telegram notifications: low battery, any alarm, geofence and the Orion XS.

Pure Python without D-Bus or network. The page's server reads the Victron devices into a
snapshot every few seconds and hands it to `Notifier.check`; the messages go out through the
`send(key, text, cooldown)` function it was given (the Starlink hub's Telegram queue).

snapshot = {
    'batteries': [{'name': 'JK BMS', 'soc': 63.0}],
    'alarms':    [{'device': 'JK BMS', 'path': '/Alarms/LowVoltage', 'value': 1}],
    'gps':       {'lat': 52.1, 'lon': 5.2} or None,
    'dcdc':      [{'name': 'Orion XS', 'state': 3, 'mode': 1, 'volts': 13.8, 'amps': 20.0}],
    'outdoor':   12.5 or None,                       # a temperature sensor set to "outdoor"
    'forecast':  {'now': 1.0, 'min': -3.0, 'at': '2026-10-31T04:00'} or None,   # at the camper's position
}
"""

import math
import re
import time

try:
    import json
except ImportError:         # not verified on every Venus OS image: then there is no forecast
    json = None

REPEAT_ALARM_S = 3 * 3600           # a battery that stays at "alarm" is repeated this often
REPEAT_WARN_S = 12 * 3600
MOVED_UPDATE_S = 600                # while outside the fence: a new position this often
COLD_HOURS = 2                      # below freezing this long: a warning
FREEZE_C = 0.0
CONFIRM = 2                         # a reading must hold for this many looks (a few seconds each)

# what the Orion XS reports in /State (the VE.Direct charger states)
ORION_STATES = {0: 'off', 1: 'low power', 2: 'fault', 3: 'bulk', 4: 'absorption', 5: 'float', 6: 'storage',
                7: 'equalize', 11: 'power supply', 245: 'starting up', 252: 'external control'}
ORION_ACTIVE = (3, 4, 5, 6, 7, 11, 252)
ORION_OFF = (0, 1, None)


def _bool(v, default):
    if v is None or v == '':
        return default
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _num(v, default, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def parse_forecast(text):
    """Open-Meteo answer (current + the next 24 hours) -> {'now': C, 'min': C, 'at': local time of the minimum}."""
    if json is None:
        return None
    try:
        d = json.loads(text)
        times, temps = d['hourly']['time'], d['hourly']['temperature_2m']
        rows = [(t, x) for t, x in zip(times, temps) if isinstance(x, (int, float))]
        now = (d.get('current') or {}).get('temperature_2m')
        if not rows:
            return None
        low = min(rows, key=lambda r: r[1])
        return {'now': now if isinstance(now, (int, float)) else rows[0][1], 'min': low[1], 'at': low[0]}
    except (ValueError, KeyError, TypeError):
        return None


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def pretty(path):
    """'/Alarms/LowVoltage' -> 'Low voltage'."""
    last = str(path).rstrip('/').rsplit('/', 1)[-1] or str(path)
    words = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', last).replace('_', ' ').strip()
    return words[:1].upper() + words[1:].lower() if words else str(path)


def map_link(lat, lon):
    return 'https://maps.google.com/?q=%.6f,%.6f' % (lat, lon)


class Notifier(object):
    def __init__(self, cfg, send, clock=time.time, record=None):
        self.send = send
        self.clock = clock
        self.record = record                     # called with the text of every message that was handed on
        self.configure(cfg)
        self.battery = {}                        # name -> [level, time of the last message]
        self.alarms = {}                         # (device, path) -> {'sent': severity, 'seen': n}
        self.fence = {'state': 'unknown', 'out_seen': 0, 'in_seen': 0, 'last': 0}
        self.orion = {}                          # name -> (state, mode)
        self.cold = {'since': None, 'sent': False}
        self.freeze_sent = False
        self.last = {'battery': None, 'gps': None, 'distance': None, 'orion': None, 'temp': None, 'temp_from': None, 'forecast': None}

    def configure(self, cfg):
        cfg = dict(cfg or {})
        self.on = {'battery': _bool(cfg.get('alert_battery'), True), 'alarms': _bool(cfg.get('alert_alarms'), True),
                   'geofence': _bool(cfg.get('alert_geofence'), False), 'orion': _bool(cfg.get('alert_orion'), True),
                   'freeze': _bool(cfg.get('alert_freeze'), True), 'cold': _bool(cfg.get('alert_cold'), True)}
        self.warn_pct = _num(cfg.get('battery_warn_pct'), 20, 1, 99)
        self.alarm_pct = min(_num(cfg.get('battery_alarm_pct'), 10, 1, 99), self.warn_pct)
        self.fence_m = _num(cfg.get('fence_m'), 200, 20, 100000)
        lat, lon = _num(cfg.get('fence_lat'), None), _num(cfg.get('fence_lon'), None)
        self.home = (lat, lon) if lat is not None and lon is not None and (lat or lon) else None

    def set_home(self, lat, lon):
        self.home = (lat, lon)
        self.fence = {'state': 'in', 'out_seen': 0, 'in_seen': 0, 'last': 0}

    def _say(self, key, text, cooldown=0):
        sent = self.send(key, text, cooldown)
        if self.record and sent is not False:
            self.record(text)
        return sent

    # ----------------------------------------------------------------- rules
    def check(self, snap):
        snap = snap or {}
        if self.on['battery']:
            self._batteries(snap.get('batteries') or [])
        if self.on['alarms']:
            self._alarms(snap.get('alarms') or [])
        gps = snap.get('gps')
        self.last['gps'] = gps
        if self.on['geofence']:
            self._geofence(gps)
        if self.on['orion']:
            self._orion(snap.get('dcdc') or [])
        else:
            self.orion = {}
        forecast = snap.get('forecast')
        self.last['forecast'] = forecast
        if self.on['freeze']:
            self._freeze(forecast)
        if self.on['cold']:
            self._cold(snap.get('outdoor'), forecast)

    def _batteries(self, batteries):
        now = self.clock()
        for b in batteries:
            soc, name = b.get('soc'), b.get('name') or 'Battery'
            if soc is None:
                continue
            self.last['battery'] = soc
            level = 'alarm' if soc <= self.alarm_pct else ('warn' if soc <= self.warn_pct else 'ok')
            old, sent_at = self.battery.get(name, ['ok', 0])
            if level == 'ok' and old != 'ok' and soc < self.warn_pct + 5:
                continue                                         # not clear of the limit yet: no flapping
            if level == 'ok':
                self.battery[name] = ['ok', 0]
                continue
            worse = old == 'ok' or (level == 'alarm' and old == 'warn')
            repeat = REPEAT_ALARM_S if level == 'alarm' else REPEAT_WARN_S
            if worse or now - sent_at >= repeat:
                if level == 'alarm':
                    text = '🔴 %s is almost empty: %d %%. Charge it now.' % (name, round(soc))
                else:
                    text = '🟠 %s is getting low: %d %%.' % (name, round(soc))
                self._say('battery-' + name, text)
                self.battery[name] = [level, now]
            else:
                self.battery[name] = [level, sent_at]

    def _alarms(self, alarms):
        active = {}
        for a in alarms:
            try:
                value = int(a.get('value') or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                active[(a.get('device') or 'Device', a.get('path') or '')] = value
        for key, value in active.items():
            st = self.alarms.setdefault(key, {'sent': 0, 'seen': 0})
            st['seen'] += 1
            if st['seen'] >= CONFIRM and value > st['sent']:
                st['sent'] = value
                text = '%s %s: %s' % ('🔴' if value >= 2 else '🟠', key[0], pretty(key[1]) + (' (alarm)' if value >= 2 else ' (warning)'))
                self._say('alarm-%s-%s' % key, text)
        for key in list(self.alarms):
            if key not in active:
                st = self.alarms.pop(key)
                if st['sent']:
                    self._say('alarm-%s-%s-clear' % key, '✅ %s: %s is back to normal' % (key[0], pretty(key[1])))

    def _geofence(self, gps):
        f = self.fence
        if not self.home or not gps or gps.get('lat') is None or gps.get('lon') is None:
            self.last['distance'] = None
            return
        d = distance_m(self.home[0], self.home[1], gps['lat'], gps['lon'])
        self.last['distance'] = d
        now = self.clock()
        if d > self.fence_m:
            f['out_seen'] += 1
            f['in_seen'] = 0
            if f['state'] != 'out' and f['out_seen'] >= CONFIRM:
                f['state'], f['last'] = 'out', now
                self._say('fence-out', '🚨 The camper left its %d m area: now %d m from home.\n%s' % (
                    self.fence_m, round(d), map_link(gps['lat'], gps['lon'])))
            elif f['state'] == 'out' and now - f['last'] >= MOVED_UPDATE_S:
                f['last'] = now
                self._say('fence-update', '📍 Still away: %d m from home.\n%s' % (round(d), map_link(gps['lat'], gps['lon'])))
        else:
            f['in_seen'] += 1
            f['out_seen'] = 0
            if f['state'] == 'out' and f['in_seen'] >= CONFIRM:
                f['state'] = 'in'
                self._say('fence-in', '✅ The camper is back inside its %d m area.' % self.fence_m)
            elif f['state'] == 'unknown':
                f['state'] = 'in'

    def _orion(self, units):
        seen = set()
        for u in units:
            name = u.get('name') or 'Orion'
            seen.add(name)
            state, mode = u.get('state'), u.get('mode')
            self.last['orion'] = ORION_STATES.get(state, state)
            old = self.orion.get(name)
            self.orion[name] = (state, mode)
            if old is None:
                continue                                         # the first look is not a change
            was_off = old[1] == 4 or old[0] in ORION_OFF
            now_on = mode != 4 and state in ORION_ACTIVE
            if was_off and now_on:
                extra = ''
                if u.get('volts') is not None:
                    extra = ' (%.1f V%s)' % (u['volts'], '' if u.get('amps') is None else ', %.1f A' % u['amps'])
                self._say('orion-' + name, '⚡ %s: charging was switched on, %s%s.' % (name, ORION_STATES.get(state, 'state %s' % state), extra), cooldown=60)
        for name in list(self.orion):
            if name not in seen:
                del self.orion[name]

    def _freeze(self, forecast):
        if not forecast or forecast.get('min') is None:
            return
        low = forecast['min']
        if low <= FREEZE_C and not self.freeze_sent:
            self.freeze_sent = True
            when = str(forecast.get('at') or '')
            when = ' around %s' % when[11:16] if len(when) >= 16 else ''
            self._say('freeze-forecast', '❄️ Frost is coming: down to %.0f °C%s at the camper within 24 hours. Protect the water.' % (low, when))
        elif low > FREEZE_C + 1:
            self.freeze_sent = False                              # it will be announced again next time

    def _cold(self, outdoor, forecast):
        temp, source = outdoor, 'the outdoor sensor'
        if temp is None and forecast and forecast.get('now') is not None:
            temp, source = forecast['now'], 'the forecast service'
        self.last['temp'], self.last['temp_from'] = temp, (source if temp is not None else None)
        if temp is None:
            return
        c, now = self.cold, self.clock()
        if temp < FREEZE_C:
            if c['since'] is None:
                c['since'] = now
            elif not c['sent'] and now - c['since'] >= COLD_HOURS * 3600:
                c['sent'] = True
                self._say('cold-2h', '🥶 It has been below 0 °C at the camper for %d hours (now %.1f °C, from %s).' % (COLD_HOURS, temp, source))
        elif temp >= FREEZE_C + 1:
            c['since'], c['sent'] = None, False

    # ------------------------------------------------------------- for the page
    def view(self):
        d = self.last['distance']
        return {'battery_soc': self.last['battery'], 'orion_state': self.last['orion'],
                'gps': self.last['gps'], 'home': None if not self.home else {'lat': self.home[0], 'lon': self.home[1]},
                'distance_m': None if d is None else round(d), 'fence_state': self.fence['state'], 'fence_m': self.fence_m,
                'temp': self.last['temp'], 'temp_from': self.last['temp_from'], 'forecast': self.last['forecast'],
                'warn_pct': self.warn_pct, 'alarm_pct': self.alarm_pct, 'on': dict(self.on),
                'active_alarms': sorted('%s: %s' % (k[0], pretty(k[1])) for k, v in self.alarms.items() if v['sent'])}
