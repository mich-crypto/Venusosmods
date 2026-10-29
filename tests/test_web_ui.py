"""
Settings web page: helpers and every API route, against temporary folders.
D-Bus/GLib are fakes (tests/fakes.py); `svc` and `svstat` are small shell
scripts that record how they were called. The real sockets and the real D-Bus
are exercised by tests/integration_dbus.py.
"""

import os
import socket
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import fakes  # noqa: E402,F401
import config_tools as ct  # noqa: E402
import web_ui  # noqa: E402

with open(os.path.join(HERE, 'data', 'serialbattery_config.default.ini')) as f:
    SB_DEFAULT = f.read()
with open(os.path.join(HERE, '..', 'data', 'shared_drivers', 'config.ini.example')) as f:
    EXAMPLE = f.read()

CSRF = {'x-requested-with': 'venussettings'}


def basic(password, user='x'):
    import base64
    return {'authorization': 'Basic ' + base64.b64encode(('%s:%s' % (user, password)).encode()).decode()}


def body_of(resp):
    import json
    return json.loads(resp.body.decode())


class HelperTests(unittest.TestCase):
    def test_to_json(self):
        import json
        obj = {'a': [1, 2.5, None, True, False, 'x"y\\z\n\té '], 'n': float('nan'),
               'nested': {'k': (1, 2)}, 3: 'int key'}
        out = json.loads(web_ui.to_json(obj))
        self.assertEqual(out['a'], [1, 2.5, None, True, False, 'x"y\\z\n\té '])
        self.assertIsNone(out['n'])
        self.assertEqual(out['nested'], {'k': [1, 2]})
        self.assertEqual(out['3'], 'int key')
        self.assertEqual(web_ui.to_json(True), 'true')               # bool is not printed as 1

    def test_forms(self):
        self.assertEqual(web_ui.parse_form('a=1&b=x%20y&c=%E2%82%AC&d=a+b&e&f=%4'),
                         {'a': '1', 'b': 'x y', 'c': '€', 'd': 'a b', 'e': '', 'f': '%4'})
        self.assertEqual(web_ui.parse_form(b'k=v%26w'), {'k': 'v&w'})

    def test_base64(self):
        self.assertEqual(web_ui.b64decode('dXNlcjpwYXNz'), b'user:pass')
        self.assertEqual(web_ui.b64decode('YTpi'), b'a:b')
        self.assertEqual(web_ui.b64decode('YTpiYw=='), b'a:bc')
        self.assertEqual(web_ui.b64decode('YTpi\n'), b'a:b')
        with self.assertRaises(ValueError):
            web_ui.b64decode('not*base64')

    def test_log_decoding(self):
        os.environ['TZ'] = 'UTC'
        time.tzset()
        stamp = '@40000000%08x%08x' % (1760932036 + 10, 0)
        self.assertEqual(web_ui.tai64n_to_epoch(stamp), 1760932036)
        lines = web_ui.decode_log('%s INFO hello\nplain line\n' % stamp)
        self.assertEqual(lines, [time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(1760932036)) + ' INFO hello',
                                 'plain line'])
        many = '\n'.join('line %d' % i for i in range(300))
        self.assertEqual(len(web_ui.decode_log(many, limit=80)), 80)

    def test_svstat(self):
        out = ('/service/fridge: up (pid 123) 45 seconds\n'
               '/service/jk: down 7 seconds, normally up\n'
               'svstat: warning: unable to open /service/x/supervise/ok: file does not exist\n')
        self.assertEqual(web_ui.parse_svstat(out), {
            '/service/fridge': {'state': 'up', 'pid': 123, 'seconds': 45},
            '/service/jk': {'state': 'down', 'pid': None, 'seconds': 7}})

    def test_unwrap(self):
        d = fakes.sys.modules['dbus']
        value = d.Dictionary({'a': d.Int32(5), 'b': d.Array([], signature='i'), 'c': d.Boolean(1),
                              'd': d.Array([d.Double(1.5), d.String('x')]), 'e': d.Byte(7)})
        self.assertEqual(web_ui.unwrap(value), {'a': 5, 'b': None, 'c': True, 'd': [1.5, 'x'], 'e': 7})
        self.assertIs(web_ui.unwrap(d.Boolean(0)), False)


class HttpParserTests(unittest.TestCase):
    def client(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return web_ui.Client(a, ('1.2.3.4', 5), None)

    def test_get_with_query(self):
        c = self.client()
        c.buf = b'GET /api/log?service=fridge&x=a%20b HTTP/1.1\r\nHost: x\r\nX-Requested-With: venussettings\r\n\r\n'
        r = c.parse()
        self.assertEqual((r.method, r.path, r.query), ('GET', '/api/log', {'service': 'fridge', 'x': 'a b'}))
        self.assertEqual(r.headers['x-requested-with'], 'venussettings')
        self.assertEqual(r.peer, ('1.2.3.4', 5))

    def test_post_waits_for_the_whole_body(self):
        c = self.client()
        c.buf = b'POST /api/x HTTP/1.1\r\nContent-Length: 9\r\n\r\na=1&b'
        self.assertIsNone(c.parse())
        c.buf += b'=2&cJUNK'
        r = c.parse()
        self.assertEqual(r.form, {'a': '1', 'b': '2', 'c': ''})     # body cut at Content-Length

    def test_incomplete_and_bad(self):
        c = self.client()
        c.buf = b'GET / HTTP/1.1\r\nHost'
        self.assertIsNone(c.parse())
        c.buf = b'x' * 20000
        self.assertEqual(c.parse().status, 413)
        c.buf = b'NONSENSE\r\n\r\n'
        self.assertEqual(c.parse().status, 400)
        c.buf = b'POST / HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n'
        self.assertEqual(c.parse().status, 413)
        c.buf = b'POST / HTTP/1.1\r\nContent-Length: abc\r\n\r\n'
        self.assertEqual(c.parse().status, 400)

    def test_response_bytes(self):
        raw = web_ui.build_response(web_ui.Response(200, 'hé', 'text/plain', {'X-A': 'b'}))
        head, _, body = raw.partition(b'\r\n\r\n')
        self.assertTrue(head.startswith(b'HTTP/1.1 200 OK'))
        self.assertIn(b'Content-Length: 3', head)                   # UTF-8 bytes, not characters
        self.assertIn(b'X-A: b', head)
        self.assertIn(b'frame-ancestors', head)
        self.assertEqual(body, 'hé'.encode())


class AppTestCase(unittest.TestCase):
    PASSWORD = 'pw-XYZ-123'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        self.init = os.path.join(t, 'init.d')
        self.svc = os.path.join(t, 'service')
        self.logs = os.path.join(t, 'log')
        self.sb = os.path.join(t, 'serialbattery')
        for d in (self.init, self.svc, self.logs, self.sb, os.path.join(t, 'bin')):
            os.makedirs(d)
        for name in ('fridge', 'mopeka_standard', 'esp32_hydro', 'web_ui'):
            os.makedirs(os.path.join(self.init, name))
        os.symlink(os.path.join(self.init, 'fridge'), os.path.join(self.svc, 'fridge'))
        os.symlink(os.path.join(self.init, 'web_ui'), os.path.join(self.svc, 'web_ui'))
        self.calls = os.path.join(t, 'calls.log')
        self.script('svc', 'echo "svc $@" >> %s' % self.calls)
        self.script('svstat', 'for a in "$@"; do echo "$a: up (pid 99) 12 seconds"; done')
        self.old_path = os.environ['PATH']
        os.environ['PATH'] = os.path.join(t, 'bin') + os.pathsep + self.old_path
        self.addCleanup(lambda: os.environ.__setitem__('PATH', self.old_path))
        self.config = os.path.join(t, 'config.ini')
        with open(self.config, 'w') as f:
            f.write(EXAMPLE.split('\n[web]')[0].replace('mac = 11:22:33:44:55:77', 'mac = ED:67:39:CB:69:6C')
                    + '\n[web]\nport = 8088\npassword = %s\n' % self.PASSWORD)
        with open(os.path.join(self.sb, 'config.default.ini'), 'w') as f:
            f.write(SB_DEFAULT)
        with open(os.path.join(self.sb, 'restart.sh'), 'w') as f:
            f.write('echo restarted > %s\n' % os.path.join(t, 'sb_restarted'))
        self.rc = os.path.join(t, 'rc.local')
        self.app = self.make_app(self.PASSWORD)

    def make_app(self, password):
        return web_ui.App({'driver_config': self.config, 'init_dir': self.init, 'service_dir': self.svc,
                           'log_dir': self.logs, 'rc_local': self.rc, 'password': password,
                           'serialbattery_dir': self.sb,
                           'state_dir': os.path.join(self.tmp, 'state'),
                           'html': os.path.join(HERE, '..', 'data', 'shared_drivers', 'web', 'index.html')})

    def script(self, name, body):
        path = os.path.join(self.tmp, 'bin', name)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n%s\n' % body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def call(self, method, path, form=None, headers=None, query='', app=None):
        h = dict(headers or {})
        from_form = '&'.join('%s=%s' % (k, v) for k, v in (form or {}).items())
        req = web_ui.Request(method, path, web_ui.parse_form(query), h, from_form.encode(), ('9.9.9.9', 1))
        return (app or self.app).handle(req)

    def post(self, path, form, auth=True, csrf=True, app=None):
        h = {}
        if csrf:
            h.update(CSRF)
        if auth:
            h.update(basic(self.PASSWORD))
        return self.call('POST', path, form, h, app=app)

    def get(self, path, auth=True, query='', app=None):
        return self.call('GET', path, None, basic(self.PASSWORD) if auth else {}, query, app)

    def svc_calls(self):
        try:
            with open(self.calls) as f:
                return f.read().split('\n')[:-1]
        except OSError:
            return []

    def config_text(self):
        with open(self.config) as f:
            return f.read()


class AccessTests(AppTestCase):
    def test_page_is_public(self):
        r = self.get('/', auth=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b'<title>', r.body)

    def test_unlock_flow(self):
        r = self.call('GET', '/unlock')
        self.assertEqual(r.status, 401)
        self.assertIn('Basic', r.headers['WWW-Authenticate'])
        self.assertEqual(self.call('GET', '/unlock', headers=basic(self.PASSWORD)).status, 302)
        self.assertEqual(self.call('GET', '/unlock', headers=basic('wrong')).status, 401)

    def test_locked_without_password(self):
        r = self.get('/api/driver_config', auth=False)
        self.assertEqual(r.status, 401)
        self.assertTrue(body_of(r)['locked'])
        self.assertEqual(self.get('/api/driver_config', auth=True).status, 200)
        self.assertEqual(self.get('/api/driver_config', app=self.app).status, 200)

    def test_post_needs_csrf_header_and_password(self):
        form = {'section': 'fridge', 'f.poll_interval': '300'}
        self.assertEqual(self.post('/api/driver_config', form, csrf=False).status, 403)
        r = self.post('/api/driver_config', form, auth=False)
        self.assertEqual(r.status, 401)
        r = self.call('POST', '/api/driver_config', form, dict(CSRF, **basic('wrong')))
        self.assertEqual(r.status, 401)
        self.assertEqual(self.post('/api/driver_config', form).status, 200)

    def test_wrong_passwords_are_throttled(self):
        for _ in range(8):
            self.call('POST', '/api/restart', {'target': 'fridge'}, dict(CSRF, **basic('nope')))
        r = self.call('POST', '/api/restart', {'target': 'fridge'}, dict(CSRF, **basic(self.PASSWORD)))
        self.assertEqual(r.status, 429)
        self.assertEqual(self.svc_calls(), [])

    def test_read_only_without_a_password(self):
        app = self.make_app('')
        r = self.get('/api/driver_config', auth=False, app=app)
        self.assertEqual(r.status, 200)                          # may look, not touch
        r = self.post('/api/driver_config', {'section': 'fridge'}, auth=False, app=app)
        self.assertEqual(r.status, 403)
        self.assertIn('read-only', body_of(r)['error'])
        self.assertFalse(web_ui.App.authed(app, web_ui.Request('GET', '/', {}, basic(''), b'', None)))

    def test_unknown_routes(self):
        self.assertEqual(self.get('/api/nothing').status, 404)
        self.assertEqual(self.get('/elsewhere', auth=False).status, 404)
        self.assertEqual(self.call('DELETE', '/api/state').status, 405)


class DriverConfigTests(AppTestCase):
    def test_get_lists_sections_and_hides_secrets(self):
        d = body_of(self.get('/api/driver_config'))
        by = dict((s['section'], s) for s in d['sections'])
        self.assertEqual(sorted(by), ['esp32_hydro', 'fridge', 'mopeka_standard', 'web'])
        self.assertEqual(by['fridge']['values']['mac'], 'ED:67:39:CB:69:6C')
        self.assertEqual(by['web']['values']['password'], '')
        self.assertTrue(by['web']['secret_set']['password'])
        self.assertNotIn(self.PASSWORD, self.get('/api/driver_config').body.decode())

    def test_save_changes_only_what_was_sent(self):
        before = self.config_text()
        r = self.post('/api/driver_config', {'section': 'fridge', 'f.poll_interval': '300',
                                             'f.zone1_name': 'Cold box', 'f.service': 'hack',
                                             'f.control': 'No'})
        self.assertEqual(r.status, 200, r.body)
        vals = ct.parse_ini(self.config_text())['fridge']
        self.assertEqual((vals['poll_interval'], vals['zone1_name'], vals['control']),
                         ('300', 'Cold box', 'no'))
        self.assertEqual(vals['service'], 'fridge')                # read-only field untouched
        self.assertEqual(ct.parse_ini(self.config_text())['mopeka_standard'],
                         ct.parse_ini(before)['mopeka_standard'])
        self.assertIn('# Seconds between status reads', self.config_text())   # comments survive
        self.assertTrue(os.path.exists(self.config + '.bak'))
        self.assertEqual(self.svc_calls(), [])                     # no restart asked

    def test_save_and_restart(self):
        r = self.post('/api/driver_config', {'section': 'fridge', 'restart': '1',
                                             'f.poll_interval': '120'})
        self.assertTrue(body_of(r)['restarted'])
        self.assertEqual(self.svc_calls(), ['svc -t ' + os.path.join(self.svc, 'fridge')])
        # a service that is not enabled: saved, nothing to restart
        r = self.post('/api/driver_config', {'section': 'mopeka_standard', 'restart': '1',
                                             'f.capacity_l': '24'})
        self.assertTrue(body_of(r)['ok'])
        self.assertFalse(body_of(r)['restarted'])

    def test_validation_errors_leave_the_file_alone(self):
        before = self.config_text()
        r = self.post('/api/driver_config', {'section': 'mopeka_standard', 'f.capacity_l': 'abc',
                                             'f.empty_mm': '300', 'f.full_mm': '100'})
        self.assertEqual(r.status, 400)
        self.assertEqual(sorted(body_of(r)['errors']), ['capacity_l', 'full_mm'])
        self.assertEqual(self.config_text(), before)

    def test_blank_removes_the_key_and_secret_blank_keeps_it(self):
        self.post('/api/driver_config', {'section': 'mopeka_standard', 'f.level_curve': '0:0, 1:100',
                                         'f.median_window': ''})
        vals = ct.parse_ini(self.config_text())['mopeka_standard']
        self.assertEqual(vals['level_curve'], '0:0, 1:100')
        self.assertNotIn('median_window', vals)
        self.post('/api/driver_config', {'section': 'web', 'f.password': '', 'f.port': '9000'})
        web = ct.parse_ini(self.config_text())['web']
        self.assertEqual((web['password'], web['port']), (self.PASSWORD, '9000'))
        self.post('/api/driver_config', {'section': 'web', 'f.password': 'new pass'})
        self.assertEqual(ct.parse_ini(self.config_text())['web']['password'], 'new pass')

    def test_unknown_section(self):
        self.assertEqual(self.post('/api/driver_config', {'section': 'nope'}).status, 400)

    def test_saving_web_restarts_this_page_later(self):
        r = self.post('/api/driver_config', {'section': 'web', 'restart': '1', 'f.port': '9001'})
        self.assertTrue(body_of(r)['restarted'])
        self.assertEqual(self.svc_calls(), [])                     # after the answer was sent


class BatteryDebugTests(AppTestCase):
    def test_sampling_and_api(self):
        import test_battery_debug as tb
        name = 'com.victronenergy.battery.ttyUSB0'
        self.app.battery_names = lambda: [name]
        self.app.dbus_dump = lambda n: tb.pack(n=4, spread=0.01)
        self.assertTrue(self.app.sample_batteries())
        self.assertTrue(self.app.sample_batteries())
        self.assertEqual(self.get('/api/battery', auth=False).status, 401)       # locked like the rest
        d = body_of(self.get('/api/battery', query='window=900'))
        self.assertEqual((d['service'], d['window']), (name, 900))
        self.assertEqual(d['batteries'], [{'service': name, 'name': 'JK BMS'}])
        self.assertEqual(len(d['history']['cells']), 4)
        self.assertEqual(d['info']['values']['/Alarms/LowVoltage'], 0)
        self.assertEqual(body_of(self.get('/api/battery', query='window=abc'))['window'], 3600)
        self.assertEqual(body_of(self.get('/api/battery', query='service=nope'))['service'], name)

    def test_no_battery(self):
        self.app.battery_names = lambda: []
        d = body_of(self.get('/api/battery'))
        self.assertEqual((d['ok'], d['batteries'], d['service']), (True, [], ''))


class StarlinkApiTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.started = []
        self.saved = web_ui.starlink_hub.run_async
        web_ui.starlink_hub.run_async = lambda argv, cb, input_bytes=None, timeout=10: self.started.append((argv, cb))
        self.addCleanup(lambda: setattr(web_ui.starlink_hub, 'run_async', self.saved))

    def test_views_and_locking(self):
        for path in ('/api/starlink_dashboard', '/api/starlink_energy', '/api/starlink_log', '/api/update_check', '/api/debug_result'):
            self.assertEqual(self.get(path, auth=False).status, 401, path)
        d = body_of(self.get('/api/starlink_dashboard'))
        self.assertEqual((d['ok'], d['last'], d['running']['running']), (True, None, False))
        self.assertEqual(body_of(self.get('/api/starlink_energy'))['energy']['currency'], 'EUR')
        self.assertEqual(body_of(self.get('/api/starlink_log', query='window=86400'))['stats']['count'], 0)

    def test_speed_test_button(self):
        r = self.post('/api/starlink_test', {})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.post('/api/starlink_test', {}).status, 409)           # one at a time
        self.assertEqual(self.post('/api/starlink_test', {}, auth=False).status, 401)

    def test_debug_tools_only_take_safe_targets(self):
        for tool, target in (('ping', '-c 1; rm -rf /'), ('ping', ''), ('ping', 'a b'), ('dns', '$(id)'), ('http', 'file:///etc/passwd'),
                             ('http', 'https://x.example/ -o /tmp/x'), ('shell', 'ls'), ('ping', '-oProxyCommand')):
            self.assertEqual(self.post('/api/debug_start', {'tool': tool, 'target': target}).status, 400, (tool, target))
        r = self.post('/api/debug_start', {'tool': 'ping', 'target': '192.168.100.1'})
        job = body_of(r)['job']
        argv, cb = self.started[-1]
        self.assertEqual(argv[:1] + argv[-1:], ['ping', '192.168.100.1'])
        self.assertFalse(body_of(self.get('/api/debug_result', query='job=' + job))['done'])
        cb(0, b'4 packets transmitted, 4 packets received, 0% packet loss')
        res = body_of(self.get('/api/debug_result', query='job=' + job))
        self.assertTrue(res['done'] and 'packet loss' in res['out'])
        self.assertEqual(self.get('/api/debug_result', query='job=nope').status, 404)
        http = self.post('/api/debug_start', {'tool': 'http', 'target': 'https://example.com/path?a=1'})
        self.assertEqual(http.status, 200)
        self.assertEqual(self.started[-1][0][-1], 'https://example.com/path?a=1')

    def test_update_check(self):
        d = body_of(self.get('/api/update_check'))
        self.assertTrue(d['checking'] and d['latest'] is None)
        argv, cb = self.started[-1]
        self.assertIn('api.github.com/repos/mich-crypto/Venusosmods/commits/master', argv[-1])
        cb(0, b'{"sha": "' + b'a' * 40 + b'", "commit": {"message": "Fix things\\n\\nbody", "author": {"date": "2026-10-29T12:00:00Z"}}}')
        d = body_of(self.get('/api/update_check'))
        self.assertEqual((d['latest'], d['message'], d['date'][:10], d['checking']), ('a' * 40, 'Fix things', '2026-10-29', False))
        self.assertTrue(d['available'])                                  # nothing installed yet = unknown version
        self.assertEqual(body_of(self.get('/api/update_log'))['running'], False)

    def test_saving_says_what_was_written_and_never_shows_secrets(self):
        r = body_of(self.post('/api/driver_config', {'section': 'starlink', 'f.cap_gb': '100'}))
        self.assertTrue(r['ok'] and r['restarted'])                                  # read once at start-up: a save restarts the page
        self.assertEqual(r['saved'], {'cap_gb': '100.0'} if r['saved']['cap_gb'] == '100.0' else {'cap_gb': '100'})
        self.assertIn('cap_gb = 100', self.config_text())
        r = body_of(self.post('/api/driver_config', {'section': 'telegram', 'f.token': 'SECRET-TOKEN'}))
        self.assertEqual(r['saved'], {'token': '(hidden)'})
        self.assertNotIn('SECRET-TOKEN', str(r))
        self.assertTrue(os.path.exists(self.config + '.bak'))

    def test_optional_sections_can_be_created(self):
        d = body_of(self.get('/api/driver_config'))
        self.assertEqual(sorted(s['kind'] for s in d['optional']), ['starlink', 'telegram'])
        r = self.post('/api/driver_config', {'section': 'telegram', 'f.enabled': 'yes', 'f.token': 'SECRET-TOKEN', 'f.chat_id': '42'})
        self.assertEqual(r.status, 200, r.body)
        text = self.config_text()
        self.assertIn('[telegram]', text)
        self.assertIn('token = SECRET-TOKEN', text)
        shown = self.get('/api/driver_config').body.decode()
        self.assertNotIn('SECRET-TOKEN', shown)                                          # never sent back to the browser
        self.assertEqual(self.post('/api/driver_config', {'section': 'nosuch', 'f.x': '1'}).status, 400)


SWITCH = 'com.victronenergy.switch.fridge_controls'


def switch_values():
    v = {'/Mgmt/ProcessName': 'fridge_venus.py', '/Connected': 1,
         '/SwitchableOutput/power/State': 1, '/SwitchableOutput/eco/State': 0}
    for ch, name, target, cur, lo, hi in (('setpoint', 'Set point 1: Fridge', 4.0, 5.5, 0.0, 8.0),
                                          ('setpoint2', 'Set point 2: Freezer', -18.0, -16.5, -20.0, -12.0)):
        base = '/SwitchableOutput/%s/' % ch
        v.update({base + 'Name': name, base + 'Settings/CustomName': name, base + 'Dimming': target, base + 'Measurement': cur,
                  base + 'Settings/DimmingMin': lo, base + 'Settings/DimmingMax': hi, base + 'Settings/StepSize': 1})
    return v


class VenusFeatureTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.app.bus_calls = self.sent

        class Bus(object):
            def call_blocking(inner, name, path, iface, method, sig, args, timeout=None):
                self.sent.append((name, path, method, sig, args[0], float(args[0])))
        self._bus = Bus()
        self.app._bus = self._bus

    # --- fridge
    def test_fridge_not_available_yet(self):
        self.app.fridge_switch = lambda: (None, {})
        self.assertEqual(body_of(self.get('/api/fridge')), {'ok': True, 'available': False})
        self.assertEqual(self.post('/api/fridge_set', {'output': 'power', 'value': '0'}).status, 409)

    def test_fridge_state_and_controls(self):
        self.app.fridge_switch = lambda: (SWITCH, switch_values())
        d = body_of(self.get('/api/fridge'))
        self.assertEqual((d['available'], d['power'], d['eco']), (True, 1, 0))
        self.assertEqual([(z['output'], z['name'], z['min'], z['max'], z['target']) for z in d['zones']],
                         [('setpoint', 'Set point 1: Fridge', 0.0, 8.0, 4.0), ('setpoint2', 'Set point 2: Freezer', -20.0, -12.0, -18.0)])
        self.assertEqual(self.post('/api/fridge_set', {'output': 'power', 'value': '0'}).status, 200)
        self.assertEqual(self.post('/api/fridge_set', {'output': 'setpoint2', 'value': '-15'}).status, 200)
        self.assertEqual([(c[1], c[5]) for c in self.sent], [('/SwitchableOutput/power/State', 0.0), ('/SwitchableOutput/setpoint2/Dimming', -15.0)])
        self.assertTrue(all(c[0] == SWITCH and c[2] == 'SetValue' and c[3] == 'v' for c in self.sent))

    def test_fridge_controls_refuse_bad_input_and_strangers(self):
        self.app.fridge_switch = lambda: (SWITCH, switch_values())
        for form in ({'output': 'setpoint', 'value': '12'}, {'output': 'setpoint2', 'value': '-5'}, {'output': 'setpoint', 'value': 'warm'},
                     {'output': 'power', 'value': '2'}, {'output': 'format_c', 'value': '1'}, {'output': 'eco', 'value': ''}):
            self.assertEqual(self.post('/api/fridge_set', form).status, 400, form)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.post('/api/fridge_set', {'output': 'power', 'value': '1'}, auth=False).status, 401)
        self.assertEqual(self.post('/api/fridge_set', {'output': 'power', 'value': '1'}, csrf=False).status, 403)
        self.assertEqual(self.get('/api/fridge', auth=False).status, 401)

    # --- Starlink as a Venus OS device
    def test_starlink_in_venus_os(self):
        import test_starlink as tsl
        hub = self.app.starlink
        hub.time, hub.status = hub.clock(), starlink_status(drop_rate=0.0, latency_ms=30.0)
        self.assertTrue(self.app.publish_starlink())
        svc = self.app.starlink_dev.service
        self.assertEqual(svc.name, 'com.victronenergy.tank.starlink')
        self.assertEqual((svc['/Level'], svc['/Starlink/Label'], svc['/Starlink/State']), (96, 'Excellent', 'Connected'))
        self.assertEqual((svc['/Starlink/DownMbps'], svc['/Starlink/LatencyMs'], svc['/Connected']), (184.5, 30.0, 1))
        hub.status = starlink_status(state='Outage: obstructed')
        self.app.publish_starlink()
        self.assertEqual((svc['/Level'], svc['/Starlink/Label']), (0, 'Offline'))
        self.assertIn('/Starlink/DropPercent', svc.paths_at_register)                # paths exist before registering

    def test_starlink_in_venus_os_can_be_switched_off(self):
        app = web_ui.App({'driver_config': self.config, 'init_dir': self.init, 'service_dir': self.svc, 'log_dir': self.logs,
                          'rc_local': self.rc, 'password': self.PASSWORD, 'state_dir': os.path.join(self.tmp, 's2'),
                          'starlink': {'venus_service': 'no'}})
        app.starlink.status, app.starlink.time = starlink_status(), app.starlink.clock()
        app.publish_starlink()
        self.assertIsNone(app.starlink_dev)

    # --- gas sensor battery
    def test_mopeka_battery_is_recorded_and_warned_about(self):
        app = self.app
        app.starlink.tg.update(enabled=True, token='T', chat_id='1')
        sent = []
        app.starlink.flush = lambda: sent.append(list(app.starlink.queue))
        app.mopeka_values = lambda: {'/BatteryVoltage': 2.45, '/Mopeka/BatteryPercent': 55}
        app.record_mopeka()
        self.assertEqual(sent, [])
        app.mopeka_values = lambda: {'/BatteryVoltage': 2.12, '/Mopeka/BatteryPercent': 12}
        app.record_mopeka()
        app.record_mopeka()                                                           # once per half day, not every look
        self.assertEqual(len(sent), 1)
        self.assertIn('battery is low: 12 %', sent[0][0])
        d = body_of(self.get('/api/mopeka_battery'))
        self.assertEqual((d['available'], d['percent'], d['low_at'], len(d['series'])), (True, 12, 20, 3))
        self.assertEqual(d['series'][-1][1:], [2.12, 12])
        app.mopeka_values = lambda: None
        self.assertEqual(body_of(self.get('/api/mopeka_battery'))['available'], False)


def starlink_status(**kw):
    import starlink as sl
    import test_starlink as tsl
    st = sl.decode_reply(sl.frame(tsl.status_message(latency=kw.pop('latency_ms', 31.5))))
    st.update(kw)
    return st


class SerialBatteryTests(AppTestCase):
    def user_text(self):
        try:
            with open(os.path.join(self.sb, 'config.ini')) as f:
                return f.read()
        except OSError:
            return None

    def test_not_installed(self):
        app = web_ui.App({'driver_config': self.config, 'init_dir': self.init, 'service_dir': self.svc,
                          'log_dir': self.logs, 'rc_local': self.rc, 'password': self.PASSWORD,
                          'serialbattery_dir': os.path.join(self.tmp, 'missing'), 'state_dir': os.path.join(self.tmp, 'state')})
        self.assertFalse(body_of(self.get('/api/serialbattery', app=app))['installed'])
        self.assertEqual(self.post('/api/serialbattery', {'s.LOGGING': 'DEBUG'}, app=app).status, 400)

    def test_view(self):
        d = body_of(self.get('/api/serialbattery'))
        self.assertTrue(d['installed'])
        groups = d['view']['groups']
        self.assertEqual(groups[0]['title'], 'Set logging level')
        self.assertIn('BLUETOOTH_BMS', d['view']['essentials'])

    def test_save_writes_only_changes_and_restarts(self):
        r = self.post('/api/serialbattery', {'s.MAX_BATTERY_CHARGE_CURRENT': '40', 's.LOGGING': 'debug',
                                             's.MAX_CELL_VOLTAGE': '3.45', 'restart': '1'})
        d = body_of(r)
        self.assertTrue(d['ok'] and d['restarted'], d)
        self.assertEqual(ct.parse_ini(self.user_text())['DEFAULT'],
                         {'max_battery_charge_current': '40', 'logging': 'DEBUG'})
        for _ in range(40):                                        # restart.sh runs in the background
            if os.path.exists(os.path.join(self.tmp, 'sb_restarted')):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'sb_restarted')))
        r = self.post('/api/serialbattery', {'r.LOGGING': '1'})
        self.assertEqual(ct.parse_ini(self.user_text())['DEFAULT'], {'max_battery_charge_current': '40'})

    def test_bluetooth_change_needs_a_reboot_not_a_restart(self):
        os.remove(os.path.join(self.tmp, 'sb_restarted')) if os.path.exists(os.path.join(self.tmp, 'sb_restarted')) else None
        r = self.post('/api/serialbattery', {'s.BLUETOOTH_BMS': 'Jkbms_Ble C8:47:8C:ED:1B:FB',
                                             'restart': '1'})
        d = body_of(r)
        self.assertTrue(d['reboot_needed'])
        self.assertFalse(d['restarted'])
        self.assertIn('reboot', d['message'].lower())
        time.sleep(0.3)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'sb_restarted')))

    def test_errors_and_secrets(self):
        before = self.user_text()
        r = self.post('/api/serialbattery', {'s.MAX_BATTERY_CHARGE_CURRENT': 'lots'})
        self.assertEqual(r.status, 400)
        self.assertIn('MAX_BATTERY_CHARGE_CURRENT', body_of(r)['errors'])
        self.assertEqual(self.user_text(), before)
        r = self.post('/api/serialbattery', {'s.NOT_A_KEY': '1'})
        self.assertEqual(r.status, 400)
        self.assertEqual(body_of(self.post('/api/serialbattery', {}))['changed'], [])


class RawFileTests(AppTestCase):
    def test_read_and_write_driver_config(self):
        d = body_of(self.get('/api/raw', query='file=driver'))
        self.assertIn('[fridge]', d['text'])
        new = d['text'].replace('poll_interval = 60', 'poll_interval = 120')
        r = self.post('/api/raw', {'file': 'driver', 'text': new})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(ct.parse_ini(self.config_text())['fridge']['poll_interval'], '120')

    def test_bad_files_are_refused(self):
        before = self.config_text()
        r = self.post('/api/raw', {'file': 'driver', 'text': 'no section header\n'})
        self.assertEqual(r.status, 400)
        r = self.post('/api/raw', {'file': 'driver', 'text': '[fridge]\npoll_interval = soon\n'})
        self.assertEqual(r.status, 400)
        self.assertIn('fridge.poll_interval', body_of(r)['errors'])
        r = self.post('/api/raw', {'file': 'serialbattery', 'text': '[DEFAULT]\nMAX_CELL_VOLTAGE = high\n'})
        self.assertEqual(r.status, 400)
        r = self.post('/api/raw', {'file': 'passwd', 'text': 'x'})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.config_text(), before)

    def test_write_serialbattery_raw(self):
        r = self.post('/api/raw', {'file': 'serialbattery', 'text': '[DEFAULT]\nMAX_CELL_VOLTAGE = 3.5\n'})
        self.assertEqual(r.status, 200, r.body)
        with open(os.path.join(self.sb, 'config.ini')) as f:
            self.assertEqual(f.read(), '[DEFAULT]\nMAX_CELL_VOLTAGE = 3.5\n')


class ServiceTests(AppTestCase):
    def test_states(self):
        states = dict((s['name'], s) for s in self.app.service_states())
        self.assertEqual(states['fridge'], {'name': 'fridge', 'enabled': True, 'state': 'up',
                                            'seconds': 12, 'pid': 99})
        self.assertEqual(states['mopeka_standard']['state'], 'off')
        self.assertFalse(states['mopeka_standard']['enabled'])

    def test_restart(self):
        self.assertEqual(self.post('/api/restart', {'target': 'fridge'}).status, 200)
        self.assertEqual(self.svc_calls(), ['svc -t ' + os.path.join(self.svc, 'fridge')])
        self.assertEqual(self.post('/api/restart', {'target': 'esp32_hydro'}).status, 400)   # not enabled
        self.assertEqual(self.post('/api/restart', {'target': '../etc'}).status, 400)
        r = self.post('/api/restart', {'target': 'serialbattery'})
        self.assertEqual(r.status, 200, r.body)

    def test_enable_and_disable(self):
        r = self.post('/api/service', {'name': 'esp32_hydro', 'action': 'enable'})
        self.assertEqual(r.status, 200, r.body)
        link = os.path.join(self.svc, 'esp32_hydro')
        self.assertEqual(os.readlink(link), os.path.join(self.init, 'esp32_hydro'))
        with open(self.rc) as f:
            rc = f.read()
        self.assertEqual(rc.count('ln -s %s %s' % (os.path.join(self.init, 'esp32_hydro'), link)), 1)
        self.post('/api/service', {'name': 'esp32_hydro', 'action': 'enable'})    # idempotent
        with open(self.rc) as f:
            self.assertEqual(f.read(), rc)
        self.assertTrue(os.access(self.rc, os.X_OK))
        r = self.post('/api/service', {'name': 'esp32_hydro', 'action': 'disable'})
        self.assertEqual(r.status, 200)
        self.assertFalse(os.path.lexists(link))
        with open(self.rc) as f:
            self.assertNotIn('esp32_hydro', f.read())
        self.assertIn('svc -d %s %s/log' % (link, link), self.svc_calls())

    def test_cannot_switch_off_the_page_itself_or_unknown(self):
        self.assertEqual(self.post('/api/service', {'name': 'web_ui', 'action': 'disable'}).status, 400)
        self.assertTrue(os.path.lexists(os.path.join(self.svc, 'web_ui')))
        self.assertEqual(self.post('/api/service', {'name': 'sshd', 'action': 'disable'}).status, 400)
        self.assertEqual(self.post('/api/service', {'name': 'fridge', 'action': 'explode'}).status, 400)

    def test_logs(self):
        os.makedirs(os.path.join(self.logs, 'fridge'))
        with open(os.path.join(self.logs, 'fridge', 'current'), 'w') as f:
            f.write('@40000000%08x%08x INFO hello\n' % (1760932036 + 10, 0))
            f.write('second line\n')
        d = body_of(self.get('/api/log', query='service=fridge'))
        self.assertEqual(len(d['lines']), 2)
        self.assertTrue(d['lines'][0].endswith('INFO hello'))
        self.assertEqual(body_of(self.get('/api/log', query='service=esp32_hydro'))['lines'], [])
        self.assertEqual(self.get('/api/log', query='service=..%2F..%2Fetc%2Fpasswd').status, 400)
        self.assertEqual(self.get('/api/log', query='service=nope').status, 400)

    def test_pair_reset_and_reboot(self):
        sent = []
        self.app.settings_set = lambda path, value: sent.append((path, value))
        r = self.post('/api/pair_reset', {'section': 'mopeka_standard'})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(sent, [('/Settings/Devices/mopeka_standard/Mac', '')])
        self.assertEqual(self.post('/api/pair_reset', {'section': 'fridge'}).status, 400)

        class Bus(object):
            def call_blocking(self, *args, **kw):
                sent.append(args)
        self.app._bus = Bus()
        self.assertEqual(self.post('/api/reboot', {}).status, 200)
        self.assertEqual(sent[-1][:4], ('com.victronenergy.platform', '/Device/Reboot',
                                        'com.victronenergy.BusItem', 'SetValue'))

    def test_state(self):
        self.app.devices = lambda: ([{'service': 'x', 'kind': 'tank', 'values': {'/Level': 55.5}}], [])
        d = body_of(self.get('/api/state', auth=False))
        self.assertEqual((d['password_set'], d['authed'], d['serialbattery']), (True, False, True))
        self.assertEqual(d['devices'][0]['values']['/Level'], 55.5)
        self.assertEqual(sorted(s['name'] for s in d['services']),
                         ['esp32_hydro', 'fridge', 'mopeka_standard', 'web_ui'])
        self.assertTrue(body_of(self.get('/api/state'))['authed'])


if __name__ == '__main__':
    unittest.main()
