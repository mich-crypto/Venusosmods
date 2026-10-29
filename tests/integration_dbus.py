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

import os
import subprocess
import sys

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


def dual_zone_status():
    data = bytearray(30)
    data[1], data[2], data[4] = 1, 1, 3          # on, eco, left target 3 C
    data[14] = 4                                 # fridge 4 C
    data[15], data[16], data[17] = 0x7F, 12, 8   # 12.8 V
    data[18], data[26] = 0xEE, 0xEF              # freezer target -18, current -17
    data[28] = 3
    return fridge.build_frame(fridge.CMD_QUERY, bytes(data))


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
        self.fridge.received += bytes(bytearray(value))
        if self.fridge.received.endswith(fridge.query_frame()) and self.fridge.notifying:
            self.fridge.received = b''
            # answer in two notifications, like the real controller
            for part in fridge.chunks(dual_zone_status()):
                GLib.idle_add(self.fridge.notify_char.PropertiesChanged, CHAR,
                              {'Value': dbus.Array(part, signature='y')}, [])


class Fridge(Device):
    def __init__(self, conn):
        path = dev_path(FRIDGE_MAC)
        super().__init__(conn, path)
        self.connected = self.notifying = False
        self.received = b''
        self.connects = self.disconnects = self.busy_rejects = 0
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


def main():
    velib = os.environ.get('VELIB')
    if not velib:
        sys.exit('set VELIB=/path/to/velib_python')
    os.environ['DBUS_SYSTEM_BUS_ADDRESS'] = os.environ['DBUS_SESSION_BUS_ADDRESS']

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    names = [dbus.service.BusName(n, bus) for n in ('org.bluez', 'com.victronenergy.settings')]
    fridge_dev = Fridge(bus)
    Root(bus, fridge_dev)
    SettingsTree(bus, '/Settings')
    adapter = Adapter(bus, '/org/bluez/hci0')
    devices = {'mopeka': Device(bus, dev_path(MOPEKA_MAC)), 'esp': Device(bus, dev_path(ESP_MAC))}

    cfg = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'venus_it_config.ini')
    with open(cfg, 'w') as f:
        f.write('[mopeka_standard]\ncapacity_l = 27\n'
                '[esp32_hydro]\ndevice_id = 1\ntemperature_service = yes\n'
                '[fridge]\nmac = %s\npoll_interval = 10\n' % FRIDGE_MAC)

    env = dict(os.environ, PYTHONPATH=velib)
    procs = [subprocess.Popen([sys.executable, os.path.join(DRIVERS, script),
                               '--config', cfg, '--section', section], env=env)
             for script, section in (('mopeka_venus.py', 'mopeka_standard'),
                                     ('esp32_hydro_tank.py', 'esp32_hydro'),
                                     ('fridge_venus.py', 'fridge'))]

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
        except dbus.exceptions.DBusException as e:
            result['error'] = str(e)
        loop.quit()
        return False

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
    # Mopeka paired via the sync button and stored the MAC in localsettings
    assert SettingsTree.stored.get('/Settings/Devices/mopeka_standard/Mac') == MOPEKA_MAC, \
        SettingsTree.stored
    assert codes == [0, 0, 0], codes
    print('INTEGRATION OK')


if __name__ == '__main__':
    main()
