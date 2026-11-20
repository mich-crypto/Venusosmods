#!/usr/bin/env python3
"""
Firmware update over the air (Bluetooth) for the ESP32 water tank sender.

  python3 esp32_ota.py esp32_hydro_tank.ino.bin            # sender from config.ini [esp32_hydro]
  python3 esp32_ota.py --mac AA:BB:CC:DD:EE:FF --key SECRET file.bin
  python3 esp32_ota.py --check file.bin                    # only look at the file

The settings page runs this for you (Water tank -> Update firmware). The sender
must already run a firmware with OTA_KEY set (the first flash with the key is
by USB), and ota_key in config.ini must be the same key.

Protocol: see the "updates over the air" section of
firmware/esp32_hydro_tank/esp32_hydro_tank.ino. In short: read a nonce, prove
we know the key with HMAC-SHA256(key, "<nonce> <size> <md5>"), send the image
in numbered chunks, and the sender checks the MD5 before it boots the new one.
A failed or interrupted update leaves the old firmware running.
"""

import argparse
import configparser
import os
import re
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SERVICE_UUID = '8e7f0001-5648-4f54-a1c3-5f2d7c3a9b10'
CTRL_UUID = '8e7f0002-5648-4f54-a1c3-5f2d7c3a9b10'
DATA_UUID = '8e7f0003-5648-4f54-a1c3-5f2d7c3a9b10'

DEFAULT_CONFIG = os.path.join(HERE, 'config.ini')
MARKER_RE = re.compile(rb'VHT-FW ([0-9A-Za-z._+-]{1,24}) id=(\d{1,3}) ;')
MIN_SIZE = 64 * 1024            # a bootloader or partition table is smaller
MAX_SIZE = 3 * 1024 * 1024      # a merged image (bootloader + app) is bigger than any app slot
CHUNK_HEADER = 4                # uint32 offset in front of every chunk
MAX_CHUNK = 512 - CHUNK_HEADER  # attribute values are at most 512 bytes
FALLBACK_CHUNK = 240            # MTU unknown: BlueZ splits it into a long write if needed
COMMAND_TIMEOUT = 15            # seconds to wait for an answer on the control characteristic


class OtaError(Exception):
    pass


# --------------------------------------------------------------- hashing
# hashlib/hmac are not on every Venus OS image: fall back to md5sum and a small SHA-256.

def md5_hex(data):
    try:
        import hashlib
        return hashlib.md5(data).hexdigest()
    except (ImportError, AttributeError, ValueError):
        out = subprocess.run(['md5sum'], input=data, stdout=subprocess.PIPE, check=True).stdout
        return out.split()[0].decode()


_K = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]


def _sha256_py(data):
    """Plain SHA-256 (FIPS 180-4), only used for the short HMAC when hashlib is missing."""
    h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
    msg = bytes(data) + b'\x80' + b'\x00' * ((55 - len(data)) % 64) + struct.pack('>Q', len(data) * 8)
    rot = lambda x, n: ((x >> n) | (x << (32 - n))) & 0xFFFFFFFF  # noqa: E731
    for i in range(0, len(msg), 64):
        w = list(struct.unpack('>16L', msg[i:i + 64]))
        for t in range(16, 64):
            s0 = rot(w[t - 15], 7) ^ rot(w[t - 15], 18) ^ (w[t - 15] >> 3)
            s1 = rot(w[t - 2], 17) ^ rot(w[t - 2], 19) ^ (w[t - 2] >> 10)
            w.append((w[t - 16] + s0 + w[t - 7] + s1) & 0xFFFFFFFF)
        a, b, c, d, e, f, g, hh = h
        for t in range(64):
            t1 = (hh + (rot(e, 6) ^ rot(e, 11) ^ rot(e, 25)) + ((e & f) ^ (~e & g)) + _K[t] + w[t]) & 0xFFFFFFFF
            t2 = ((rot(a, 2) ^ rot(a, 13) ^ rot(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) & 0xFFFFFFFF
            a, b, c, d, e, f, g, hh = (t1 + t2) & 0xFFFFFFFF, a, b, c, (d + t1) & 0xFFFFFFFF, e, f, g
        h = [(x + y) & 0xFFFFFFFF for x, y in zip(h, (a, b, c, d, e, f, g, hh))]
    return struct.pack('>8L', *h)


def hmac_sha256_hex(key, msg):
    key, msg = key.encode() if isinstance(key, str) else key, msg.encode() if isinstance(msg, str) else msg
    try:
        import hashlib
        import hmac
        return hmac.new(key, msg, hashlib.sha256).hexdigest()
    except (ImportError, AttributeError, ValueError):
        pass
    if len(key) > 64:
        key = _sha256_py(key)
    key = key.ljust(64, b'\x00')
    inner = _sha256_py(bytes(k ^ 0x36 for k in key) + msg)
    outer = _sha256_py(bytes(k ^ 0x5c for k in key) + inner)
    return ''.join('%02x' % b for b in outer)


# --------------------------------------------------------------- image

def inspect_image(data):
    """
    Check a firmware file before it is sent. Returns {'size', 'md5', 'version', 'device_id'};
    raises OtaError with a message for the user.
    """
    if len(data) < 24 or data[0] != 0xE9:
        if data[:2] == b'\xaa\x50':
            raise OtaError('this is the partition table (.partitions.bin); use the .ino.bin file')
        raise OtaError('this is not an ESP32 firmware file; use the .ino.bin file from '
                       '"Sketch > Export Compiled Binary"')
    if len(data) > MAX_SIZE:
        raise OtaError('the file is %d KB: that is the merged image (.merged.bin) with the bootloader; '
                       'use the .ino.bin file' % (len(data) // 1024))
    if len(data) < MIN_SIZE:
        raise OtaError('the file is only %d KB: that is the bootloader (.bootloader.bin); '
                       'use the .ino.bin file' % (len(data) // 1024))
    chip = struct.unpack_from('<H', data, 12)[0]
    if chip != 9:   # esp_chip_id_t: 0 ESP32, 2 S2, 5 C3, 9 S3
        raise OtaError('the file is built for another chip (id %d); the water sender is an ESP32-S3: '
                       'pick "ESP32S3 Dev Module" as the board' % chip)
    m = MARKER_RE.search(data)
    if not m:
        raise OtaError('this firmware is not the water tank sender (esp32_hydro_tank.ino 1.1.0 or newer)')
    return {'size': len(data), 'md5': md5_hex(data), 'version': m.group(1).decode(),
            'device_id': int(m.group(2))}


def chunks(data, size):
    """Data frames: <offset uint32 LE><bytes>."""
    for off in range(0, len(data), size):
        yield struct.pack('<I', off) + data[off:off + size]


def chunk_size(mtu):
    if not mtu or mtu < 23:
        return FALLBACK_CHUNK
    return max(16, min(MAX_CHUNK, mtu - 3 - CHUNK_HEADER))


def parse_info(text):
    """'info fw=1.1.0 id=1 nonce=ab.. free=1310720' -> dict."""
    if not text.startswith('info '):
        return None
    return dict(p.split('=', 1) for p in text[5:].split() if '=' in p)


# --------------------------------------------------------------- the update

class OtaUpdate(object):
    """
    One update over a GattClient (ble_gatt.py) whose write and notify characteristic is
    CTRL_UUID and which also knows DATA_UUID. done(ok, message) is called once.
    """

    def __init__(self, gatt, image, key, done, say=print, clock=time.time, expect_id=None):
        self.gatt, self.image, self.key, self.done, self.say = gatt, image, key, done, say
        self.clock = clock
        self.expect_id = expect_id
        self.info = inspect_image(image)
        self.stage = 'idle'
        self.sent = 0
        self.total = 0
        self.size = FALLBACK_CHUNK
        self.last_pct = -1
        self.started = None
        self.deadline = None
        self.finished = False
        gatt.on_written = self._on_written

    def start(self):
        self.stage = 'info'
        self.deadline = self.clock() + COMMAND_TIMEOUT + 45     # includes connecting
        self.say('Connecting to the sender %s ...' % self.gatt.mac)
        self.gatt.exchange([b'info'], self._exchange_done)

    def tick(self):
        """Call every second or so: catches a sender that stops answering."""
        if not self.finished and self.deadline and self.clock() > self.deadline:
            self._finish(False, 'the sender did not answer (%s)' % self.stage)
        return not self.finished

    def on_notify(self, value):
        text = bytes(value).decode('ascii', 'replace').strip()
        if text.startswith('err'):
            self._finish(False, 'the sender says: ' + text[3:].strip())
        elif self.stage == 'info':
            self._got_info(text)
        elif self.stage == 'begin' and text == 'ok begin':
            self._send_image()
        elif self.stage == 'end' and text == 'ok end':
            self.say('The sender checked the new firmware and restarts into it.')
            self._finish(True, 'Updated to %s. The sender restarts; the readings are back in a few seconds.'
                         % self.info['version'])

    def _got_info(self, text):
        info = parse_info(text)
        if not info or 'nonce' not in info:
            return
        self.say('Sender runs firmware %s (id %s); new firmware %s (id %d), %d KB.'
                 % (info.get('fw', '?'), info.get('id', '?'), self.info['version'], self.info['device_id'],
                    self.info['size'] // 1024))
        if self.expect_id is not None and str(self.info['device_id']) != str(self.expect_id):
            self._finish(False, 'the new firmware has DEVICE_ID %d but config.ini expects %s: the Pi would '
                         'no longer recognise the sender. Change DEVICE_ID in the sketch.'
                         % (self.info['device_id'], self.expect_id))
            return
        free = int(info.get('free', '0') or 0)
        if free and self.info['size'] > free:
            self._finish(False, 'the firmware (%d KB) does not fit the sender\'s update slot (%d KB)'
                         % (self.info['size'] // 1024, free // 1024))
            return
        msg = '%s %d %s' % (info['nonce'], self.info['size'], self.info['md5'])
        self.stage = 'begin'
        self.deadline = self.clock() + COMMAND_TIMEOUT + 30          # erasing the slot takes a while
        self.gatt.send([('begin %d %s %s' % (self.info['size'], self.info['md5'],
                                             hmac_sha256_hex(self.key, msg))).encode()])

    def _send_image(self):
        self.stage = 'data'
        self.size = chunk_size(self.gatt.mtu(DATA_UUID))
        frames = list(chunks(self.image, self.size))
        self.total = len(frames)
        self.sent = 0
        self.started = self.clock()
        self.deadline = self.clock() + COMMAND_TIMEOUT
        self.say('Sending %d KB in %d chunks of %d bytes ...' % (len(self.image) // 1024, self.total, self.size))
        self.gatt.send(frames, DATA_UUID)

    def _on_written(self, uuid=None):
        if self.stage != 'data' or uuid != DATA_UUID:
            return     # e.g. the acknowledgement of "begin", which can come after its answer
        self.sent += 1
        self.deadline = self.clock() + COMMAND_TIMEOUT
        pct = self.sent * 100 // self.total
        if pct // 10 != self.last_pct // 10 or self.sent == self.total:
            self.last_pct = pct
            elapsed = max(0.001, self.clock() - self.started)
            self.say('%3d %%  %d KB/s' % (pct, int(self.sent * self.size / 1024 / elapsed)))
        if self.sent == self.total:
            self.stage = 'end'
            self.deadline = self.clock() + COMMAND_TIMEOUT
            self.gatt.send([b'end'])

    def _exchange_done(self, ok, error):
        if not ok and not self.finished:
            self._finish(False, error or 'connection ended')

    def _finish(self, ok, message):
        if self.finished:
            return
        self.finished = True
        self.stage = 'done'
        # disconnecting is enough: the sender drops a half-written update when the connection ends
        if ok:
            self.gatt.finish()
        else:
            self.gatt.close()
        self.done(ok, message)


# --------------------------------------------------------------- command line

def read_config(path, section):
    cp = configparser.ConfigParser()
    cp.read(path)
    if not cp.has_section(section):
        return {}
    return dict(cp.items(section))


def find_mac(bus, device_id):
    """MAC of the sender with this device id, from what BlueZ has seen recently."""
    import dbus
    import esp32_hydro_protocol as proto
    om = dbus.Interface(bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager')
    for path, ifaces in om.GetManagedObjects().items():
        dev = ifaces.get('org.bluez.Device1')
        if not dev:
            continue
        data = dev.get('ManufacturerData', {}).get(proto.COMPANY_ID)
        r = proto.parse(bytes(bytearray(data))) if data is not None else None
        if r is not None and r.device_id == device_id:
            return str(dev['Address'])
    return None


def device_known(bus, dev_path):
    import dbus
    try:
        props = dbus.Interface(bus.get_object('org.bluez', dev_path), 'org.freedesktop.DBus.Properties')
        props.Get('org.bluez.Device1', 'Address')
        return True
    except dbus.exceptions.DBusException:
        return False


def when_known(bus, adapter, mac, then, say, seconds=20):
    """
    BlueZ forgets a device it has not heard for ~30 s, and Connect needs it known. If it is
    missing, listen for it ourselves (the drivers' scanning is paused), then call then(found).
    """
    import dbus
    from gi.repository import GLib
    dev_path = '/org/bluez/%s/dev_%s' % (adapter, mac.upper().replace(':', '_'))
    if device_known(bus, dev_path):
        then(True)
        return
    say('Listening for the sender ...')
    ad = dbus.Interface(bus.get_object('org.bluez', '/org/bluez/' + adapter), 'org.bluez.Adapter1')
    try:
        ad.SetDiscoveryFilter({'Transport': dbus.String('le'), 'DuplicateData': dbus.Boolean(False)})
        ad.StartDiscovery()
    except dbus.exceptions.DBusException as e:
        say('(could not start a scan: %s)' % e)
    end = time.time() + seconds

    def poll():
        found = device_known(bus, dev_path)
        if found or time.time() > end:
            try:
                ad.StopDiscovery()
            except dbus.exceptions.DBusException:
                pass
            then(found)
            return False
        return True
    GLib.timeout_add(500, poll)


def main():
    ap = argparse.ArgumentParser(description='Update the ESP32 water sender over Bluetooth')
    ap.add_argument('image', help='the .ino.bin file')
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    ap.add_argument('--section', default='esp32_hydro')
    ap.add_argument('--mac', help='sender address (default: config.ini, else the one seen with the device id)')
    ap.add_argument('--key', help='OTA key (default: ota_key in config.ini)')
    ap.add_argument('--check', action='store_true', help='only check the file')
    ap.add_argument('--adapter', default=None)
    args = ap.parse_args()

    def say(text):
        print(text, flush=True)

    with open(args.image, 'rb') as f:
        image = f.read()
    try:
        info = inspect_image(image)
    except OtaError as e:
        say('Not flashed: ' + str(e))
        return 2
    say('File OK: water sender firmware %s, DEVICE_ID %d, %d KB, md5 %s'
        % (info['version'], info['device_id'], info['size'] // 1024, info['md5']))
    if args.check:
        return 0

    cfg = read_config(args.config, args.section)
    key = args.key or cfg.get('ota_key', '').strip()
    if not key:
        say('Not flashed: no OTA key. Set ota_key in config.ini ([%s]) to the OTA_KEY of the sender.' % args.section)
        return 2
    device_id = int(cfg.get('device_id', '1') or 1)

    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    import ble_scanner
    from ble_gatt import GattClient

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    mac = args.mac or cfg.get('mac', '').strip() or find_mac(bus, device_id)
    if not mac:
        say('Not flashed: the sender with DEVICE_ID %d has not been seen. Is it powered and in range? '
            'Or give --mac.' % device_id)
        return 2

    loop = GLib.MainLoop()
    result = {'ok': False}
    holder = {}

    def done(ok, message):
        result['ok'] = ok
        say(('Done: ' if ok else 'Failed: ') + message)
        GLib.timeout_add(500, loop.quit)

    # the other drivers stop scanning meanwhile: connecting and sending go much faster
    ble_scanner.request_global_pause(600)
    gatt = GattClient(bus, mac, CTRL_UUID, CTRL_UUID, lambda v: holder['ota'].on_notify(v),
                      adapter=args.adapter or cfg.get('adapter', 'hci0') or 'hci0', timeout_s=900,
                      extra_uuids=[DATA_UUID])
    ota = holder['ota'] = OtaUpdate(gatt, image, key, done, say=say, expect_id=device_id)

    def tick():
        ble_scanner.request_global_pause(600)
        return ota.tick()

    adapter = args.adapter or cfg.get('adapter', 'hci0') or 'hci0'

    def go(found):
        if not found:
            done(False, 'the sender %s was not heard. Is it powered and in range, and does its firmware '
                 'have an OTA key?' % mac)
            return
        GLib.timeout_add_seconds(1, tick)
        ota.start()
    GLib.idle_add(lambda: (when_known(bus, adapter, mac, go, say), False)[1])
    try:
        loop.run()
    finally:
        ble_scanner.clear_global_pause()
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
