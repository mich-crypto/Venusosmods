#!/usr/bin/env python3
"""
Firmware update of the ESP32 water sender from the Pi (firmware 2.7+).

  python3 esp32_ota.py --mac AA:BB:CC:DD:EE:FF --password watertank [--download | --file F] [--ip 192.168.1.60]

1. Gets the app image (--download: the latest from GitHub, or --file).
2. Checks it is an ESP32 app image (not the FLASH-AT-0x0 file).
3. With --ip (the sender is on the van Wi-Fi): uploads it over HTTP, done in seconds.
4. Otherwise over Bluetooth: stops the water driver (one connection at a time),
   pauses the other drivers' scanning, connects, "auth", "ota begin <size> <md5>",
   sends the image in chunks, "ota end". The sender checks the MD5 and restarts.
   If anything fails it keeps its old firmware.

Run by the settings page (Water tab, "Update sender firmware"); prints progress
lines the page shows. Exit code 0 = updated.
"""

import argparse
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import esp32_hydro_protocol as proto  # noqa: E402

REPO = 'mich-crypto/Venusosmods'
FW_URL = 'https://raw.githubusercontent.com/%s/master/firmware/bin/WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin' % REPO
VERSION_URL = 'https://raw.githubusercontent.com/%s/master/firmware/bin/VERSION' % REPO
BLUEZ = 'org.bluez'
DEVICE_IFACE = 'org.bluez.Device1'
CHAR_IFACE = 'org.bluez.GattCharacteristic1'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
OM_IFACE = 'org.freedesktop.DBus.ObjectManager'


def say(text):
    print(text, flush=True)


def check_image(data):
    """None if data looks like an ESP32 app image, else the reason."""
    if len(data) < 4096:
        return 'file too small (%d bytes)' % len(data)
    if data[0] != 0xE9:
        return 'not an ESP32 firmware image'
    if data[27] != 0x3C:
        return 'this is the full image for the USB flasher (FLASH-AT-0x0), not the app update'
    return None


def packet_size(mtu):
    """Firmware bytes per write. One ATT packet holds MTU - 3 bytes, minus the 'D'.
    Without a negotiated MTU BlueZ splits bigger writes itself (long write)."""
    if mtu and mtu > 23:
        return min(500, mtu - 3) - 1
    return 499


def chunks(data, size):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def download(url, path):
    """curl, or wget when curl is missing or fails (the installer uses wget too)."""
    try:
        subprocess.check_call(['curl', '-fsSL', '-m', '120', '-o', path, url])
        return
    except (OSError, subprocess.CalledProcessError) as e:
        say('curl did not work (%s), trying wget' % e)
    subprocess.check_call(['wget', '-q', '-T', '120', '-O', path, url])


def http_update(ip, path):
    """Upload through the sender's own page; True when it says OK."""
    try:
        out = subprocess.run(['curl', '-s', '-m', '180', '-F', 'fw=@%s' % path, 'http://%s/update' % ip],
                             capture_output=True, text=True, timeout=200).stdout
    except (OSError, subprocess.SubprocessError) as e:
        say('Wi-Fi upload failed: %s' % e)
        return False
    say('Sender says: %s' % out.strip())
    return out.startswith('Update OK')


class BleLink(object):
    """Synchronous GATT session over BlueZ D-Bus (no main loop needed)."""

    def __init__(self, mac, adapter='hci0'):
        import dbus
        self.dbus = dbus
        self.bus = dbus.SystemBus()
        self.path = '/org/bluez/%s/dev_%s' % (adapter, mac.upper().replace(':', '_'))
        self.cmd = self.reply = None
        self.mtu = 0                       # 0 = BlueZ does not say

    def _props(self, path):
        return self.dbus.Interface(self.bus.get_object(BLUEZ, path), PROPS_IFACE)

    def connect(self, attempts=4):
        dev = self.dbus.Interface(self.bus.get_object(BLUEZ, self.path), DEVICE_IFACE)
        for i in range(attempts):
            try:
                dev.Connect(timeout=30)
                break
            except self.dbus.exceptions.DBusException as e:
                if i == attempts - 1:
                    raise
                say('connect attempt %d failed (%s), trying again' % (i + 1, e.get_dbus_message()))
                time.sleep(3)
        for _ in range(100):
            if self._props(self.path).Get(DEVICE_IFACE, 'ServicesResolved'):
                break
            time.sleep(0.2)
        om = self.dbus.Interface(self.bus.get_object(BLUEZ, '/'), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if not str(path).startswith(self.path + '/') or CHAR_IFACE not in ifaces:
                continue
            uuid = str(ifaces[CHAR_IFACE]['UUID']).lower()
            if uuid == proto.LINK_CMD_UUID:
                self.cmd = str(path)
                self.mtu = int(ifaces[CHAR_IFACE].get('MTU', 0) or 0)
            elif uuid == proto.LINK_REPLY_UUID:
                self.reply = str(path)
        if not (self.cmd and self.reply):
            raise RuntimeError('the sender has no Bluetooth update service (firmware older than 2.7: '
                               'update once over USB or its own page)')

    def write(self, data):
        char = self.dbus.Interface(self.bus.get_object(BLUEZ, self.cmd), CHAR_IFACE)
        char.WriteValue(self.dbus.Array(bytearray(data), signature='y'),
                        self.dbus.Dictionary({'type': 'request'}, signature='sv'), timeout=30)

    def read(self):
        char = self.dbus.Interface(self.bus.get_object(BLUEZ, self.reply), CHAR_IFACE)
        return bytes(bytearray(char.ReadValue(self.dbus.Dictionary({}, signature='sv'), timeout=30))).decode('utf-8', 'replace')

    def command(self, text):
        self.write(b'C' + text.encode('utf-8'))
        return self.read()

    def disconnect(self):
        try:
            self.dbus.Interface(self.bus.get_object(BLUEZ, self.path), DEVICE_IFACE).Disconnect()
        except Exception:
            pass


def md5_of(path):
    """MD5 with the system tool (hashlib is not certain on Venus OS; BusyBox has md5sum)."""
    return subprocess.check_output(['md5sum', path]).decode().split()[0]


def v2_packet_size(mtu):
    """Firmware bytes per packet that fit one Bluetooth frame: MTU - 3, minus 'D' and the offset."""
    return max(16, min(500, (mtu or 23) - 3) - 5)


def parse_status(text):
    """'ota active 12345/1266000' -> 12345; anything else -> None (the sender dropped the upload)."""
    m = re.match(r'ota active (\d+)/(\d+)', text or '')
    return int(m.group(1)) if m else None


def progress(sent, total, started, step):
    pct = sent * 100 // total
    if pct >= step:
        rate = sent / max(0.1, time.time() - started) / 1024
        say('Sending %3d %%  (%d of %d kB, %.1f kB/s)' % (pct, sent // 1024, total // 1024, rate))
        return pct + 5
    return step


def ble_update(mac, password, data, md5, adapter='hci0', max_errors=40):
    link = BleLink(mac, adapter)

    def connect():
        say('Connecting to the sender over Bluetooth ...')
        link.connect()
        answer = link.command('auth ' + password)
        if not answer.startswith('ok'):
            raise RuntimeError('the sender refused the password: set "Sender password" to its Wi-Fi password')

    try:
        connect()
        total = len(data)
        answer = link.command('ota begin2 %d %s' % (total, md5))
        if answer.startswith('ok ota begin v2'):
            send_v2(link, connect, data, answer, max_errors)
        else:
            # Firmware before 3.0: chunks without offsets, so an error can't be repaired.
            # Packets that fit one frame avoid the split-up writes that failed before.
            time.sleep(1.5)                   # it answers "unknown command" to begin2 in the meantime
            answer = link.command('ota begin %d %s' % (total, md5))
            if not answer.startswith('ok'):
                raise RuntimeError(answer)
            size = packet_size(link.mtu) if link.mtu else 179
            say('Older sender firmware: one try, %d-byte packets.' % size)
            sent, step, started = 0, 0, time.time()
            for part in chunks(data, size):
                link.write(b'D' + part)
                sent += len(part)
                step = progress(sent, total, started, step)
        answer = link.command('ota end')
        for _ in range(60):                   # the sender checks the whole image first
            if answer != 'working':
                break
            time.sleep(0.5)
            answer = link.read()
        say('Sender says: %s' % answer)
        if not answer.startswith('ok'):
            raise RuntimeError(answer)
    finally:
        link.disconnect()


def send_v2(link, connect, data, answer, max_errors):
    """Chunks with their offset: after an error ask how much arrived and carry on from there."""
    m = re.search(r'mtu=(\d+)', answer)
    size = v2_packet_size(int(m.group(1)) if m else link.mtu)
    total, pos, errors, step, started = len(data), 0, 0, 0, time.time()
    say('Sending %d kB in %d-byte packets.' % (total // 1024, size))
    while pos < total:
        part = data[pos:pos + size]
        try:
            link.write(b'D' + struct.pack('<I', pos) + part)
            pos += len(part)
            step = progress(pos, total, started, step)
            continue
        except Exception as e:                 # a refused packet, or the connection dropped
            errors += 1
            if errors > max_errors:
                raise RuntimeError('too many transfer errors (last: %s)' % e)
            say('Packet at %d kB not accepted (%s): checking with the sender ...' % (pos // 1024, short(e)))
        pos = resume(link, connect)


def resume(link, connect, attempts=5):
    """Where to carry on: ask the sender, reconnecting if the connection is gone."""
    for i in range(attempts):
        time.sleep(1 + i)
        try:
            got = parse_status(link.command('ota status'))
        except Exception:
            try:
                link.disconnect()
                connect()
                got = parse_status(link.command('ota status'))
            except Exception as e:
                say('reconnect attempt %d failed: %s' % (i + 1, short(e)))
                continue
        if got is None:
            raise RuntimeError('the sender dropped the upload: start the update again')
        say('Sender has %d kB, carrying on from there.' % (got // 1024))
        return got
    raise RuntimeError('lost the connection to the sender')


def short(e):
    text = getattr(e, 'get_dbus_message', lambda: str(e))() or str(e)
    return text.strip().splitlines()[0][:120] if text.strip() else type(e).__name__


def svc(action, service):
    path = '/service/%s' % service
    if os.path.isdir(path):
        subprocess.call(['svc', action, path])


def main():
    ap = argparse.ArgumentParser(description='Update the ESP32 water sender')
    ap.add_argument('--mac', help='the sender\'s Bluetooth address')
    ap.add_argument('--password', default='watertank', help='the sender\'s Wi-Fi password (also its Bluetooth key)')
    ap.add_argument('--file', help='app image (WEBPAGE-UPDATE-ONLY_...bin)')
    ap.add_argument('--download', action='store_true', help='get the latest from GitHub')
    ap.add_argument('--ip', help='the sender\'s address on the van Wi-Fi: upload over HTTP instead')
    ap.add_argument('--service', default='esp32_hydro', help='daemontools service of the water driver')
    ap.add_argument('--adapter', default='hci0')
    args = ap.parse_args()

    path = args.file
    if args.download or not path:
        path = '/tmp/venusosmods-sender-fw.bin'
        say('Downloading the latest sender firmware ...')
        download(FW_URL, path)
    with open(path, 'rb') as f:
        data = f.read()
    problem = check_image(data)
    if problem:
        say('Not updating: %s' % problem)
        return 2
    say('Firmware: %d kB' % (len(data) // 1024))

    if args.ip:
        say('The sender is on the van Wi-Fi (%s): uploading over Wi-Fi ...' % args.ip)
        if http_update(args.ip, path):
            say('Done. The sender restarts with the new firmware.')
            return 0
        say('Wi-Fi upload did not work, trying Bluetooth.')
    if not args.mac:
        say('Not updating: the sender\'s Bluetooth address is not known yet (no reading received).')
        return 2

    other = proto.ota_running()
    if other and other != os.getpid():
        say('Not updating: another sender update is running (process %d).' % other)
        return 2
    import ble_scanner
    with open(proto.OTA_LOCK, 'w') as f:   # page and driver keep their hands off meanwhile
        f.write('%d %s\n' % (os.getpid(), args.service))
    svc('-d', args.service)               # the driver checks in over the same link
    ble_scanner.request_global_pause(1200)
    time.sleep(3)
    try:
        ble_update(args.mac, args.password, data, md5_of(path), args.adapter)
        say('Done. The sender restarts with the new firmware (about 10 s).')
        return 0
    except Exception as e:
        say('Update failed: %s' % e)
        say('The sender keeps its old firmware. Try again, or update over USB.')
        return 1
    finally:
        ble_scanner.clear_global_pause()
        try:
            os.remove(proto.OTA_LOCK)
        except OSError:
            pass
        svc('-u', args.service)


if __name__ == '__main__':
    sys.exit(main())
