#!/usr/bin/env python3
"""
Settings web page for the Venus OS mods.

  http://<pi>:8088/      (port and password: [web] section of config.ini)

* edit our drivers' config.ini (fridge, gas, water tank) in forms
* edit dbus-serialbattery's config.ini with all its documented settings
* see the live values, driver status and logs, restart drivers, reboot

Without a password the page is read-only. Everything that changes something
needs the password (HTTP basic auth) and a custom request header (so another
web page cannot trigger changes through your browser).

Uses only the Python modules that exist on Venus OS, a minimal HTTP server on
the GLib main loop and the system D-Bus. No pip packages.
"""

import argparse
import configparser
import glob
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402

import battery_debug  # noqa: E402
import config_tools as ct  # noqa: E402
import rawlog  # noqa: E402
import netmon  # noqa: E402
import starlink  # noqa: E402
import starlink_hub  # noqa: E402

log = logging.getLogger('web_ui')

VERSION = '1.0.0'
OUR_PROCESSES = ('mopeka_venus.py', 'esp32_hydro_tank.py', 'fridge_venus.py')
BUS_ITEM = 'com.victronenergy.BusItem'
LAN_RANGES = ('192.168.0.0/16', '10.0.0.0/8', '172.16.0.0/12', '100.64.0.0/10')   # incl. Tailscale
MAX_BODY = 1024 * 1024
CSRF_HEADER = 'x-requested-with'
CSRF_VALUE = 'venussettings'
TAI64_OFFSET = 4611686018427387914          # 2**62 + 10, what tai64nlocal subtracts


# ------------------------------------------------------------ small helpers
# (written out here because json/base64/urllib may be missing on Venus OS)

def json_str(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif o < 0x20 or o in (0x2028, 0x2029):
            out.append('\\u%04x' % o)
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def to_json(obj):
    if obj is None:
        return 'null'
    if obj is True:
        return 'true'
    if obj is False:
        return 'false'
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        if obj != obj or obj in (float('inf'), float('-inf')):
            return 'null'
        return repr(obj)
    if isinstance(obj, str):
        return json_str(obj)
    if isinstance(obj, dict):
        return '{' + ','.join('%s:%s' % (json_str(str(k)), to_json(v)) for k, v in obj.items()) + '}'
    if isinstance(obj, (list, tuple)):
        return '[' + ','.join(to_json(v) for v in obj) + ']'
    return json_str(str(obj))


_B64 = dict((c, i) for i, c in enumerate(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'))


def b64decode(text):
    text = text.strip().rstrip('=')
    bits, nbits, out = 0, 0, bytearray()
    for ch in text:
        if ch not in _B64:
            raise ValueError('bad base64')
        bits = bits << 6 | _B64[ch]
        nbits += 6
        if nbits >= 8:
            nbits -= 8
            out.append(bits >> nbits & 0xFF)
    return bytes(out)


def urldecode(text):
    text = text.replace('+', ' ')
    out = bytearray()
    i = 0
    raw = text.encode('utf-8')
    while i < len(raw):
        if raw[i:i + 1] == b'%' and re.match(rb'^[0-9A-Fa-f]{2}$', raw[i + 1:i + 3]):
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode('utf-8', 'replace')


def parse_form(data):
    """application/x-www-form-urlencoded -> {key: value} (last one wins)."""
    if isinstance(data, bytes):
        data = data.decode('utf-8', 'replace')
    result = {}
    for part in data.split('&'):
        if not part:
            continue
        key, _, value = part.partition('=')
        result[urldecode(key)] = urldecode(value)
    return result


def tai64n_to_epoch(stamp):
    """'@4000000067...' (multilog) -> unix time."""
    return int(stamp[1:17], 16) - TAI64_OFFSET


def decode_log(raw, limit=80):
    """multilog 'current' text -> ['2026-10-29 12:00:01 message', ...] (last `limit`)."""
    lines = []
    for line in raw.splitlines()[-limit:]:
        if line.startswith('@') and len(line) > 25:
            try:
                t = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tai64n_to_epoch(line[:25])))
                lines.append('%s %s' % (t, line[26:]))
                continue
            except ValueError:
                pass
        lines.append(line)
    return lines


SVSTAT_RE = re.compile(r'^(\S+): (up|down)(?: \(pid (\d+)\))? (\d+) seconds')


def parse_svstat(text):
    """{'/service/x': {'state': 'up', 'pid': 12, 'seconds': 5}} from svstat output."""
    result = {}
    for line in text.splitlines():
        m = SVSTAT_RE.match(line.strip())
        if m:
            result[m.group(1)] = {'state': m.group(2), 'pid': int(m.group(3)) if m.group(3) else None,
                                  'seconds': int(m.group(4))}
    return result


def unwrap(v):
    """dbus types -> plain python; Victron's invalid value (empty array) -> None."""
    if isinstance(v, dbus.Boolean):
        return bool(v)
    if isinstance(v, (dbus.Array, list, tuple)):
        return None if len(v) == 0 else [unwrap(x) for x in v]
    if isinstance(v, dbus.Dictionary):
        return dict((str(k), unwrap(x)) for k, x in v.items())
    if isinstance(v, (dbus.Byte, dbus.Int16, dbus.UInt16, dbus.Int32, dbus.UInt32,
                      dbus.Int64, dbus.UInt64)):
        return int(v)
    if isinstance(v, dbus.Double):
        return float(v)
    if isinstance(v, (dbus.String, dbus.ObjectPath, dbus.Signature)):
        return str(v)
    return v


# ---------------------------------------------------------------- the app

class Response(object):
    def __init__(self, status=200, body=b'', ctype='application/json', headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode('utf-8')
        self.ctype = ctype
        self.headers = headers or {}


def jsonr(obj, status=200, headers=None):
    return Response(status, to_json(obj), 'application/json; charset=utf-8', headers)


class Request(object):
    def __init__(self, method, path, query, headers, body, peer):
        self.method, self.path, self.query, self.headers, self.body, self.peer = \
            method, path, query, headers, body, peer
        self.form = parse_form(body) if method == 'POST' else {}


class App(object):
    def __init__(self, opts):
        self.driver_config = opts['driver_config']
        self.init_dir = opts['init_dir']
        self.service_dir = opts['service_dir']
        self.log_dir = opts['log_dir']
        self.rc_local = opts['rc_local']
        self.serialbattery_dir = opts.get('serialbattery_dir')
        self.password = opts.get('password', '')
        self.html = opts.get('html', os.path.join(HERE, 'web', 'index.html'))
        self.failures = {}                  # ip -> [count, first time]
        self._bus = None
        self.recorder = battery_debug.Recorder(interval=opts.get('sample_s', 10))
        self.state_dir = opts.get('state_dir') or os.path.join(HERE, 'state')
        self.starlink = starlink_hub.Hub(opts.get('starlink') or {}, opts.get('telegram') or {},
                                         netmon.Store(os.path.join(self.state_dir, 'starlink.json')))
        self.rawlog_dir = opts.get('rawlog_dir') or rawlog.DEFAULT_DIR
        self.starlink_dev = None          # the dish as a Venus OS device; False = could not be created

    # ---------------------------------------------------------- plumbing

    @property
    def bus(self):
        if self._bus is None:
            self._bus = dbus.SystemBus()
        return self._bus

    def authed(self, req):
        if not self.password:
            return False
        header = req.headers.get('authorization', '')
        if not header.lower().startswith('basic '):
            return False
        try:
            user_pass = b64decode(header[6:]).decode('utf-8', 'replace')
        except ValueError:
            return False
        given = user_pass.partition(':')[2]
        ok = len(given) == len(self.password)
        for a, b in zip(given.ljust(len(self.password)), self.password):
            ok = ok and a == b            # no early exit on the first wrong character
        return ok

    def throttled(self, ip):
        count, first = self.failures.get(ip, (0, 0))
        if time.time() - first > 60:
            return False
        return count >= 8

    def note_failure(self, ip):
        count, first = self.failures.get(ip, (0, time.time()))
        if time.time() - first > 60:
            count, first = 0, time.time()
        self.failures[ip] = (count + 1, first)

    def handle(self, req):
        try:
            return self._route(req)
        except Exception as e:                       # never kill the main loop
            log.exception('request failed')
            return jsonr({'ok': False, 'error': 'internal error: %s' % e}, 500)

    def _route(self, req):
        ip = req.peer[0] if req.peer else ''
        if req.method == 'GET' and req.path == '/':
            return self.page()
        if req.method == 'GET' and req.path == '/unlock':
            if self.throttled(ip):
                return Response(429, 'Too many wrong passwords, wait a minute.', 'text/plain')
            if self.authed(req):
                return Response(302, b'', 'text/plain', {'Location': '/'})
            if self.password and req.headers.get('authorization'):
                self.note_failure(ip)
            return Response(401, 'Password needed' if self.password else
                            'No password set in config.ini ([web] password): read-only',
                            'text/plain', {'WWW-Authenticate': 'Basic realm="Venus settings"'})
        if req.method == 'GET' and req.path == '/api/state':
            return self.api_state(req)
        if req.method not in ('GET', 'POST'):
            return jsonr({'ok': False, 'error': 'method not allowed'}, 405)
        if not req.path.startswith('/api/'):
            return jsonr({'ok': False, 'error': 'not found'}, 404)

        writes = req.method == 'POST'
        authed = self.authed(req)
        if writes:
            if self.throttled(ip):
                return jsonr({'ok': False, 'error': 'too many attempts, wait a minute'}, 429)
            if not self.password:
                return jsonr({'ok': False, 'error': 'read-only: set a password in config.ini '
                              '([web] password) to enable changes'}, 403)
            if req.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return jsonr({'ok': False, 'error': 'missing request header'}, 403)
            if not authed:
                if req.headers.get('authorization'):
                    self.note_failure(ip)
                return jsonr({'ok': False, 'error': 'locked', 'locked': True}, 401)
        elif self.password and not authed:
            return jsonr({'ok': False, 'error': 'locked', 'locked': True}, 401)

        name = (req.method + ' ' + req.path)
        handler = {
            'GET /api/driver_config': self.api_driver_config,
            'POST /api/driver_config': self.api_driver_config_save,
            'GET /api/battery': self.api_battery,
            'GET /api/starlink': self.api_starlink,
            'GET /api/fridge': self.api_fridge,
            'POST /api/fridge_set': self.api_fridge_set,
            'GET /api/mopeka_battery': self.api_mopeka_battery,
            'GET /api/starlink_dashboard': self.api_starlink_dashboard,
            'GET /api/starlink_energy': self.api_starlink_energy,
            'GET /api/starlink_log': self.api_starlink_log,
            'POST /api/starlink_test': self.api_starlink_test,
            'POST /api/telegram_test': self.api_telegram_test,
            'POST /api/debug_start': self.api_debug_start,
            'GET /api/debug_result': self.api_debug_result,
            'GET /api/update_check': self.api_update_check,
            'POST /api/update_run': self.api_update_run,
            'GET /api/update_log': self.api_update_log,
            'GET /api/rawlog': self.api_rawlog,
            'POST /api/rawlog_clear': self.api_rawlog_clear,
            'GET /api/serialbattery': self.api_serialbattery,
            'POST /api/serialbattery': self.api_serialbattery_save,
            'GET /api/raw': self.api_raw,
            'POST /api/raw': self.api_raw_save,
            'GET /api/log': self.api_log,
            'POST /api/restart': self.api_restart,
            'POST /api/service': self.api_service,
            'POST /api/pair_reset': self.api_pair_reset,
            'POST /api/reboot': self.api_reboot,
        }.get(name)
        if handler is None:
            return jsonr({'ok': False, 'error': 'not found'}, 404)
        return handler(req)

    # -------------------------------------------------------------- pages

    def page(self):
        try:
            with open(self.html, 'rb') as f:
                body = f.read()
        except OSError:
            return Response(500, 'index.html missing: copy data/shared_drivers/web too', 'text/plain')
        return Response(200, body, 'text/html; charset=utf-8')

    # ------------------------------------------------------------- state

    def init_services(self):
        try:
            return sorted(d for d in os.listdir(self.init_dir)
                          if os.path.isdir(os.path.join(self.init_dir, d)))
        except OSError:
            return []

    def link(self, name):
        return os.path.join(self.service_dir, name)

    def service_states(self):
        names = self.init_services()
        linked = [n for n in names if os.path.lexists(self.link(n))]
        status = {}
        if linked:
            try:
                out = subprocess.run(['svstat'] + [self.link(n) for n in linked],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5)
                status = parse_svstat(out.stdout.decode('utf-8', 'replace'))
            except (OSError, subprocess.SubprocessError) as e:
                log.warning('svstat failed: %s', e)
        result = []
        for n in names:
            s = status.get(self.link(n), {})
            result.append({'name': n, 'enabled': n in linked, 'state': s.get('state', 'down' if n in linked else 'off'),
                           'seconds': s.get('seconds'), 'pid': s.get('pid')})
        return result

    def dbus_dump(self, name):
        obj = self.bus.get_object(name, '/', introspect=False)
        raw = obj.GetValue(dbus_interface=BUS_ITEM, timeout=2)
        return dict(('/' + str(k).lstrip('/'), unwrap(v)) for k, v in raw.items())

    def devices(self):
        ours, batteries = [], []
        try:
            names = sorted(str(n) for n in self.bus.list_names() if str(n).startswith('com.victronenergy.'))
        except dbus.exceptions.DBusException:
            return ours, batteries
        for name in names:
            kind = name.split('.')[2] if name.count('.') >= 2 else ''
            if kind not in ('tank', 'temperature', 'battery'):
                continue
            try:
                values = self.dbus_dump(name)
            except dbus.exceptions.DBusException:
                continue
            process = values.get('/Mgmt/ProcessName') or ''
            if process in OUR_PROCESSES:
                ours.append({'service': name, 'kind': kind, 'values': values})
            elif kind == 'battery':
                keep = ('/ProductName', '/CustomName', '/Soc', '/Dc/0/Voltage', '/Dc/0/Current',
                        '/Dc/0/Power', '/Dc/0/Temperature', '/Connected')
                batteries.append({'service': name, 'kind': kind,
                                  'values': dict((k, values.get(k)) for k in keep)})
        return ours, batteries

    def battery_names(self):
        try:
            return sorted(str(n) for n in self.bus.list_names()
                          if str(n).startswith('com.victronenergy.battery.'))
        except dbus.exceptions.DBusException:
            return []

    def sample_batteries(self):
        """Timer: remember what every battery reports, for the graphs on the Battery tab."""
        names = self.battery_names()
        for name in names:
            try:
                self.recorder.add(name, self.dbus_dump(name))
            except dbus.exceptions.DBusException as e:
                log.debug('battery %s: %s', name, e)
        self.recorder.forget_missing(names)
        return True

    def api_battery(self, req):
        q = req.query
        raw = q.get('window', '')
        window = max(60, min(6 * 3600, int(raw))) if raw.isdigit() else 3600
        names = self.recorder.services()
        service = q.get('service') or ''
        if service not in names:
            service = names[0] if names else ''
        batteries = [{'service': n, 'name': (self.recorder.latest[n][1].get('/CustomName') or
                                             self.recorder.latest[n][1].get('/ProductName') or n)}
                     for n in names]
        out = {'ok': True, 'batteries': batteries, 'service': service, 'interval': self.recorder.interval,
               'window': window}
        if service:
            out['info'] = self.recorder.info(service)
            out['history'] = self.recorder.history(service, window)
        return jsonr(out)

    def api_rawlog(self, req):
        source = req.query.get('source', '')
        if source not in rawlog.SOURCES:
            source = rawlog.SOURCES[0]
        raw = req.query.get('lines', '')
        count = max(10, min(1000, int(raw))) if raw.isdigit() else 200
        sources = []
        for name in rawlog.SOURCES:
            try:
                size = os.path.getsize(os.path.join(self.rawlog_dir, name + '.log'))
            except OSError:
                size = 0
            sources.append({'name': name, 'bytes': size})
        return jsonr({'ok': True, 'source': source, 'sources': sources, 'dir': self.rawlog_dir,
                      'lines': rawlog.read_tail(source, count, self.rawlog_dir)})

    def api_rawlog_clear(self, req):
        source = req.form.get('source', '')
        if source not in rawlog.SOURCES:
            return jsonr({'ok': False, 'error': 'unknown source'}, 400)
        rawlog.clear(source, self.rawlog_dir)
        return jsonr({'ok': True})

    def api_starlink(self, req):
        raw = req.query.get('window', '')
        window = max(60, min(2 * 3600, int(raw))) if raw.isdigit() else 3600
        m = self.starlink
        out = dict(m.summary(), ok=True, window=window, interval=m.interval, host=m.host)
        out['status'] = m.status
        out['history'] = m.history.history(window)
        out['hist_error'] = m.hist_error
        return jsonr(out)

    # ---------------------------------------------------- Starlink in Venus OS
    STARLINK_PATHS = ('/Starlink/State', '/Starlink/Label', '/Starlink/DownMbps', '/Starlink/UpMbps', '/Starlink/LatencyMs',
                      '/Starlink/DropPercent', '/Starlink/ObstructedPercent', '/Starlink/Alerts', '/Starlink/Throttled')

    def publish_starlink(self):
        """
        Timer: show the dish in Venus OS. There is no Starlink device type, so it is a tank-type service whose
        level is the connection quality (0-100 %): it appears in the Device list and on the Levels page, and in VRM.
        """
        hub = self.starlink
        if not hub.enabled or not hub.venus_service or self.starlink_dev is False:
            return True
        if self.starlink_dev is None:
            try:
                import venus_service
                if venus_service.VeDbusService is None:
                    raise RuntimeError('velib_python not found')
                self.starlink_dev = venus_service.TankDevice(
                    self.bus, 'starlink', product_name='Starlink', connection='Starlink dish %s' % hub.host,
                    fluid_type=venus_service.FLUID_RAW_WATER, default_instance=hub.venus_instance,
                    custom_name='Starlink connection', timeout_s=60, extra_paths=self.STARLINK_PATHS)
            except Exception as e:                                   # noqa: BLE001 - a missing nicety must not stop the page
                log.warning('Starlink is not shown in Venus OS: %s', e)
                self.starlink_dev = False
                return True
        dev = self.starlink_dev
        st = hub.status
        if st is not None and hub.time is not None and hub.clock() - hub.time < 30:
            score, label = starlink.quality(st)
            alerts = ', '.join(st.get('alerts') or [])
            dev.publish(level=score, extra={
                '/Starlink/State': st.get('state'), '/Starlink/Label': label,
                '/Starlink/DownMbps': None if st.get('down_bps') is None else round(st['down_bps'] / 1e6, 1),
                '/Starlink/UpMbps': None if st.get('up_bps') is None else round(st['up_bps'] / 1e6, 1),
                '/Starlink/LatencyMs': None if st.get('latency_ms') is None else round(st['latency_ms'], 1),
                '/Starlink/DropPercent': None if st.get('drop_rate') is None else round(st['drop_rate'] * 100, 2),
                '/Starlink/ObstructedPercent': None if st.get('obstructed_pct') is None else round(st['obstructed_pct'], 2),
                '/Starlink/Alerts': alerts, '/Starlink/Throttled': int(bool(st.get('limit_down') or st.get('limit_up')))})
        else:
            dev.check_timeout()                                      # no news for a minute: disconnected, level cleared
        return True

    # ------------------------------------------------------ Mopeka battery
    def mopeka_values(self):
        ours, _ = self.devices()
        for d in ours:
            if d['kind'] == 'tank' and d['values'].get('/Mopeka/BatteryPercent') is not None:
                return d['values']
        return None

    def record_mopeka(self):
        """Timer: remember the gas sensor's battery, and warn when it runs low."""
        v = self.mopeka_values()
        if v is None:
            return True
        volts, pct = v.get('/BatteryVoltage'), v.get('/Mopeka/BatteryPercent')
        self.starlink.store.add_series('mopeka_battery', [int(time.time()), volts, pct])
        self.starlink.store.save()
        hub = self.starlink
        if hub.tg['kinds']['sensor'] and pct is not None and pct <= hub.tg['sensor_pct']:
            hub.alert('mopeka-battery', 'The gas sensor battery is low: %d %% (%s V). Replace it soon.' % (
                pct, '–' if volts is None else '%.2f' % volts), cooldown=12 * 3600)
        return True

    def api_mopeka_battery(self, req):
        v = self.mopeka_values() or {}
        rows = self.starlink.store.data.get('series', {}).get('mopeka_battery', [])
        return jsonr({'ok': True, 'available': bool(v), 'volts': v.get('/BatteryVoltage'), 'percent': v.get('/Mopeka/BatteryPercent'),
                      'low_at': self.starlink.tg['sensor_pct'], 'series': rows[-1500:]})

    # --------------------------------------------------------- fridge controls
    FRIDGE_OUTPUTS = ('power', 'eco', 'setpoint', 'setpoint2')

    def fridge_switch(self):
        try:
            names = [str(n) for n in self.bus.list_names() if str(n).startswith('com.victronenergy.switch.')]
        except dbus.exceptions.DBusException:
            return None, {}
        for name in names:
            try:
                values = self.dbus_dump(name)
            except dbus.exceptions.DBusException:
                continue
            if values.get('/Mgmt/ProcessName') == 'fridge_venus.py':
                return name, values
        return None, {}

    def api_fridge(self, req):
        name, v = self.fridge_switch()
        if not name:
            return jsonr({'ok': True, 'available': False})
        zones = []
        for ch in ('setpoint', 'setpoint2'):
            base = '/SwitchableOutput/%s/' % ch
            if base + 'Name' in v:
                zones.append({'output': ch, 'name': v.get(base + 'Settings/CustomName') or v.get(base + 'Name'),
                              'target': v.get(base + 'Dimming'), 'current': v.get(base + 'Measurement'),
                              'min': v.get(base + 'Settings/DimmingMin'), 'max': v.get(base + 'Settings/DimmingMax'),
                              'step': v.get(base + 'Settings/StepSize') or 1})
        return jsonr({'ok': True, 'available': True, 'service': name, 'connected': v.get('/Connected'),
                      'power': v.get('/SwitchableOutput/power/State'), 'eco': v.get('/SwitchableOutput/eco/State'), 'zones': zones})

    def api_fridge_set(self, req):
        output, raw = req.form.get('output', ''), req.form.get('value', '')
        if output not in self.FRIDGE_OUTPUTS:
            return jsonr({'ok': False, 'error': 'unknown control'}, 400)
        name, v = self.fridge_switch()
        if not name:
            return jsonr({'ok': False, 'error': 'the fridge controls are not available (the fridge has not been read yet, or control = no)'}, 409)
        try:
            if output in ('power', 'eco'):
                if raw not in ('0', '1'):
                    return jsonr({'ok': False, 'error': 'value must be 0 or 1'}, 400)
                path, value = '/SwitchableOutput/%s/State' % output, dbus.Int32(int(raw), variant_level=1)
            else:
                target = float(raw)
                lo = v.get('/SwitchableOutput/%s/Settings/DimmingMin' % output)
                hi = v.get('/SwitchableOutput/%s/Settings/DimmingMax' % output)
                if lo is not None and hi is not None and not lo <= target <= hi:
                    return jsonr({'ok': False, 'error': 'the set point must be between %g and %g' % (lo, hi)}, 400)
                path, value = '/SwitchableOutput/%s/Dimming' % output, dbus.Double(target, variant_level=1)
        except ValueError:
            return jsonr({'ok': False, 'error': 'not a number'}, 400)
        try:
            self.bus.call_blocking(name, path, BUS_ITEM, 'SetValue', 'v', [value], timeout=4)
        except dbus.exceptions.DBusException as e:
            return jsonr({'ok': False, 'error': 'the driver did not accept it: %s' % e}, 502)
        return jsonr({'ok': True, 'message': 'Sent. The fridge takes a few seconds to answer.'})

    def api_starlink_dashboard(self, req):
        m = self.starlink
        out = dict(m.dashboard(), ok=True, enabled=m.enabled, usage=m.usage())
        out['summary'] = m.summary()
        return jsonr(out)

    def api_starlink_energy(self, req):
        m = self.starlink
        return jsonr({'ok': True, 'enabled': m.enabled, 'energy': m.energy(), 'usage': m.usage(),
                      'hist_error': m.hist_error, 'hist_time': m.hist_time})

    def api_starlink_log(self, req):
        raw = req.query.get('window', '')
        window = max(3600, min(90 * 86400, int(raw))) if raw.isdigit() else 86400
        return jsonr(dict(self.starlink.log_view(window), ok=True, window=window))

    def api_starlink_test(self, req):
        m = self.starlink
        if not m.start_speedtest(manual=True):
            return jsonr({'ok': False, 'error': 'a test is already running'}, 409)
        return jsonr({'ok': True, 'message': 'Speed test started (about %d MB of data)' % (m.test_down_mb + m.test_up_mb)})

    def api_telegram_test(self, req):
        m = self.starlink
        if not (m.tg['enabled'] and m.tg['token'] and m.tg['chat_id']):
            return jsonr({'ok': False, 'error': 'Telegram is not set up: enable it and fill in the bot token and chat ID, save and restart the page'}, 400)
        m.test_message()
        return jsonr({'ok': True, 'message': 'Test message queued'})

    # ----------------------------------------------------- network tools
    DEBUG_HOST_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.:_-]{0,99}$')
    DEBUG_URL_RE = re.compile(r'^https?://[A-Za-z0-9][A-Za-z0-9.:_/?&=%+~#@,;!*()\[\]-]{0,200}$')

    def api_debug_start(self, req):
        tool, target = req.form.get('tool', ''), req.form.get('target', '').strip()
        host_tools = {'ping': (['ping', '-c', '4', '-W', '2'], 14), 'traceroute': (['traceroute', '-n', '-m', '15', '-w', '1', '-q', '1'], 35),
                      'dns': (['nslookup'], 12)}
        if tool in host_tools:
            if not self.DEBUG_HOST_RE.match(target):
                return jsonr({'ok': False, 'error': 'enter a host name or an IP address'}, 400)
            argv, limit = host_tools[tool]
            argv = argv + [target]
        elif tool == 'http':
            if not self.DEBUG_URL_RE.match(target):
                return jsonr({'ok': False, 'error': 'enter a URL starting with http:// or https://'}, 400)
            argv, limit = ['curl', '-sS', '-I', '-m', '10', '-w', '\n--- total %{time_total}s, connect %{time_connect}s, remote %{remote_ip}\n', target], 14
        else:
            return jsonr({'ok': False, 'error': 'unknown tool'}, 400)
        self.jobs = dict((k, v) for k, v in getattr(self, 'jobs', {}).items() if time.time() - v['time'] < 600)
        job_id = str(int(time.time() * 1000))
        job = self.jobs[job_id] = {'time': time.time(), 'done': False, 'out': '', 'cmd': ' '.join(argv[:3]) + ' ...'}

        def finished(rc, out):
            text = out.decode('utf-8', 'replace')[:20000]
            if rc is None:
                text += '\n(stopped after %d s)' % limit
            elif rc != 0 and not text.strip():
                text = 'exit code %s, no output' % rc
            job.update(done=True, out=text, rc=rc)
        starlink_hub.run_async(argv, finished, timeout=limit)
        return jsonr({'ok': True, 'job': job_id})

    def api_debug_result(self, req):
        job = getattr(self, 'jobs', {}).get(req.query.get('job', ''))
        if job is None:
            return jsonr({'ok': False, 'error': 'unknown job'}, 404)
        return jsonr({'ok': True, 'done': job['done'], 'out': job['out']})

    # ------------------------------------------------------------- updates
    UPDATE_REPO = 'mich-crypto/Venusosmods'
    update_info = None

    def installed_version(self):
        try:
            with open(os.path.join(HERE, '.version')) as f:
                return f.read().strip()[:40] or None
        except OSError:
            return None

    def api_update_check(self, req):
        info = self.update_info or {}
        stale = time.time() - info.get('checked', 0) > 6 * 3600
        if (req.query.get('refresh') == '1' or stale) and not info.get('checking'):
            self.update_info = info = dict(info, checking=True)

            def got(rc, out):
                text = out.decode('utf-8', 'replace') if rc == 0 else ''
                sha = re.search(r'"sha":\s*"([0-9a-f]{40})"', text)
                msg = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', text)
                date = re.search(r'"date":\s*"([^"]+)"', text)
                self.update_info = {'checked': time.time(), 'checking': False,
                                    'error': None if sha else 'could not reach GitHub',
                                    'latest': sha.group(1) if sha else (info.get('latest')),
                                    'message': (msg.group(1).split('\\n')[0] if msg else info.get('message')),
                                    'date': date.group(1) if date else info.get('date')}
            starlink_hub.run_async(['curl', '-s', '-m', '10', 'https://api.github.com/repos/%s/commits/master' % self.UPDATE_REPO],
                                   got, timeout=14)
        installed = self.installed_version()
        info = self.update_info or {}
        return jsonr({'ok': True, 'installed': installed, 'latest': info.get('latest'), 'message': info.get('message'),
                      'date': info.get('date'), 'error': info.get('error'), 'checking': bool(info.get('checking')),
                      'available': bool(info.get('latest') and installed != info.get('latest')),
                      'running': self.update_running()})

    UPDATE_LOG = '/tmp/venusosmods-update.log'

    def update_running(self):
        proc = getattr(self, 'update_proc', None)
        return proc is not None and proc.poll() is None

    def api_update_run(self, req):
        if self.update_running():
            return jsonr({'ok': False, 'error': 'an update is already running'}, 409)
        script = os.path.join(HERE, 'update.sh')
        if not os.path.isfile(script):
            return jsonr({'ok': False, 'error': 'update.sh not found'}, 400)
        try:
            log_file = open(self.UPDATE_LOG, 'w')
            self.update_proc = subprocess.Popen(['sh', script], stdout=log_file, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return jsonr({'ok': False, 'error': 'could not start the update: %s' % e}, 500)
        return jsonr({'ok': True, 'message': 'Update started. The page restarts when it is done.'})

    def api_update_log(self, req):
        try:
            with open(self.UPDATE_LOG) as f:
                text = f.read()[-6000:]
        except OSError:
            text = ''
        return jsonr({'ok': True, 'text': text, 'running': self.update_running()})

    def venus_version(self):
        try:
            with open('/opt/victronenergy/version') as f:
                return f.readline().strip()
        except OSError:
            return ''

    def api_state(self, req):
        ours, batteries = self.devices()
        sb = self.serialbattery_path()
        return jsonr({
            'time': int(time.time()), 'host': socket.gethostname(), 'venus': self.venus_version(),
            'version': VERSION, 'password_set': bool(self.password), 'authed': self.authed(req),
            'services': self.service_states(), 'devices': ours, 'batteries': batteries,
            'serialbattery': bool(sb), 'starlink': self.starlink.summary(),
        })

    # ------------------------------------------------------ driver config

    def read_text(self, path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ''

    def api_driver_config(self, req):
        text = self.read_text(self.driver_config)
        sections = ct.read_driver_config(text)
        for s in sections:                       # never send secrets to the browser
            for f in s['fields']:
                if f['type'] == 'secret':
                    s['values'][f['key']] = ''
                    s.setdefault('secret_set', {})[f['key']] = bool(
                        ct.parse_ini(text).get(s['section'], {}).get(f['key']))
        have = set(x['kind'] for x in sections)
        optional = [{'section': k, 'kind': k, 'title': ct.SCHEMA[k]['title'], 'values': {}, 'fields': ct.SCHEMA[k]['fields'],
                     'presets': [], 'secret_set': {}} for k in self.OPTIONAL_SECTIONS if k not in have]
        return jsonr({'ok': True, 'path': self.driver_config, 'exists': bool(text), 'sections': sections,
                      'optional': optional, 'services': self.init_services()})

    OPTIONAL_SECTIONS = ('starlink', 'telegram')           # work without a section; saving one creates it

    def restart_service(self, name):
        link = self.link(name)
        if not os.path.lexists(link):
            return False
        subprocess.run(['svc', '-t', link], timeout=5)
        return True

    def api_driver_config_save(self, req):
        form = req.form
        text = self.read_text(self.driver_config)
        parsed = ct.parse_ini(text)
        section = next((s for s in parsed if s.lower() == form.get('section', '').lower()), None)
        if section is None and form.get('section') in self.OPTIONAL_SECTIONS:
            section = form['section']
            parsed[section] = {}
        if section is None:
            return jsonr({'ok': False, 'error': 'unknown section'}, 400)
        kind = ct.kind_of(section, parsed[section])
        if kind is None:
            return jsonr({'ok': False, 'error': 'not a driver section'}, 400)
        secrets = set(f['key'] for f in ct.SCHEMA[kind]['fields'] if f['type'] == 'secret')
        values = dict((k[2:], v) for k, v in form.items() if k.startswith('f.'))
        for key in secrets:
            if values.get(key, '') == '':
                values.pop(key, None)             # blank secret = unchanged
        clean, errors = ct.validate_section(kind, values)
        if errors:
            return jsonr({'ok': False, 'errors': errors, 'error': 'Please correct the marked fields'}, 400)
        new = ct.set_values(text, section, dict((k, v if v != '' else None) for k, v in clean.items()))
        problem = ct.check_syntax(new)
        if problem:
            return jsonr({'ok': False, 'error': 'file would be invalid: ' + problem}, 400)
        ct.write_file(self.driver_config, new)
        # read it back: what the page says it saved is what is in the file now
        written = ct.parse_ini(self.read_text(self.driver_config)).get(section, {})
        saved = dict((k, '(hidden)' if k in secrets else written.get(k.lower())) for k in clean)
        missing = [k for k, v in clean.items() if v != '' and written.get(k.lower()) is None]
        if missing:
            return jsonr({'ok': False, 'error': 'the file does not contain %s after saving: not saved' % ', '.join(missing)}, 500)
        result = {'ok': True, 'message': 'Saved', 'restarted': False, 'saved': saved}
        if kind in ('web', 'starlink', 'telegram'):
            # these are read once at start-up, so a save is only real after the page restarts
            GLib.timeout_add(800, self._restart_self)
            result.update(restarted=True, message='Saved. This page restarts in a moment.')
        elif form.get('restart') == '1':
            if self.restart_service(section):
                result.update(restarted=True, message='Saved and restarted')
            else:
                result['message'] = 'Saved (the service is not enabled, nothing to restart)'
        return jsonr(result)

    def _restart_self(self):
        self.restart_service('web_ui')
        return False

    # -------------------------------------------------- dbus-serialbattery

    def serialbattery_path(self):
        if self.serialbattery_dir:
            return self.serialbattery_dir if os.path.isfile(
                os.path.join(self.serialbattery_dir, 'config.default.ini')) else None
        return ct.find_serialbattery()

    def api_serialbattery(self, req):
        path = self.serialbattery_path()
        if not path:
            return jsonr({'ok': True, 'installed': False})
        default = self.read_text(os.path.join(path, 'config.default.ini'))
        user = self.read_text(os.path.join(path, 'config.ini'))
        view = ct.serialbattery_view(default, user)
        for g in view['groups']:
            for e in g['entries']:
                if e.get('secret'):               # keep passwords out of the page
                    e['has_value'] = bool(e.get('value'))
                    e['value'] = None if not e.get('value') else ''
        return jsonr({'ok': True, 'installed': True, 'dir': path, 'view': view})

    def api_serialbattery_save(self, req):
        path = self.serialbattery_path()
        if not path:
            return jsonr({'ok': False, 'error': 'dbus-serialbattery is not installed'}, 400)
        default = self.read_text(os.path.join(path, 'config.default.ini'))
        user_path = os.path.join(path, 'config.ini')
        user = self.read_text(user_path)
        changes = {}
        for k, v in req.form.items():
            if k.startswith('s.'):
                changes[k[2:]] = v
            elif k.startswith('r.') and v == '1':
                changes[k[2:]] = None
        entries = dict((e['key'], e) for g in ct.parse_default_ini(default) for e in g['entries'])
        for key in list(changes):                 # blank password field = unchanged
            if entries.get(key, {}).get('secret') and changes[key] == '':
                del changes[key]
        if not changes:
            return jsonr({'ok': True, 'message': 'Nothing to change', 'changed': []})
        new, errors = ct.apply_serialbattery(default, user, changes)
        if errors:
            return jsonr({'ok': False, 'errors': errors, 'error': 'Please correct the marked settings'}, 400)
        problem = ct.check_syntax(new)
        if problem:
            return jsonr({'ok': False, 'error': 'file would be invalid: ' + problem}, 400)
        ct.write_file(user_path, new)
        reboot = [k for k in changes if k in ct.REBOOT_KEYS]
        result = {'ok': True, 'message': 'Saved', 'changed': sorted(changes), 'reboot_needed': bool(reboot),
                  'restarted': False}
        if req.form.get('restart') == '1' and not reboot:
            result['restarted'] = self.run_restart_script(path)
            result['message'] = 'Saved and restarted' if result['restarted'] else \
                'Saved (restart.sh not found: restart the driver or reboot)'
        elif reboot:
            result['message'] = 'Saved. Bluetooth/CAN settings need a reboot of the Pi.'
        return jsonr(result)

    def run_restart_script(self, path):
        script = os.path.join(path, 'restart.sh')
        if not os.path.isfile(script):
            return False
        subprocess.Popen(['sh', script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True

    # ----------------------------------------------------------- raw files

    def raw_path(self, which):
        if which == 'driver':
            return self.driver_config
        if which == 'serialbattery':
            path = self.serialbattery_path()
            return os.path.join(path, 'config.ini') if path else None
        return None

    def api_raw(self, req):
        which = req.query.get('file', '')
        path = self.raw_path(which)
        if not path:
            return jsonr({'ok': False, 'error': 'unknown file'}, 400)
        return jsonr({'ok': True, 'path': path, 'text': self.read_text(path)})

    def api_raw_save(self, req):
        which = req.form.get('file', '')
        path = self.raw_path(which)
        if not path:
            return jsonr({'ok': False, 'error': 'unknown file'}, 400)
        text = req.form.get('text', '').replace('\r\n', '\n')
        if text and not text.endswith('\n'):
            text += '\n'
        problem = ct.check_syntax(text)
        errors = {}
        if not problem and which == 'driver':
            for section, values in ct.parse_ini(text).items():
                kind = ct.kind_of(section, values)
                if kind:
                    _, errs = ct.validate_section(kind, values)
                    errors.update(('%s.%s' % (section, k), v) for k, v in errs.items())
        elif not problem and which == 'serialbattery':
            default = self.read_text(os.path.join(self.serialbattery_path(), 'config.default.ini'))
            entries = dict((e['key'].lower(), e) for g in ct.parse_default_ini(default) for e in g['entries'])
            for key, value in ct.parse_ini(text).get('DEFAULT', {}).items():
                if key in entries:
                    _, err = ct.normalize_value(entries[key], value)
                    if err:
                        errors[key] = '%s %s' % (entries[key]['label'], err)
        if problem or errors:
            return jsonr({'ok': False, 'error': problem or 'Some values are not valid',
                          'errors': errors}, 400)
        ct.write_file(path, text)
        return jsonr({'ok': True, 'message': 'Saved'})

    # --------------------------------------------------------------- logs

    def log_file(self, name):
        if name == 'dbus-serialbattery':
            found = sorted(glob.glob(os.path.join(self.log_dir, 'dbus-serialbattery*', 'current')))
            return found[0] if found else None
        if name in self.init_services():
            return os.path.join(self.log_dir, name, 'current')
        return None

    def api_log(self, req):
        path = self.log_file(req.query.get('service', ''))
        if not path:
            return jsonr({'ok': False, 'error': 'unknown service'}, 400)
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 24000))
                raw = f.read().decode('utf-8', 'replace')
        except OSError:
            raw = ''
        return jsonr({'ok': True, 'lines': decode_log(raw)})

    # ----------------------------------------------------------- services

    def api_restart(self, req):
        target = req.form.get('target', '')
        if target == 'serialbattery':
            path = self.serialbattery_path()
            if path and self.run_restart_script(path):
                return jsonr({'ok': True, 'message': 'dbus-serialbattery is restarting'})
            return jsonr({'ok': False, 'error': 'restart.sh not found'}, 400)
        if target not in self.init_services():
            return jsonr({'ok': False, 'error': 'unknown service'}, 400)
        if target == 'web_ui':
            GLib.timeout_add(800, self._restart_self)
            return jsonr({'ok': True, 'message': 'This page restarts in a moment'})
        if not self.restart_service(target):
            return jsonr({'ok': False, 'error': '%s is not enabled' % target}, 400)
        return jsonr({'ok': True, 'message': '%s restarted' % target})

    def rc_line(self, name):
        return '[ -e %s ] || ln -s %s %s' % (self.link(name), os.path.join(self.init_dir, name),
                                             self.link(name))

    def api_service(self, req):
        name, action = req.form.get('name', ''), req.form.get('action', '')
        if name not in self.init_services() or action not in ('enable', 'disable'):
            return jsonr({'ok': False, 'error': 'unknown service or action'}, 400)
        if name == 'web_ui' and action == 'disable':
            return jsonr({'ok': False, 'error': 'Disable this page over SSH, it cannot be '
                          'switched back on from here'}, 400)
        line = self.rc_line(name)
        rc = self.read_text(self.rc_local) or '#!/bin/sh\n'
        lines = [l for l in rc.split('\n') if l.strip() != line]
        if action == 'enable':
            if not os.path.lexists(self.link(name)):
                os.symlink(os.path.join(self.init_dir, name), self.link(name))
            lines = [l for l in lines if l != ''] + [line]
        else:
            if os.path.lexists(self.link(name)):
                subprocess.run(['svc', '-d', self.link(name), self.link(name) + '/log'], timeout=5)
                os.remove(self.link(name))
        with open(self.rc_local, 'w') as f:
            f.write('\n'.join(lines).rstrip('\n') + '\n')
        os.chmod(self.rc_local, 0o755)
        return jsonr({'ok': True, 'message': '%s %sd' % (name, action)})

    def settings_set(self, path, value):
        self.bus.call_blocking('com.victronenergy.settings', path, BUS_ITEM, 'SetValue', 'v',
                               [dbus.String(value, variant_level=1)], timeout=5)

    def api_pair_reset(self, req):
        section = req.form.get('section', '')
        parsed = ct.parse_ini(self.read_text(self.driver_config))
        match = next((s for s in parsed if s.lower() == section.lower()), None)
        if match is None or ct.kind_of(match, parsed[match]) != 'mopeka':
            return jsonr({'ok': False, 'error': 'not a gas sensor section'}, 400)
        uid = parsed[match].get('service', match)
        try:
            self.settings_set('/Settings/Devices/%s/Mac' % uid, '')
        except dbus.exceptions.DBusException as e:
            return jsonr({'ok': False, 'error': 'could not clear the pairing: %s' % e}, 500)
        self.restart_service(match)
        return jsonr({'ok': True, 'message': 'Pairing cleared: press the sync button on the new sensor'})

    def api_reboot(self, req):
        try:
            self.bus.call_blocking('com.victronenergy.platform', '/Device/Reboot', BUS_ITEM, 'SetValue',
                                   'v', [dbus.Int32(1, variant_level=1)], timeout=5)
        except dbus.exceptions.DBusException as e:
            return jsonr({'ok': False, 'error': 'reboot failed: %s' % e}, 500)
        return jsonr({'ok': True, 'message': 'Rebooting...'})


# ------------------------------------------------------- HTTP on GLib

STATUS = {200: 'OK', 302: 'Found', 400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden',
          404: 'Not Found', 405: 'Method Not Allowed', 413: 'Payload Too Large',
          429: 'Too Many Requests', 500: 'Internal Server Error'}


def build_response(resp):
    head = ['HTTP/1.1 %d %s' % (resp.status, STATUS.get(resp.status, 'OK')),
            'Content-Type: ' + resp.ctype, 'Content-Length: %d' % len(resp.body),
            'Cache-Control: no-store', 'X-Content-Type-Options: nosniff',
            'Content-Security-Policy: default-src \'self\'; style-src \'self\' \'unsafe-inline\'; '
            'script-src \'self\' \'unsafe-inline\'; img-src \'self\' data:; frame-ancestors \'none\'',
            'Connection: close']
    head += ['%s: %s' % kv for kv in resp.headers.items()]
    return ('\r\n'.join(head) + '\r\n\r\n').encode('latin-1') + resp.body


class Client(object):
    """One HTTP connection: read the request, answer, close."""

    def __init__(self, conn, peer, app):
        self.conn, self.peer, self.app = conn, peer, app
        conn.setblocking(False)
        self.buf = b''
        self.out = b''
        self.closed = False
        self.in_id = GLib.io_add_watch(conn.fileno(), GLib.PRIORITY_DEFAULT,
                                       GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_read)
        self.out_id = None
        self.timeout_id = GLib.timeout_add_seconds(20, self.on_timeout)

    def on_timeout(self):
        self.timeout_id = None          # returning False ends this source
        self.close()
        return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        for attr in ('in_id', 'out_id', 'timeout_id'):
            source = getattr(self, attr)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, attr, None)
        try:
            self.conn.close()
        except OSError:
            pass

    def on_read(self, fd, condition):
        if self.closed:
            return False
        try:
            data = self.conn.recv(65536)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            self.in_id = None
            self.close()
            return False
        if not data:
            self.in_id = None
            self.close()
            return False
        self.buf += data
        request = self.parse()
        if request is None:
            return True                           # need more data
        self.in_id = None                         # this watch ends by returning False
        self.respond(request if isinstance(request, Response) else self.app.handle(request))
        return False

    def parse(self):
        end = self.buf.find(b'\r\n\r\n')
        if end < 0:
            if len(self.buf) > 16384:
                return Response(413, b'', 'text/plain')
            return None
        head = self.buf[:end].decode('latin-1').split('\r\n')
        try:
            method, target, _ = head[0].split(' ', 2)
        except ValueError:
            return Response(400, b'bad request', 'text/plain')
        headers = {}
        for line in head[1:]:
            k, _, v = line.partition(':')
            headers[k.strip().lower()] = v.strip()
        try:
            length = int(headers.get('content-length', '0') or 0)
        except ValueError:
            return Response(400, b'bad request', 'text/plain')
        if length > MAX_BODY:
            return Response(413, b'', 'text/plain')
        body = self.buf[end + 4:]
        if len(body) < length:
            return None
        path, _, qs = target.partition('?')
        return Request(method.upper(), path, parse_form(qs), headers, body[:length], self.peer)

    def respond(self, resp):
        self.out = build_response(resp)
        try:
            sent = self.conn.send(self.out)
            self.out = self.out[sent:]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self.close()
            return
        if not self.out:
            self.close()
            return
        self.out_id = GLib.io_add_watch(self.conn.fileno(), GLib.PRIORITY_DEFAULT,
                                        GLib.IO_OUT | GLib.IO_ERR | GLib.IO_HUP, self.on_writable)

    def on_writable(self, fd, condition):
        if self.closed:
            return False
        try:
            sent = self.conn.send(self.out)
            self.out = self.out[sent:]
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            self.out_id = None
            self.close()
            return False
        if self.out:
            return True
        self.out_id = None
        self.close()
        return False


class Server(object):
    def __init__(self, app, host, port):
        self.app = app
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self.on_accept)

    def on_accept(self, fd, condition):
        try:
            conn, peer = self.sock.accept()
        except (BlockingIOError, InterruptedError):
            return True
        except OSError as e:
            if e.errno in (23, 24):                  # ENFILE, EMFILE
                log.warning('too many open files')
            return True
        Client(conn, peer, self.app)
        return True


def open_firewall(port):
    """Allow the port from local-network addresses only (idempotent)."""
    for source in LAN_RANGES:
        rule = ['INPUT', '-p', 'tcp', '--dport', str(port), '-s', source, '-j', 'ACCEPT']
        try:
            if subprocess.run(['iptables', '-C'] + rule, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5).returncode != 0:
                subprocess.run(['iptables', '-I'] + rule, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, check=True)
                log.info('firewall: opened port %d for %s', port, source)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning('could not open the firewall for port %d (%s); use an SSH tunnel instead', port, e)
            return


def main():
    ap = argparse.ArgumentParser(description='Venus OS mods settings page')
    ap.add_argument('--config', default=os.path.join(HERE, 'config.ini'))
    ap.add_argument('--section', default='web')
    ap.add_argument('--starlink-section', default='starlink')
    ap.add_argument('--telegram-section', default='telegram')
    ap.add_argument('--state-dir', default=None)
    ap.add_argument('--init-dir', default='/data/etc/init.d')
    ap.add_argument('--service-dir', default='/service')
    ap.add_argument('--log-dir', default='/var/log')
    ap.add_argument('--rc-local', default='/data/rc.local')
    ap.add_argument('--serialbattery-dir', default=None)
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--bind', default=None)
    ap.add_argument('--no-firewall', action='store_true')
    ap.add_argument('--rawlog-dir', default=None)
    ap.add_argument('--sample-s', type=int, default=10, help='seconds between battery samples')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='%(levelname)s %(name)s: %(message)s')

    parser = configparser.ConfigParser(interpolation=None)    # a % in the password is fine here
    parser.read(args.config)
    cfg = parser[args.section] if parser.has_section(args.section) else {}
    get = lambda k, d='': cfg.get(k, d) if cfg else d
    password = get('password')
    port = args.port or int(get('port', '8088') or 8088)
    host = args.bind or get('bind', '0.0.0.0') or '0.0.0.0'
    if not password:
        log.warning('No password in [%s] of %s: the page is read-only', args.section, args.config)

    starlink_cfg = dict(parser[args.starlink_section]) if parser.has_section(args.starlink_section) else {}
    telegram_cfg = dict(parser[args.telegram_section]) if parser.has_section(args.telegram_section) else {}

    DBusGMainLoop(set_as_default=True)
    app = App({'driver_config': args.config, 'init_dir': args.init_dir, 'service_dir': args.service_dir,
               'log_dir': args.log_dir, 'rc_local': args.rc_local, 'password': password,
               'serialbattery_dir': args.serialbattery_dir, 'sample_s': max(1, args.sample_s),
               'starlink': starlink_cfg, 'telegram': telegram_cfg, 'rawlog_dir': args.rawlog_dir,
               'state_dir': args.state_dir})
    server = Server(app, host, port)
    log.info('Settings page on http://%s:%d/', host, server.port)
    if not args.no_firewall and get('open_firewall', 'yes').lower() in ct.TRUE:
        open_firewall(server.port)

    app.sample_batteries()
    hub = app.starlink
    if hub.enabled:
        hub.poll_status()
        GLib.timeout_add_seconds(hub.interval, hub.poll_status)
        GLib.timeout_add_seconds(hub.history_interval, hub.poll_history)
        GLib.timeout_add_seconds(hub.icmp_interval, hub.poll_icmp)
        GLib.timeout_add_seconds(60, hub.maybe_speedtest)
        GLib.timeout_add_seconds(60, hub.flush)
        GLib.timeout_add_seconds(5, lambda: (hub.poll_icmp(), hub.poll_wan(), False)[2])
        GLib.timeout_add_seconds(300, hub.poll_wan)
        GLib.timeout_add_seconds(300, lambda: (hub.store.save(), True)[1])
    GLib.timeout_add_seconds(app.recorder.interval, app.sample_batteries)
    GLib.timeout_add_seconds(5, app.publish_starlink)
    GLib.timeout_add_seconds(600, app.record_mopeka)
    GLib.timeout_add_seconds(30, lambda: (app.record_mopeka(), False)[1])
    loop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, loop.quit)
    loop.run()
    hub.store.save(force=True)                    # the books survive a restart of the page


if __name__ == '__main__':
    main()
