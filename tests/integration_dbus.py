#!/usr/bin/env python3
"""
End-to-end test on a private D-Bus, without Venus OS or Bluetooth hardware:
a fake BlueZ sends advertisements and plays a dual-zone fridge over GATT, a
fake localsettings answers AddSettings, and the real drivers (with the real
velib_python) publish their services.

Needs dbus-python + PyGObject, dbus-run-session and a velib_python checkout:
  git clone https://github.com/victronenergy/velib_python /tmp/velib_python
  VELIB=/tmp/velib_python dbus-run-session -- python3 tests/integration_dbus.py
"""

import http.client
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVERS = os.path.join(HERE, '..', 'data', 'shared_drivers')
sys.path.insert(0, HERE)
sys.path.insert(0, DRIVERS)

import alpicool_protocol as fridge  # noqa: E402
import esp32_hydro_protocol as proto  # noqa: E402
from packets import mopeka_packet  # noqa: E402

PROPS = 'org.freedesktop.DBus.Properties'
DEV = 'org.bluez.Device1'
CHAR = 'org.bluez.GattCharacteristic1'
MOPEKA_MAC = 'AA:BB:CC:DD:EE:01'
ESP_MAC = 'AA:BB:CC:DD:EE:02'
FRIDGE_MAC = 'AA:BB:CC:DD:EE:03'


def dev_path(mac):
    return '/org/bluez/hci0/dev_' + mac.replace(':', '_')


class Device(dbus.service.Object):
    @dbus.service.signal(PROPS, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


class Adapter(dbus.service.Object):
    """Discovery sessions are per D-Bus client, like in BlueZ."""
    instance = None

    def __init__(self, conn, path):
        super().__init__(conn, path)
        self.sessions = set()
        self.stops = 0
        Adapter.instance = self

    @property
    def discovering(self):
        return bool(self.sessions)

    @dbus.service.method(PROPS, in_signature='ss', out_signature='v')
    def Get(self, iface, prop):
        return {'Powered': True, 'Discovering': self.discovering}[prop]

    @dbus.service.method('org.bluez.Adapter1', in_signature='a{sv}')
    def SetDiscoveryFilter(self, f):
        assert f['Transport'] == 'le'

    @dbus.service.method('org.bluez.Adapter1', sender_keyword='sender')
    def StartDiscovery(self, sender=None):
        if sender in self.sessions:
            raise dbus.exceptions.DBusException('Operation already in progress',
                                                name='org.bluez.Error.InProgress')
        self.sessions.add(sender)

    @dbus.service.method('org.bluez.Adapter1', sender_keyword='sender')
    def StopDiscovery(self, sender=None):
        if sender not in self.sessions:
            raise dbus.exceptions.DBusException('No discovery started',
                                                name='org.bluez.Error.Failed')
        self.sessions.discard(sender)
        self.stops += 1


def dual_zone_state():
    data = bytearray(30)
    data[1], data[2], data[4] = 1, 1, 3          # on, eco, left target 3 C
    data[5], data[6] = 8, 0                      # fridge range 0..8
    data[14] = 4                                 # fridge 4 C
    data[15], data[16], data[17] = 0x7F, 12, 8   # 12.8 V
    data[18], data[26] = 0xEE, 0xEF              # freezer target -18, current -17
    data[19], data[20] = 0xF4, 0xEC              # freezer range -20..-12
    data[28] = 3
    return data


def dual_zone_status():
    return fridge.build_frame(fridge.CMD_QUERY, bytes(dual_zone_state()))


class FridgeChar(Device):
    def __init__(self, conn, path, fridge_dev, uuid):
        super().__init__(conn, path)
        self.fridge, self.uuid = fridge_dev, uuid

    @dbus.service.method(CHAR)
    def StartNotify(self):
        self.fridge.notifying = True

    @dbus.service.method(CHAR)
    def StopNotify(self):
        self.fridge.notifying = False

    @dbus.service.method(CHAR, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        assert len(value) <= 20 and options['type'] == 'request'
        f = self.fridge
        for cmd, data in f.assembler.feed(bytes(bytearray(value))):
            f.commands.append((cmd, bytes(data)))
            if cmd == fridge.CMD_SET_LEFT:
                f.state[4] = data[0]
            elif cmd == fridge.CMD_SET_RIGHT:
                f.state[18] = data[0]
            elif cmd == fridge.CMD_SET_OTHERS:
                f.state[0:4] = data[0:4]
            elif cmd == fridge.CMD_QUERY and f.notifying:
                # answer in two notifications, like the real controller
                for part in fridge.chunks(fridge.build_frame(fridge.CMD_QUERY, bytes(f.state))):
                    GLib.idle_add(f.notify_char.PropertiesChanged, CHAR,
                                  {'Value': dbus.Array(part, signature='y')}, [])


class Fridge(Device):
    def __init__(self, conn):
        path = dev_path(FRIDGE_MAC)
        super().__init__(conn, path)
        self.connected = self.notifying = False
        self.received = b''
        self.connects = self.disconnects = self.busy_rejects = 0
        self.assembler = fridge.FrameAssembler()
        self.commands = []
        self.state = dual_zone_state()
        self.write_char = FridgeChar(conn, path + '/service0010/char0011', self, fridge.WRITE_UUID)
        self.notify_char = FridgeChar(conn, path + '/service0010/char0013', self, fridge.NOTIFY_UUID)

    @dbus.service.method(PROPS, in_signature='ss', out_signature='v')
    def Get(self, iface, prop):
        return {'Connected': self.connected, 'ServicesResolved': self.connected}[prop]

    @dbus.service.method(DEV)
    def Connect(self):
        self.connects += 1
        if Adapter.instance.discovering:
            # what the Raspberry Pi's controller does while any process scans
            self.busy_rejects += 1
            raise dbus.exceptions.DBusException('le-connection-abort-by-local',
                                                name='org.bluez.Error.Failed')
        self.connected = True
        # BlueZ signals ServicesResolved after the Connect reply
        GLib.idle_add(self.PropertiesChanged, DEV, {'Connected': True}, [])
        GLib.timeout_add(50, self.PropertiesChanged, DEV, {'ServicesResolved': True}, [])

    @dbus.service.method(DEV)
    def Disconnect(self):
        self.disconnects += 1
        self.connected = self.notifying = False
        GLib.idle_add(self.PropertiesChanged, DEV,
                      {'Connected': False, 'ServicesResolved': False}, [])

    def objects(self):
        return {dbus.ObjectPath(self.write_char._object_path): {CHAR: {'UUID': fridge.WRITE_UUID}},
                dbus.ObjectPath(self.notify_char._object_path): {CHAR: {'UUID': fridge.NOTIFY_UUID}},
                dbus.ObjectPath(dev_path(FRIDGE_MAC)): {DEV: {'Address': FRIDGE_MAC,
                                                              'Name': 'WT-0001',
                                                              'RSSI': dbus.Int16(-60)}}}


class Root(dbus.service.Object):
    """org.bluez ObjectManager and com.victronenergy.settings AddSettings on '/'."""

    def __init__(self, conn, fridge_dev):
        super().__init__(conn, '/')
        self.fridge = fridge_dev

    @dbus.service.method('org.freedesktop.DBus.ObjectManager', out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        objs = {dbus.ObjectPath('/org/bluez/hci0'): {'org.bluez.Adapter1': {'Powered': True}}}
        objs.update(self.fridge.objects())
        return objs

    @dbus.service.method('com.victronenergy.Settings', in_signature='aa{sv}', out_signature='aa{sv}')
    def AddSettings(self, items):
        return [{'path': i['path'], 'error': 0, 'value': i['default']} for i in items]


class SettingsTree(dbus.service.FallbackObject):
    """Answers SetValue on any /Settings/... path, like localsettings."""
    stored = {}

    @dbus.service.method('com.victronenergy.BusItem', in_signature='v', out_signature='i',
                         rel_path_keyword='path')
    def SetValue(self, value, path=None):
        SettingsTree.stored['/Settings' + path] = value
        return 0


class Platform(dbus.service.Object):
    """com.victronenergy.platform /Device/Reboot, which the settings page calls."""
    rebooted = None

    @dbus.service.method('com.victronenergy.BusItem', in_signature='v', out_signature='i')
    def SetValue(self, value):
        Platform.rebooted = int(value)
        return 0


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def advertise(devices):
    devices['mopeka'].PropertiesChanged(DEV, {
        'RSSI': dbus.Int16(-71),
        'ManufacturerData': dbus.Dictionary(
            # sync button "pressed" on the third advert -> the driver pairs
            {dbus.UInt16(0x000D): dbus.Array(mopeka_packet([(19, 0), (9, 12)] + [(0, 0)] * 10,
                                                           sync=advertise.seq == 2),
                                             signature='y')}, signature='qv'),
    }, [])
    advertise.seq += 1
    payload = proto.build(1, 50.0, 37.5, 75.0, 210, temperature=12.5, sequence=advertise.seq)
    devices['esp'].PropertiesChanged(DEV, {
        'RSSI': dbus.Int16(-55),
        'ManufacturerData': dbus.Dictionary(
            {dbus.UInt16(0xFFFF): dbus.Array(payload, signature='y')}, signature='qv'),
    }, [])
    return True


advertise.seq = 0


def get(bus, service, path):
    return bus.call_blocking(service, path, 'com.victronenergy.BusItem', 'GetValue', '', [])


def check_web_results(web):
    res = web['results']
    assert 'exception' not in res, res
    st, page = res['page']
    assert st == 200 and b'<title>Venus settings' in page, (st, page[:80])
    st, state = res['state']
    assert st == 200 and state['password_set'] and not state['authed'] and state['serialbattery'], state
    services = set(d['service'] for d in state['devices'])
    assert {'com.victronenergy.tank.mopeka_standard', 'com.victronenergy.tank.esp32_hydro',
            'com.victronenergy.temperature.esp32_hydro_temp',
            'com.victronenergy.temperature.fridge_zone1'} <= services, services
    mop = [d for d in state['devices'] if d['service'].endswith('mopeka_standard')][0]
    assert mop['values']['/Mopeka/DistanceMm'] and mop['values']['/Capacity'] == 0.027, mop['values']
    zone = [d for d in state['devices'] if d['service'].endswith('fridge_zone1')][0]
    assert zone['values']['/Fridge/SupplyVoltage'] == 12.8
    names = dict((s['name'], s) for s in state['services'])
    assert names['fridge']['state'] == 'up' and names['fridge']['seconds'] == 12, names
    st, locked = res['locked']
    assert st == 401 and locked['locked'], (st, locked)
    st, cfg = res['config']
    assert st == 200 and sorted(s['section'] for s in cfg['sections']) == [
        'esp32_hydro', 'fridge', 'mopeka_standard', 'web'], cfg
    assert 'pw-XYZ-123' not in str(cfg), 'password leaked'
    assert res['no_csrf'][0] == 403, res['no_csrf']
    st, saved = res['save']
    assert st == 200 and saved['restarted'], res['save']
    assert res['bad'][0] == 400 and 'capacity_l' in res['bad'][1]['errors'], res['bad']
    with open(os.path.join(web['dir'], 'config.ini')) as f:
        text = f.read()
    assert 'capacity_l = 24' in text and '[fridge]' in text, text
    with open(web['calls']) as f:
        calls = f.read()
    assert 'svc -t %s' % os.path.join(web['dir'], 'service', 'mopeka_standard') in calls, calls
    assert res['sb_get'][0] == 200 and res['sb_get'][1]['installed'], res['sb_get']
    assert res['sb_save'][0] == 200 and res['sb_save'][1]['restarted'], res['sb_save']
    with open(os.path.join(web['dir'], 'sb', 'config.ini')) as f:
        assert f.read() == '[DEFAULT]\nMAX_BATTERY_CHARGE_CURRENT = 40\n'
    for _ in range(40):
        if os.path.exists(os.path.join(web['dir'], 'sb_restarted')):
            break
        time.sleep(0.05)
    assert os.path.exists(os.path.join(web['dir'], 'sb_restarted')), 'restart.sh did not run'
    st, log = res['log']
    assert st == 200 and log['lines'][0].endswith('INFO fridge poll ok'), res['log']
    assert res['pair'][0] == 200 and SettingsTree.stored['/Settings/Devices/mopeka_standard/Mac'] == '', res['pair']
    assert res['reboot'][0] == 200 and Platform.rebooted == 1, res['reboot']


def main():
    velib = os.environ.get('VELIB')
    if not velib:
        sys.exit('set VELIB=/path/to/velib_python')
    os.environ['DBUS_SYSTEM_BUS_ADDRESS'] = os.environ['DBUS_SESSION_BUS_ADDRESS']

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    names = [dbus.service.BusName(n, bus) for n in ('org.bluez', 'com.victronenergy.settings',
                                                    'com.victronenergy.platform')]
    Platform(bus, '/Device/Reboot')
    fridge_dev = Fridge(bus)
    Root(bus, fridge_dev)
    SettingsTree(bus, '/Settings')
    adapter = Adapter(bus, '/org/bluez/hci0')
    devices = {'mopeka': Device(bus, dev_path(MOPEKA_MAC)), 'esp': Device(bus, dev_path(ESP_MAC))}

    W = tempfile.mkdtemp(prefix='venus_web_', dir=os.environ.get('TMPDIR', '/tmp'))
    web = {'dir': W, 'port': int(os.environ.get('WEB_PORT') or free_port()), 'password': 'pw-XYZ-123'}
    cfg = os.path.join(W, 'config.ini')
    with open(cfg, 'w') as f:
        f.write('[mopeka_standard]\ncapacity_l = 27\n'
                '[esp32_hydro]\ndevice_id = 1\ntemperature_service = yes\n'
                '[fridge]\nmac = %s\npoll_interval = 10\n'
                '[web]\npassword = %s\nopen_firewall = no\n' % (FRIDGE_MAC, web['password']))
    # folders the page looks at: services, logs, dbus-serialbattery, fake svc/svstat
    shutil.copytree(os.path.join(DRIVERS, '..', 'etc', 'init.d'), os.path.join(W, 'init.d'))
    for d in ('service', 'log/fridge', 'sb', 'bin'):
        os.makedirs(os.path.join(W, d))
    for name in ('fridge', 'mopeka_standard', 'esp32_hydro', 'web_ui'):
        os.symlink(os.path.join(W, 'init.d', name), os.path.join(W, 'service', name))
    web['calls'] = os.path.join(W, 'calls.log')
    for name, body in (('svc', 'echo "svc $@" >> %s' % web['calls']),
                       ('svstat', 'for a in "$@"; do echo "$a: up (pid 99) 12 seconds"; done')):
        path = os.path.join(W, 'bin', name)
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n%s\n' % body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    shutil.copy(os.path.join(HERE, 'data', 'serialbattery_config.default.ini'),
                os.path.join(W, 'sb', 'config.default.ini'))
    with open(os.path.join(W, 'sb', 'restart.sh'), 'w') as f:
        f.write('echo restarted > %s\n' % os.path.join(W, 'sb_restarted'))
    with open(os.path.join(W, 'log', 'fridge', 'current'), 'w') as f:
        f.write('@40000000%08x%08x INFO fridge poll ok\n' % (int(time.time()) + 10, 0))

    env = dict(os.environ, PYTHONPATH=velib)
    procs = [subprocess.Popen([sys.executable, os.path.join(DRIVERS, script),
                               '--config', cfg, '--section', section], env=env)
             for script, section in (('mopeka_venus.py', 'mopeka_standard'),
                                     ('esp32_hydro_tank.py', 'esp32_hydro'),
                                     ('fridge_venus.py', 'fridge'))]
    procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, 'fake_battery.py')],
                                  env=dict(env, VELIB=velib)))
    procs.append(subprocess.Popen(
        [sys.executable, os.path.join(DRIVERS, 'web_ui.py'), '--config', cfg, '--port', str(web['port']),
         '--bind', '127.0.0.1', '--no-firewall', '--sample-s', '1', '--init-dir', os.path.join(W, 'init.d'),
         '--service-dir', os.path.join(W, 'service'), '--log-dir', os.path.join(W, 'log'),
         '--rc-local', os.path.join(W, 'rc.local'), '--serialbattery-dir', os.path.join(W, 'sb')],
        env=dict(env, PATH=os.path.join(W, 'bin') + os.pathsep + env['PATH'])))

    GLib.timeout_add(500, advertise, devices)
    loop = GLib.MainLoop()
    result = {}

    def values(service, paths):
        return {p: get(bus, service, p) for p in paths}

    def check():
        try:
            result['mopeka'] = values('com.victronenergy.tank.mopeka_standard', (
                '/Level', '/Remaining', '/FluidType', '/Temperature', '/Connected', '/Rssi',
                '/DeviceInstance', '/ProductName', '/Mopeka/DistanceMm'))
            result['esp'] = values('com.victronenergy.tank.esp32_hydro', (
                '/Level', '/Remaining', '/Capacity', '/FluidType', '/Status', '/Hydro/HeightMm'))
            result['esp_temp'] = values('com.victronenergy.temperature.esp32_hydro_temp', (
                '/Temperature', '/TemperatureType', '/Connected', '/CustomName'))
            result['zone1'] = values('com.victronenergy.temperature.fridge_zone1', (
                '/Temperature', '/TemperatureType', '/Fridge/SupplyVoltage',
                '/Fridge/TargetTemperature', '/Connected'))
            result['zone2'] = values('com.victronenergy.temperature.fridge_zone2', (
                '/Temperature', '/TemperatureType', '/CustomName'))
            # what the GUI's switch pane does: freezer to -15 C, power off
            bus.call_blocking('com.victronenergy.switch.fridge_controls',
                              '/SwitchableOutput/setpoint2/Dimming', 'com.victronenergy.BusItem',
                              'SetValue', 'v', [dbus.Double(-15.0, variant_level=1)])
            bus.call_blocking('com.victronenergy.switch.fridge_controls',
                              '/SwitchableOutput/power/State', 'com.victronenergy.BusItem',
                              'SetValue', 'v', [dbus.Int32(0, variant_level=1)])
            GLib.timeout_add_seconds(7, check_control)
            return False
        except dbus.exceptions.DBusException as e:
            result['error'] = str(e)
        loop.quit()
        return False

    def web_worker():
        import base64
        auth = {'Authorization': 'Basic ' + base64.b64encode(('x:' + web['password']).encode()).decode()}
        csrf = {'X-Requested-With': 'venussettings'}
        out = web['results'] = {}

        def http_call(name, method, path, form=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', web['port'], timeout=20)
            body = None
            h = dict(headers or {})
            if form is not None:
                body = '&'.join('%s=%s' % (k, v) for k, v in form.items())
                h['Content-Type'] = 'application/x-www-form-urlencoded'
            conn.request(method, path, body, h)
            r = conn.getresponse()
            data = r.read()
            conn.close()
            try:
                import json
                parsed = json.loads(data.decode())
            except ValueError:
                parsed = data
            out[name] = (r.status, parsed)

        try:
            for _ in range(50):                      # the page needs a moment to start
                try:
                    socket.create_connection(('127.0.0.1', web['port']), 1).close()
                    break
                except OSError:
                    time.sleep(0.2)
            http_call('page', 'GET', '/')
            http_call('state', 'GET', '/api/state')
            http_call('locked', 'GET', '/api/driver_config')
            http_call('config', 'GET', '/api/driver_config', headers=auth)
            http_call('no_csrf', 'POST', '/api/driver_config', {'section': 'mopeka_standard'}, auth)
            http_call('save', 'POST', '/api/driver_config',
                      {'section': 'mopeka_standard', 'f.capacity_l': '24', 'restart': '1'}, dict(auth, **csrf))
            http_call('bad', 'POST', '/api/driver_config',
                      {'section': 'mopeka_standard', 'f.capacity_l': 'x'}, dict(auth, **csrf))
            http_call('sb_get', 'GET', '/api/serialbattery', headers=auth)
            http_call('sb_save', 'POST', '/api/serialbattery',
                      {'s.MAX_BATTERY_CHARGE_CURRENT': '40', 'restart': '1'}, dict(auth, **csrf))
            http_call('log', 'GET', '/api/log?service=fridge', headers=auth)
            http_call('pair', 'POST', '/api/pair_reset', {'section': 'mopeka_standard'}, dict(auth, **csrf))
            http_call('reboot', 'POST', '/api/reboot', {}, dict(auth, **csrf))
        except Exception as e:                       # noqa: BLE001 - reported by the main thread
            out['exception'] = repr(e)

    def check_control():
        result['paired_mac'] = SettingsTree.stored.get('/Settings/Devices/mopeka_standard/Mac')
        threading.Thread(target=web_worker, daemon=True).start()
        GLib.timeout_add(200, wait_for_web)
        try:
            result['control'] = {
                'freezer_target': get(bus, 'com.victronenergy.temperature.fridge_zone2',
                                      '/Fridge/TargetTemperature'),
                'powered': get(bus, 'com.victronenergy.temperature.fridge_zone1',
                               '/Fridge/PoweredOn'),
                'power_switch': get(bus, 'com.victronenergy.switch.fridge_controls',
                                    '/SwitchableOutput/power/State'),
                'freezer_range': (get(bus, 'com.victronenergy.switch.fridge_controls',
                                      '/SwitchableOutput/setpoint2/DimmingMin'),
                                  get(bus, 'com.victronenergy.switch.fridge_controls',
                                      '/SwitchableOutput/setpoint2/DimmingMax')),
            }
        except dbus.exceptions.DBusException as e:
            result['error'] = str(e)
        return False

    def wait_for_web():
        res = web.get('results', {})
        if 'reboot' in res or 'exception' in res or time.time() > web['deadline']:
            keep = int(os.environ.get('KEEP_UI') or 0)
            if keep:
                print('UI at http://127.0.0.1:%d/  password %s' % (web['port'], web['password']), flush=True)
                GLib.timeout_add_seconds(keep, loop.quit)
            else:
                loop.quit()
            return False
        return True

    web['deadline'] = time.time() + 120
    GLib.timeout_add_seconds(9, check)
    loop.run()

    # SIGTERM -> graceful shutdown
    for p in procs:
        p.terminate()
    codes = [p.wait(10) for p in procs]
    del names

    for k, v in sorted(result.items()):
        print(k, {p: (x if not isinstance(x, dbus.Double) else float(x))
                  for p, x in v.items()} if isinstance(v, dict) else v)
    print('fridge connects=%d (rejected while scanning: %d) disconnects=%d, adapter stops=%d, '
          'sessions now=%d' % (fridge_dev.connects, fridge_dev.busy_rejects,
                               fridge_dev.disconnects, adapter.stops, len(adapter.sessions)))

    m, e, t, z1, z2 = (result.get(k) for k in ('mopeka', 'esp', 'esp_temp', 'zone1', 'zone2'))
    assert m and e and t and z1 and z2, result
    assert m['/Connected'] == 1 and m['/FluidType'] == 8 and m['/Rssi'] == -71
    assert abs(m['/Level'] - 90.2) < 0.2, m['/Level']
    assert m['/Temperature'] == 19
    assert e['/Level'] == 50.0 and e['/Remaining'] == 0.0375 and e['/Capacity'] == 0.075
    assert e['/FluidType'] == 1 and e['/Hydro/HeightMm'] == 210
    assert t['/Temperature'] == 12.5 and t['/TemperatureType'] == 2 and t['/Connected'] == 1
    assert z1['/Temperature'] == 4 and z1['/TemperatureType'] == 1 and z1['/Connected'] == 1
    assert z1['/Fridge/SupplyVoltage'] == 12.8 and z1['/Fridge/TargetTemperature'] == 3
    assert z2['/Temperature'] == -17 and z2['/TemperatureType'] == 6
    assert z2['/CustomName'] == 'Freezer'
    # the fridge only connects when NO process scans: the Mopeka and ESP32
    # drivers must have paused for it, and everyone resumed afterwards
    assert fridge_dev.disconnects >= 1 and fridge_dev.connects >= 1, fridge_dev.connects
    assert adapter.stops >= 3, 'not all drivers paused discovery: %d' % adapter.stops
    assert len(adapter.sessions) == 3, 'discovery not resumed: %r' % adapter.sessions
    # GUI control: the commands reached the fridge and its read-back status
    # shows the new values
    c = result.get('control')
    assert c, result
    assert c['freezer_target'] == -15 and c['powered'] == 0 and c['power_switch'] == 0, c
    assert c['freezer_range'] == (-20, -12), c
    sent = [cmd for cmd, _ in fridge_dev.commands]
    assert fridge.CMD_SET_RIGHT in sent and fridge.CMD_SET_OTHERS in sent, sent

    # Mopeka paired via the sync button and stored the MAC in localsettings
    assert result['paired_mac'] == MOPEKA_MAC, result['paired_mac']
    check_web_results(web)
    assert codes == [0, 0, 0, 0], codes
    print('INTEGRATION OK')


if __name__ == '__main__':
    main()
