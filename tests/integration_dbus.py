#!/usr/bin/env python3
"""
End-to-end test on a private D-Bus, without Venus OS or Bluetooth hardware:
a fake BlueZ sends advertisements, a fake localsettings answers AddSettings,
and the real drivers (with the real velib_python) publish tank services.

Needs dbus-python + PyGObject, dbus-run-session and a velib_python checkout:
  git clone https://github.com/victronenergy/velib_python /tmp/velib_python
  VELIB=/tmp/velib_python dbus-run-session -- python3 tests/integration_dbus.py
"""

import os
import subprocess
import sys
import time

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVERS = os.path.join(HERE, '..', 'data', 'shared_drivers')
sys.path.insert(0, HERE)
sys.path.insert(0, DRIVERS)

import esp32_hydro_protocol as proto  # noqa: E402
from packets import mopeka_packet  # noqa: E402

PROPS = 'org.freedesktop.DBus.Properties'
MOPEKA_MAC = 'AA:BB:CC:DD:EE:01'
ESP_MAC = 'AA:BB:CC:DD:EE:02'


class Device(dbus.service.Object):
    @dbus.service.signal(PROPS, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


class Adapter(dbus.service.Object):
    def __init__(self, conn, path):
        super().__init__(conn, path)
        self.discovering = False

    @dbus.service.method(PROPS, in_signature='ss', out_signature='v')
    def Get(self, iface, prop):
        return {'Powered': True, 'Discovering': self.discovering}[prop]

    @dbus.service.method('org.bluez.Adapter1', in_signature='a{sv}')
    def SetDiscoveryFilter(self, f):
        assert f['Transport'] == 'le'

    @dbus.service.method('org.bluez.Adapter1')
    def StartDiscovery(self):
        self.discovering = True


class Root(dbus.service.Object):
    """org.bluez ObjectManager and com.victronenergy.settings AddSettings on '/'."""

    @dbus.service.method('org.freedesktop.DBus.ObjectManager', out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        return {dbus.ObjectPath('/org/bluez/hci0'): {'org.bluez.Adapter1': {'Powered': True}}}

    @dbus.service.method('com.victronenergy.Settings', in_signature='aa{sv}', out_signature='aa{sv}')
    def AddSettings(self, items):
        return [{'path': i['path'], 'error': 0, 'value': i['default']} for i in items]


def advertise(devices):
    m = devices['mopeka']
    m.PropertiesChanged('org.bluez.Device1', {
        'RSSI': dbus.Int16(-71),
        'ManufacturerData': dbus.Dictionary(
            {dbus.UInt16(0x000D): dbus.Array(mopeka_packet([(19, 0), (9, 12)] + [(0, 0)] * 10),
                                             signature='y')}, signature='qv'),
    }, [])
    advertise.seq += 1
    payload = proto.build(1, 50.0, 37.5, 75.0, 210, temperature=12.5, sequence=advertise.seq)
    devices['esp'].PropertiesChanged('org.bluez.Device1', {
        'RSSI': dbus.Int16(-55),
        'ManufacturerData': dbus.Dictionary(
            {dbus.UInt16(0xFFFF): dbus.Array(payload, signature='y')}, signature='qv'),
    }, [])
    return True


advertise.seq = 0


def get(bus, service, path):
    return bus.call_blocking(service, path, 'com.victronenergy.BusItem', 'GetValue', '', [])


def main():
    velib = os.environ.get('VELIB')
    if not velib:
        sys.exit('set VELIB=/path/to/velib_python')
    addr = os.environ['DBUS_SESSION_BUS_ADDRESS']
    os.environ['DBUS_SYSTEM_BUS_ADDRESS'] = addr

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    names = [dbus.service.BusName(n, bus) for n in ('org.bluez', 'com.victronenergy.settings')]
    Root(bus, '/')
    Adapter(bus, '/org/bluez/hci0')
    devices = {
        'mopeka': Device(bus, '/org/bluez/hci0/dev_' + MOPEKA_MAC.replace(':', '_')),
        'esp': Device(bus, '/org/bluez/hci0/dev_' + ESP_MAC.replace(':', '_')),
    }

    cfg = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'venus_it_config.ini')
    with open(cfg, 'w') as f:
        f.write('[mopeka_standard]\nmac = %s\ncapacity_l = 27\n'
                '[esp32_hydro]\ndevice_id = 1\n' % MOPEKA_MAC.lower())

    env = dict(os.environ, PYTHONPATH=velib)
    procs = [subprocess.Popen([sys.executable, os.path.join(DRIVERS, script),
                               '--config', cfg, '--section', section], env=env)
             for script, section in (('mopeka_venus.py', 'mopeka_standard'),
                                     ('esp32_hydro_tank.py', 'esp32_hydro'))]

    GLib.timeout_add(500, advertise, devices)
    loop = GLib.MainLoop()
    result = {}

    def check():
        try:
            result['mopeka'] = {p: get(bus, 'com.victronenergy.tank.mopeka_standard', p)
                                for p in ('/Level', '/Remaining', '/FluidType',
                                          '/Temperature', '/Connected', '/Rssi',
                                          '/DeviceInstance', '/ProductName')}
            result['esp'] = {p: get(bus, 'com.victronenergy.tank.esp32_hydro', p)
                             for p in ('/Level', '/Remaining', '/Capacity', '/FluidType',
                                       '/Temperature', '/Status', '/Hydro/HeightMm')}
        except dbus.exceptions.DBusException as e:
            result['error'] = str(e)
        loop.quit()
        return False

    GLib.timeout_add_seconds(5, check)
    loop.run()
    for p in procs:
        p.terminate()
    del names

    print(result)
    m, e = result.get('mopeka'), result.get('esp')
    assert m and e, result
    assert m['/Connected'] == 1 and m['/FluidType'] == 8 and m['/Rssi'] == -71
    assert abs(m['/Level'] - 90.2) < 0.2, m['/Level']
    assert m['/Temperature'] == 19
    assert e['/Level'] == 50.0 and e['/Remaining'] == 0.0375 and e['/Capacity'] == 0.075
    assert e['/FluidType'] == 1 and e['/Temperature'] == 12.5 and e['/Hydro/HeightMm'] == 210
    print('INTEGRATION OK')


if __name__ == '__main__':
    main()
