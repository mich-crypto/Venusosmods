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
import starlink  # noqa: E402

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


# ------------------------------------------------------------- starlink dish

class StarlinkMonitor(object):
    """
    Asks the dish for its status every few seconds. The request goes out with curl (no HTTP or gRPC
    libraries on Venus OS) and is watched from the main loop, so a dish that does not answer never
    blocks the page. Tries gRPC-web on the configured port, on port 80 and plain gRPC on 9200, and
    sticks with whichever answered last.
    """

    def __init__(self, cfg):
        self.enabled = str(cfg.get('enabled', 'yes')).strip().lower() in ct.TRUE
        self.host = cfg.get('host') or '192.168.100.1'
        self.interval = max(2, int(cfg.get('interval') or 5))
        self.port = int(cfg.get('port') or 9201)
        self.status = None            # last decoded status
        self.time = None              # when it was read
        self.error = None
        self.via = None
        self.fails = 0
        self.method = 0
        self.busy = False
        self.skip_until = 0
        self.history = starlink.History(interval=self.interval)

    def candidates(self):
        web = ['--http1.1', '-H', 'Content-Type: application/grpc-web+proto', '-H', 'X-Grpc-Web: 1',
               '-H', 'Accept: application/grpc-web+proto']
        native = ['--http2-prior-knowledge', '-H', 'Content-Type: application/grpc', '-H', 'TE: trailers']
        path = starlink.SERVICE_PATH
        return [('gRPC-web port %d' % self.port, web, 'http://%s:%d%s' % (self.host, self.port, path)),
                ('gRPC-web port 80', web, 'http://%s%s' % (self.host, path)),
                ('gRPC port 9200', native, 'http://%s:9200%s' % (self.host, path))]

    def summary(self):
        age = None if self.time is None else int(time.time() - self.time)
        s = self.status or {}
        return {'enabled': self.enabled, 'reachable': self.error is None and self.status is not None,
                'error': self.error, 'age': age, 'via': self.via, 'state': s.get('state'),
                'down_bps': s.get('down_bps'), 'up_bps': s.get('up_bps'), 'latency_ms': s.get('latency_ms'),
                'obstructed_pct': s.get('obstructed_pct'), 'alerts': s.get('alerts')}

    # -- polling
    def poll(self):
        """Timer callback (returns True to stay armed)."""
        if not self.enabled or self.busy or time.time() < self.skip_until:
            return True
        self.busy = True
        self._try(0)
        return True

    def _try(self, tried):
        cands = self.candidates()
        idx = (self.method + tried) % len(cands)
        name, args, url = cands[idx]
        try:
            proc = subprocess.Popen(['curl', '-s', '-m', '4', '-X', 'POST'] + args + ['--data-binary', '@-', url],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            proc.stdin.write(starlink.status_request())
            proc.stdin.close()
        except OSError as e:
            self._finish(tried, idx, name, None, 'could not run curl: %s' % e)
            return
        deadline = time.time() + 6
        GLib.timeout_add(100, self._check, proc, tried, idx, name, deadline)

    def _check(self, proc, tried, idx, name, deadline):
        if proc.poll() is None and time.time() < deadline:
            return True
        if proc.poll() is None:
            proc.kill()
        data = proc.stdout.read()
        proc.stdout.close()
        proc.wait()
        self._finish(tried, idx, name, data, None if proc.returncode == 0 else 'curl exit code %s' % proc.returncode)
        return False

    def _finish(self, tried, idx, name, data, failure):
        """Handle one answer (or its absence); also called directly by the tests."""
        error = failure
        status = None
        if error is None:
            try:
                status = starlink.decode_reply(data)
            except ValueError as e:
                error = str(e)
        if status is not None:
            self.status, self.time, self.error, self.via = status, time.time(), None, name
            self.method, self.fails, self.busy = idx, 0, False
            self.history.add(status)
            return
        self.error = '%s: %s' % (name, error)
        if tried + 1 < len(self.candidates()):
            self._try(tried + 1)
            return
        self.fails += 1
        self.busy = False
        if self.fails >= 5:
            self.skip_until = time.time() + 60          # nobody home: only look once a minute
        log.debug('Starlink dish not read: %s', self.error)


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
        self.starlink = StarlinkMonitor(opts.get('starlink') or {})

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

    def api_starlink(self, req):
        raw = req.query.get('window', '')
        window = max(60, min(2 * 3600, int(raw))) if raw.isdigit() else 3600
        m = self.starlink
        out = dict(m.summary(), ok=True, window=window, interval=m.interval, host=m.host)
        out['status'] = m.status
        out['history'] = m.history.history(window)
        return jsonr(out)

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
        return jsonr({'ok': True, 'path': self.driver_config, 'exists': bool(text), 'sections': sections,
                      'services': self.init_services()})

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
        result = {'ok': True, 'message': 'Saved', 'restarted': False}
        if form.get('restart') == '1':
            if kind in ('web', 'starlink'):
                GLib.timeout_add(800, self._restart_self)
                result.update(restarted=True, message='Saved. This page restarts in a moment.')
            elif self.restart_service(section):
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
    ap.add_argument('--init-dir', default='/data/etc/init.d')
    ap.add_argument('--service-dir', default='/service')
    ap.add_argument('--log-dir', default='/var/log')
    ap.add_argument('--rc-local', default='/data/rc.local')
    ap.add_argument('--serialbattery-dir', default=None)
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--bind', default=None)
    ap.add_argument('--no-firewall', action='store_true')
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

    DBusGMainLoop(set_as_default=True)
    app = App({'driver_config': args.config, 'init_dir': args.init_dir, 'service_dir': args.service_dir,
               'log_dir': args.log_dir, 'rc_local': args.rc_local, 'password': password,
               'serialbattery_dir': args.serialbattery_dir, 'sample_s': max(1, args.sample_s),
               'starlink': starlink_cfg})
    server = Server(app, host, port)
    log.info('Settings page on http://%s:%d/', host, server.port)
    if not args.no_firewall and get('open_firewall', 'yes').lower() in ct.TRUE:
        open_firewall(server.port)

    app.sample_batteries()
    if app.starlink.enabled:
        app.starlink.poll()
        GLib.timeout_add_seconds(app.starlink.interval, app.starlink.poll)
    GLib.timeout_add_seconds(app.recorder.interval, app.sample_batteries)
    loop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, loop.quit)
    loop.run()


if __name__ == '__main__':
    main()
