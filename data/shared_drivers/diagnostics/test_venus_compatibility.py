#!/usr/bin/env python3
"""
Pre-flight check for the custom drivers. Read-only: it changes no files and
restarts nothing. The only side effect of --scan is a BLE discovery session.

  python3 /data/shared_drivers/diagnostics/test_venus_compatibility.py
  python3 /data/shared_drivers/diagnostics/test_venus_compatibility.py --scan 30

Every check prints PASS, WARN, FAIL or INFO. Exit code 1 if anything FAILed.
Passing here means the environment looks right - the final proof is still the
tank showing up in the GUI.
"""

import argparse
import configparser
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVERS = os.path.dirname(HERE)
sys.path.insert(0, DRIVERS)

SERVICES_DIR = '/data/etc/init.d'
RCLOCAL = '/data/rc.local'
MAC_RE = re.compile(r'^([0-9A-F]{2}:){5}[0-9A-F]{2}$')

results = []


def report(level, name, detail=''):
    results.append(level)
    print('%-4s  %-34s %s' % (level, name, detail))


# ------------------------------------------------------------ environment

def check_python():
    v = sys.version_info
    text = '%s (%d.%d.%d)' % (sys.executable, v[0], v[1], v[2])
    report('PASS' if v >= (3, 8) else 'FAIL', 'Python version', text)


def check_venus():
    try:
        with open('/opt/victronenergy/version') as f:
            lines = f.read().split()
        report('PASS', 'Venus OS', ' '.join(lines[:2]))
    except OSError:
        report('WARN', 'Venus OS', '/opt/victronenergy/version missing - not a Venus OS device?')
    for path in ('/etc/venus/machine', '/proc/device-tree/model'):
        try:
            with open(path) as f:
                report('INFO', 'Machine', f.read().strip('\x00\n '))
            break
        except OSError:
            continue
    report('PASS' if os.access('/data', os.W_OK) else 'FAIL', '/data writable')


def check_imports():
    ok = True
    for mod in ('dbus', 'dbus.mainloop.glib', 'gi.repository.GLib', 'socket'):
        try:
            __import__(mod)
            report('PASS', 'import ' + mod)
        except Exception as e:
            report('FAIL', 'import ' + mod, str(e))
            ok = False
    try:
        import json  # noqa: F401
        report('PASS', 'import json')
    except ImportError:
        report('WARN', 'import json', 'missing: the Starlink energy and data-use history will not be saved')
    return ok


def check_velib():
    import venus_service
    path = venus_service.find_velib()
    if path is None:
        report('FAIL', 'velib_python', 'not found in %s or /opt/victronenergy/*/ext/'
               % venus_service.VELIB_CANDIDATES[1])
        return False
    report('PASS', 'velib_python', path)
    if venus_service.VeDbusService is None:
        report('FAIL', 'import vedbus / settingsdevice', 'found the folder but import failed')
        return False
    import inspect
    params = inspect.signature(venus_service.VeDbusService.__init__).parameters
    if 'register' in params:
        report('PASS', 'VeDbusService(register=False)', 'supported')
    else:
        report('WARN', 'VeDbusService(register=False)',
               'old velib: service registers before all paths exist (works, but GUI may '
               'see incomplete services briefly)')
    return True


def check_dbus():
    import dbus
    try:
        bus = dbus.SystemBus()
        names = set(str(n) for n in bus.list_names())
    except Exception as e:
        report('FAIL', 'System D-Bus', str(e))
        return None, set()
    report('PASS', 'System D-Bus', '%d names' % len(names))
    report('PASS' if 'com.victronenergy.settings' in names else 'FAIL',
           'localsettings service', 'com.victronenergy.settings')
    return bus, names


def check_bluetooth(bus, names):
    import dbus
    if 'org.bluez' not in names:
        report('FAIL', 'BlueZ (bluetoothd)', 'org.bluez not on D-Bus - Bluetooth disabled in '
               'Settings or no adapter')
        return
    report('PASS', 'BlueZ (bluetoothd)')
    om = dbus.Interface(bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager')
    adapters = [(str(p), i['org.bluez.Adapter1']) for p, i in om.GetManagedObjects().items()
                if 'org.bluez.Adapter1' in i]
    if not adapters:
        report('FAIL', 'Bluetooth adapter', 'none found')
    for path, a in adapters:
        level = 'PASS' if a.get('Powered') else 'WARN'
        report(level, 'Adapter ' + path.rsplit('/', 1)[-1],
               '%s powered=%s discovering=%s' % (a.get('Address'), bool(a.get('Powered')),
                                                 bool(a.get('Discovering'))))


# ----------------------------------------------------------------- config

def driver_of(section, cfg):
    kind = cfg.get('driver', '') or section
    for prefix in ('mopeka', 'esp32', 'fridge', 'starlink', 'telegram', 'web'):
        if kind.startswith(prefix):
            return prefix
    return None


def check_config(path):
    cp = configparser.ConfigParser()
    try:
        if not cp.read(path):
            report('FAIL', 'config.ini', '%s not found (copy config.ini.example)' % path)
            return None
    except configparser.Error as e:
        report('FAIL', 'config.ini', 'syntax error: %s' % e)
        return None
    if not cp.sections():
        report('FAIL', 'config.ini', '%s has no [sections] - empty or not saved? '
               'See config.ini.example' % path)
        return None
    report('PASS', 'config.ini', '%s, sections: %s' % (path, ', '.join(cp.sections())))

    seen = {}
    for name in cp.sections():
        cfg = cp[name]
        kind = driver_of(name, cfg)
        if kind is None:
            report('WARN', '[%s]' % name, 'unknown driver - name the section mopeka_*, '
                   'esp32_*, fridge_* or web, or add driver = ...')
            continue
        if kind == 'telegram':
            if cfg.get('enabled', 'no').lower() in ('yes', 'true', '1', 'on') and not (cfg.get('token') and cfg.get('chat_id')):
                report('WARN', '[%s]' % name, 'enabled but the bot token or chat id is empty')
            else:
                report('PASS', '[%s]' % name, 'Telegram alerts')
            continue
        if kind == 'starlink':
            try:
                cfg.getboolean('enabled', True)
                if not 2 <= cfg.getint('interval', 5) <= 300:
                    report('FAIL', '[%s] interval' % name, 'use 2-300 seconds')
                cfg.getint('port', 9201)
            except ValueError as e:
                report('FAIL', '[%s]' % name, 'bad value: %s' % e)
                continue
            report('PASS', '[%s]' % name, 'Starlink dish at %s' % cfg.get('host', '192.168.100.1'))
            continue
        if kind == 'web':
            try:
                port = cfg.getint('port', 8088)
                if not 1024 <= port <= 65535:
                    report('FAIL', '[%s] port' % name, '%d: use 1024-65535' % port)
                cfg.getboolean('open_firewall', True)
            except ValueError as e:
                report('FAIL', '[%s]' % name, 'bad value: %s' % e)
                continue
            if not cfg.get('password', '').strip():
                report('WARN', '[%s] password' % name, 'empty: the settings page is read-only '
                       '(install.sh web_ui sets one)')
            report('PASS', '[%s]' % name, 'settings page, port %s' % cfg.get('port', 8088))
            continue
        service = cfg.get('service', name)
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', service):
            report('FAIL', '[%s] service' % name, '%r: use letters, digits, underscore' % service)
        if service in seen:
            report('FAIL', '[%s] service' % name, '%r already used by [%s]' % (service, seen[service]))
        seen[service] = name
        mac = cfg.get('mac', '').strip().upper()
        if mac and not MAC_RE.match(mac):
            report('FAIL', '[%s] mac' % name, '%r is not AA:BB:CC:DD:EE:FF' % mac)
        elif not mac and kind == 'fridge':
            report('FAIL', '[%s] mac' % name, 'required for this sensor')
        elif not mac and kind == 'mopeka':
            report('INFO', '[%s] mac' % name, 'empty: pairs with the first sensor whose '
                   'sync button is pressed')
        elif mac in ('AA:BB:CC:DD:EE:FF', '11:22:33:44:55:77'):
            report('WARN', '[%s] mac' % name, 'still the example address')
        try:
            if kind == 'mopeka':
                lo, hi = cfg.getfloat('empty_mm', 38), cfg.getfloat('full_mm', 254)
                if hi <= lo:
                    report('FAIL', '[%s] empty_mm/full_mm' % name, 'full_mm must be > empty_mm')
                if not cfg.getfloat('capacity_l', 0):
                    report('WARN', '[%s] capacity_l' % name, 'not set - no remaining litres')
                for key in ('level_table', 'level_curve'):
                    if cfg.get(key, '').strip():
                        from venus_service import parse_table
                        parse_table(cfg[key])
            elif kind == 'esp32':
                cfg.getint('device_id', 1)
                cfg.getboolean('temperature_service', False)
            elif kind == 'fridge':
                cfg.getint('poll_interval', 60)
                cfg.getboolean('stay_connected', False)
            report('PASS', '[%s]' % name, '%s driver, service %s' % (kind, service))
        except ValueError as e:
            report('FAIL', '[%s]' % name, 'bad value: %s' % e)
    return cp


def check_services(names):
    if not os.path.isdir(SERVICES_DIR):
        report('WARN', 'Service folders', '%s missing' % SERVICES_DIR)
        return
    try:
        with open(RCLOCAL) as f:
            rc = f.read()
    except OSError:
        rc = ''
    for svc in sorted(os.listdir(SERVICES_DIR)):
        link = '/service/' + svc
        persistent = ('/service/%s' % svc) in rc
        if not os.path.exists(link):
            report('INFO', 'service ' + svc, 'not enabled (install.sh %s)' % svc)
            continue
        try:
            state = subprocess.check_output(['svstat', link], stderr=subprocess.STDOUT,
                                            timeout=5).decode().strip()
        except Exception as e:
            state = 'svstat failed: %s' % e
        up = ': up' in state
        level = 'PASS' if up and persistent else 'WARN'
        detail = state.split(':', 1)[-1].strip()
        if not persistent:
            detail += ' - not in /data/rc.local, will be gone after reboot'
        report(level, 'service ' + svc, detail)


def check_bus_services(bus, names, cp):
    import dbus
    instances = {}
    for n in sorted(names):
        m = re.match(r'^com\.victronenergy\.(tank|temperature)\.', n)
        if not m:
            continue
        try:
            inst = bus.call_blocking(n, '/DeviceInstance', 'com.victronenergy.BusItem',
                                     'GetValue', '', [])
            pname = bus.call_blocking(n, '/ProductName', 'com.victronenergy.BusItem',
                                      'GetValue', '', [])
        except dbus.exceptions.DBusException:
            inst, pname = '?', '?'
        key = (m.group(1), inst)
        report('INFO', n, 'instance %s, %s' % (inst, pname))
        if inst != '?' and key in instances:
            report('FAIL', 'Device instance %s:%s' % key, 'used by %s and %s' % (instances[key], n))
        instances[key] = n


# ------------------------------------------------------------------- scan

def ble_scan(bus, seconds):
    """Listen for adverts and summarise what each driver would recognise."""
    import dbus
    from gi.repository import GLib
    import esp32_hydro_protocol
    import mopeka_std_parser
    import alpicool_protocol
    from ble_scanner import BlueZScanner

    seen = {}

    def on_adv(mac, mfr, rssi):
        seen.setdefault(mac, {}).update({'mfr': mfr, 'rssi': rssi})

    BlueZScanner(bus, on_adv).start()
    print('\nScanning %d s... (press the Mopeka sync button now)' % seconds)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, loop.quit)
    loop.run()

    om = dbus.Interface(bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager')
    for path, ifaces in om.GetManagedObjects().items():
        d = ifaces.get('org.bluez.Device1')
        if d is None:
            continue
        mac = str(d.get('Address', '')).upper()
        info = seen.setdefault(mac, {})
        info['name'] = str(d.get('Name', d.get('Alias', '')))
        info['uuids'] = [str(u).lower() for u in d.get('UUIDs', [])]
        info.setdefault('rssi', int(d['RSSI']) if 'RSSI' in d else None)
        if 'mfr' not in info and 'ManufacturerData' in d:   # cached by BlueZ
            info['mfr'] = {int(k): bytes(bytearray(v)) for k, v in d['ManufacturerData'].items()}

    found = 0
    for mac, info in sorted(seen.items()):
        mfr = info.get('mfr', {})
        r = mopeka_std_parser.parse(mfr.get(mopeka_std_parser.MANUFACTURER_ID))
        if r:
            found += 1
            report('PASS', 'Mopeka %s' % r.sensor_type, '%s rssi=%s distance=%s mm temp=%s C '
                   'battery=%.2f V' % (mac, info.get('rssi'),
                                       None if r.distance_mm is None else round(r.distance_mm),
                                       r.temperature, r.battery_voltage))
        h = esp32_hydro_protocol.parse(mfr.get(esp32_hydro_protocol.COMPANY_ID))
        if h:
            found += 1
            report('PASS', 'ESP32 tank id %d' % h.device_id, '%s rssi=%s level=%.1f%% height=%d mm'
                   % (mac, info.get('rssi'), h.level, h.height_mm))
        name = info.get('name', '')
        if name.startswith(alpicool_protocol.NAME_PREFIXES) or \
                alpicool_protocol.SERVICE_UUID in info.get('uuids', []):
            found += 1
            report('PASS', 'Fridge candidate', '%s "%s" rssi=%s - confirm with fridge_probe.py'
                   % (mac, name, info.get('rssi')))
    if not found:
        report('WARN', 'BLE scan', 'no known sensor heard (%d devices seen)' % len(seen))
    print('\nAll devices seen:')
    for mac, info in sorted(seen.items(), key=lambda kv: -(kv[1].get('rssi') or -999)):
        print('  %s  rssi=%-5s %-24s mfr=%s' % (
            mac, info.get('rssi'), info.get('name', '')[:24],
            ','.join('0x%04X' % k for k in info.get('mfr', {}))))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--config', default=os.path.join(DRIVERS, 'config.ini'))
    ap.add_argument('--scan', type=int, metavar='SECONDS', default=0,
                    help='also listen for BLE sensors for this long')
    args = ap.parse_args()

    print('== Environment')
    check_python()
    check_venus()
    if check_imports():
        check_velib()
        bus, names = check_dbus()
        if bus is not None:
            check_bluetooth(bus, names)
    else:
        bus, names = None, set()
    print('\n== Configuration')
    cp = check_config(args.config)
    print('\n== Services')
    check_services(names)
    if bus is not None:
        check_bus_services(bus, names, cp)
        if args.scan:
            from dbus.mainloop.glib import DBusGMainLoop
            DBusGMainLoop(set_as_default=True)
            import dbus
            ble_scan(dbus.SystemBus(private=True), args.scan)

    print('\nSummary: %d PASS, %d WARN, %d FAIL' % (
        results.count('PASS'), results.count('WARN'), results.count('FAIL')))
    sys.exit(1 if 'FAIL' in results else 0)


if __name__ == '__main__':
    main()
