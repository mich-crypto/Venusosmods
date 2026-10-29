#!/usr/bin/env python3
"""
Find out whether a Bluetooth fridge (e.g. Hyckes) speaks the Alpicool
protocol that fridge_venus.py understands.

  1. Close the fridge app on your phone (the fridge allows one connection).
  2. python3 fridge_probe.py                 -> lists nearby candidates
  3. python3 fridge_probe.py AA:BB:CC:DD:EE:FF
       connects, lists the GATT services, sends one status query and prints
       the raw answer and the decoded values.

If step 3 says the 1234/1235/1236 characteristics are missing, your fridge
uses a different protocol: send the printed service list to whoever helps you
extend the driver.
"""

import argparse
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402

import alpicool_protocol as proto  # noqa: E402
from ble_gatt import GattClient  # noqa: E402
from ble_scanner import BlueZScanner  # noqa: E402

OM = 'org.freedesktop.DBus.ObjectManager'


def managed(bus):
    return dbus.Interface(bus.get_object('org.bluez', '/'), OM).GetManagedObjects()


def scan(bus, seconds):
    BlueZScanner(bus, lambda *a: None).start()
    print('Scanning %d s...' % seconds)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, loop.quit)
    loop.run()
    rows = []
    for path, ifaces in managed(bus).items():
        d = ifaces.get('org.bluez.Device1')
        if not d or 'RSSI' not in d:
            continue
        name = str(d.get('Name', ''))
        uuids = [str(u).lower() for u in d.get('UUIDs', [])]
        hit = name.startswith(proto.NAME_PREFIXES) or proto.SERVICE_UUID in uuids
        rows.append((not hit, -int(d['RSSI']), str(d['Address']), name, hit))
    for _, rssi, mac, name, hit in sorted(rows):
        print('%s %s  rssi=%-4d %s' % ('-> LIKELY FRIDGE' if hit else '               ',
                                       mac, -rssi, name))
    print('\nNo fridge listed? Unnamed devices may still be it: try the strongest '
          'signals next to the fridge with: fridge_probe.py <MAC>')


def probe(bus, mac, timeout, loop, scanner=None, rssi=None):
    asm = proto.FrameAssembler()
    state = {'status': None}

    def on_notify(data):
        print('  notify: %s' % data.hex(' '))
        for cmd, payload in asm.feed(data):
            print('  frame cmd=0x%02X data=%s' % (cmd, payload.hex(' ')))
            if cmd == proto.CMD_QUERY:
                state['status'] = proto.parse_status(payload)
                client.finish()

    def list_gatt():
        dev = client.dev_path
        print('\nGATT characteristics:')
        for path, ifaces in sorted(managed(bus).items()):
            c = ifaces.get('org.bluez.GattCharacteristic1')
            if c and str(path).startswith(dev + '/'):
                print('  %s  %s' % (c['UUID'], ','.join(str(f) for f in c.get('Flags', []))))

    def done(ok, error):
        try:
            list_gatt()
        except dbus.exceptions.DBusException:
            pass
        if ok and state['status']:
            st = state['status']
            print('\nOK - this fridge speaks the Alpicool protocol.')
            for i, z in enumerate(st.zones, 1):
                print('  zone %d: current %s C, target %s C' % (i, z.current, z.target))
            print('  powered=%s eco=%s supply=%.1f V battery-saver=%s' % (
                st.powered_on, st.eco_mode, st.supply_voltage, st.battery_saver))
            print('\nPut  mac = %s  in the [fridge] section of config.ini.' % mac)
        else:
            print('\nFAILED: %s' % error)
            print('\nThings to try:\n'
                  '  * close the fridge app / switch off Bluetooth on every phone nearby\n'
                  '  * bring the Pi and the fridge closer (signal -70 or better)\n'
                  '  * switch the fridge off and on, wait 30 s, try again')
        loop.quit()

    if rssi is not None:
        print('Heard the fridge, signal %d dBm%s' % (rssi, '  (weak - move closer if this fails)'
                                                     if rssi < -78 else ''))
    print('Connecting (scanning paused, up to 3 attempts; close the phone app first)...')
    client = GattClient(bus, mac, proto.WRITE_UUID, proto.NOTIFY_UUID, on_notify,
                        timeout_s=timeout, scanner=scanner)
    frame = proto.query_frame()
    print('  write : %s' % frame.hex(' '))
    client.exchange(proto.chunks(frame), done)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mac', nargs='?', help='connect to this fridge and query it')
    ap.add_argument('--seconds', type=int, default=20, help='scan time without a MAC')
    ap.add_argument('--wait', type=int, default=45,
                    help='with a MAC: how long to wait for the fridge to be heard')
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format='  %(message)s')
    logging.getLogger('ble_gatt').setLevel(logging.DEBUG)   # show connect attempts
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    if args.mac:
        # Connect right after hearing a broadcast: the fridge is then known to
        # BlueZ and in range, which matters on a weak link.
        mac = args.mac.upper()
        loop = GLib.MainLoop()
        state = {'heard': False}

        def on_adv(m, mfr, rssi):
            if not state['heard']:
                state['heard'] = True
                GLib.idle_add(probe, bus, mac, 60, loop, scanner, rssi)

        def give_up():
            if not state['heard']:
                print('\nFAILED: the fridge was not heard in %d s.\n'
                      '  * A connected phone makes the fridge invisible: close the app\n'
                      '    completely or switch off the phone\'s Bluetooth.\n'
                      '  * Out of range? Bring the Pi and the fridge closer.\n'
                      '  * Check that the fridge is switched on.' % args.wait)
                loop.quit()
            return False

        scanner = BlueZScanner(bus, on_adv, mac_filter=[mac])
        scanner.start()
        print('Waiting up to %d s for the fridge %s to be heard...' % (args.wait, mac))
        GLib.timeout_add_seconds(args.wait, give_up)
        loop.run()
    else:
        scan(bus, args.seconds)


if __name__ == '__main__':
    main()
