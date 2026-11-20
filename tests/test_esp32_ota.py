"""
Firmware update over Bluetooth for the ESP32 water sender (esp32_ota.py and its
web routes). FakeSender plays the firmware side exactly as
esp32_hydro_tank.ino does it (nonce, HMAC check, offsets, MD5), so the
protocol is tested from both ends without hardware.
"""

import hashlib
import hmac
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import fakes  # noqa: E402,F401
import esp32_ota as ota  # noqa: E402

KEY = 'my-secret-key-123'


def make_image(size=200 * 1024, chip=9, version='1.2.0', device_id=1, marker=True):
    head = bytearray(24)
    head[0] = 0xE9
    struct.pack_into('<H', head, 12, chip)
    body = bytearray((i * 7) & 0xFF for i in range(size - len(head)))
    if marker:
        m = ('VHT-FW %s id=%d ;' % (version, device_id)).encode()
        body[5000:5000 + len(m)] = m
    return bytes(head + body)


class FakeSender(object):
    """The firmware side of the protocol, written like the sketch."""

    def __init__(self, key=KEY, version='1.1.0', device_id=1, free=1310720):
        self.key, self.version, self.device_id, self.free = key, version, device_id, free
        self.nonce = os.urandom(16).hex()
        self.active = self.failed = False
        self.size = self.written = 0
        self.md5 = None
        self.buf = bytearray()
        self.replies = []
        self.flashed = None
        self.restarting = False

    def reply(self, text):
        self.replies.append(text.encode())

    def control(self, line):
        cmd, _, rest = line.decode().strip().partition(' ')
        if cmd == 'info':
            self.reply('info fw=%s id=%d nonce=%s free=%d' % (self.version, self.device_id, self.nonce, self.free))
        elif cmd == 'begin':
            size, md5, auth = rest.split(' ')
            expect = hmac.new(self.key.encode(), ('%s %s %s' % (self.nonce, size, md5)).encode(),
                              hashlib.sha256).hexdigest()
            self.nonce = os.urandom(16).hex()
            if not hmac.compare_digest(expect, auth.lower()):
                return self.reply('err auth')
            self.active, self.failed, self.size, self.written, self.md5 = True, False, int(size), 0, md5
            self.buf = bytearray()
            self.reply('ok begin')
        elif cmd == 'end':
            if not self.active:
                return self.reply('err not started')
            if self.written != self.size:
                return self.reply('err incomplete %d' % self.written)
            if hashlib.md5(bytes(self.buf)).hexdigest() != self.md5:
                self.active = False
                return self.reply('err MD5 Check Failed')
            self.flashed = bytes(self.buf)
            self.restarting = True
            self.reply('ok end')

    def data(self, frame):
        if not self.active or self.failed:
            return
        off = struct.unpack_from('<I', frame)[0]
        if off != self.written:
            self.failed = True
            return self.reply('err offset %d' % self.written)
        self.buf += frame[4:]
        self.written += len(frame) - 4


class FakeGatt(object):
    """Stands in for ble_gatt.GattClient: writes go to the FakeSender at once."""

    def __init__(self, sender, mtu=247, corrupt_at=None):
        self.sender, self._mtu, self.corrupt_at = sender, mtu, corrupt_at
        self.mac = 'AA:BB:CC:DD:EE:FF'
        self.busy = False
        self.on_written = None
        self.on_notify = None
        self.closed = self.finished = False
        self.frames = 0

    def exchange(self, frames, done):
        self.busy, self.done = True, done
        self.send(frames)

    def mtu(self, uuid=None):
        return self._mtu

    def send(self, frames, uuid=None):
        for f in frames:
            if not self.busy:
                return
            if uuid == ota.DATA_UUID:
                self.frames += 1
                if self.corrupt_at == self.frames:
                    f = f[:4] + b'\x00' + f[5:]
                self.sender.data(f)
            else:
                self.sender.control(f)
            while self.sender.replies:
                self.on_notify(self.sender.replies.pop(0))
            if uuid == ota.DATA_UUID and self.on_written and self.busy:
                self.on_written(ota.DATA_UUID)

    def finish(self):
        self.busy, self.finished = False, True

    def close(self):
        self.busy, self.closed = False, True


def run(sender, image, key=KEY, **kw):
    gatt = FakeGatt(sender, **kw)
    result = []
    log = []
    up = ota.OtaUpdate(gatt, image, key, lambda ok, msg: result.append((ok, msg)), say=log.append,
                       expect_id=1)
    gatt.on_notify = up.on_notify
    up.start()
    return result, gatt, log


class HashTests(unittest.TestCase):
    def test_pure_python_sha256_matches_hashlib(self):
        for msg in (b'', b'abc', b'x' * 55, b'x' * 56, b'y' * 64, os.urandom(300)):
            self.assertEqual(ota._sha256_py(msg), hashlib.sha256(msg).digest())

    def test_hmac_fallback_matches(self):
        real = ota.hmac_sha256_hex(KEY, 'nonce 123 abc')
        self.assertEqual(real, hmac.new(KEY.encode(), b'nonce 123 abc', hashlib.sha256).hexdigest())
        import builtins
        orig = builtins.__import__

        def no_hashlib(name, *a, **kw):
            if name in ('hashlib', 'hmac'):
                raise ImportError(name)
            return orig(name, *a, **kw)
        builtins.__import__ = no_hashlib
        try:
            self.assertEqual(ota.hmac_sha256_hex(KEY, 'nonce 123 abc'), real)
            self.assertEqual(ota.hmac_sha256_hex('k' * 100, 'm'),
                             hmac.new(b'k' * 100, b'm', hashlib.sha256).hexdigest())
        finally:
            builtins.__import__ = orig


class ImageTests(unittest.TestCase):
    def test_good_image(self):
        img = make_image()
        info = ota.inspect_image(img)
        self.assertEqual(info['version'], '1.2.0')
        self.assertEqual(info['device_id'], 1)
        self.assertEqual(info['md5'], hashlib.md5(img).hexdigest())

    def test_wrong_files(self):
        cases = [(b'\xaa\x50' + b'\x00' * 100000, 'partition table'),
                 (b'hello world' * 10000, 'not an ESP32 firmware'),
                 (make_image(size=20 * 1024), 'bootloader'),
                 (make_image(size=4 * 1024 * 1024), 'merged'),
                 (make_image(chip=0), 'another chip'),
                 (make_image(marker=False), 'not the water tank sender')]
        for data, words in cases:
            with self.assertRaises(ota.OtaError) as cm:
                ota.inspect_image(data)
            self.assertIn(words, str(cm.exception))

    def test_chunks(self):
        data = bytes(range(256)) * 3
        frames = list(ota.chunks(data, 100))
        self.assertEqual(len(frames), 8)
        self.assertEqual(struct.unpack_from('<I', frames[3])[0], 300)
        self.assertEqual(b''.join(f[4:] for f in frames), data)
        self.assertEqual(ota.chunk_size(None), ota.FALLBACK_CHUNK)
        self.assertEqual(ota.chunk_size(247), 240)
        self.assertEqual(ota.chunk_size(517), 508)

    def test_parse_info(self):
        self.assertEqual(ota.parse_info('info fw=1.1.0 id=1 nonce=ab free=9'),
                         {'fw': '1.1.0', 'id': '1', 'nonce': 'ab', 'free': '9'})
        self.assertIsNone(ota.parse_info('ok begin'))


class UpdateTests(unittest.TestCase):
    def test_full_update(self):
        img = make_image()
        sender = FakeSender()
        result, gatt, log = run(sender, img)
        self.assertEqual(result[0][0], True, result)
        self.assertEqual(sender.flashed, img)
        self.assertTrue(sender.restarting)
        self.assertTrue(gatt.finished)
        self.assertIn('100 %', '\n'.join(log))

    def test_wrong_key(self):
        sender = FakeSender(key='another-key-999')
        result, gatt, _ = run(sender, make_image())
        self.assertEqual(result, [(False, 'the sender says: auth')])
        self.assertIsNone(sender.flashed)
        self.assertTrue(gatt.closed)

    def test_corrupted_chunk_fails_md5(self):
        sender = FakeSender()
        result, _, _ = run(sender, make_image(), corrupt_at=10)
        self.assertFalse(result[0][0])
        self.assertIn('MD5', result[0][1])
        self.assertIsNone(sender.flashed)

    def test_wrong_device_id(self):
        sender = FakeSender()
        result, _, _ = run(sender, make_image(device_id=2))
        self.assertFalse(result[0][0])
        self.assertIn('DEVICE_ID 2', result[0][1])
        self.assertFalse(sender.active)

    def test_too_big_for_slot(self):
        sender = FakeSender(free=100 * 1024)
        result, _, _ = run(sender, make_image())
        self.assertIn('does not fit', result[0][1])

    def test_nonce_is_single_use(self):
        sender = FakeSender()
        sender.control(b'info')
        nonce = ota.parse_info(sender.replies.pop().decode())['nonce']
        img = make_image()
        info = ota.inspect_image(img)
        auth = ota.hmac_sha256_hex(KEY, '%s %d %s' % (nonce, info['size'], info['md5']))
        line = ('begin %d %s %s' % (info['size'], info['md5'], auth)).encode()
        sender.control(line)
        self.assertEqual(sender.replies.pop(), b'ok begin')
        sender.control(line)                                   # replayed
        self.assertEqual(sender.replies.pop(), b'err auth')

    def test_silent_sender_times_out(self):
        now = [1000.0]

        class Mute(FakeGatt):
            def send(self, frames, uuid=None):
                pass
        gatt = Mute(FakeSender())
        result = []
        up = ota.OtaUpdate(gatt, make_image(), KEY, lambda ok, m: result.append((ok, m)), say=lambda t: None,
                           clock=lambda: now[0])
        gatt.on_notify = up.on_notify
        up.start()
        self.assertTrue(up.tick())
        now[0] += 120
        self.assertFalse(up.tick())
        self.assertFalse(result[0][0])
        self.assertIn('did not answer', result[0][1])
        self.assertTrue(gatt.closed)


class WebRouteTests(unittest.TestCase):
    def setUp(self):
        import test_web_ui
        self.case = test_web_ui.AppTestCase('setUp')
        self.case.setUp()
        self.tmp = tempfile.mkdtemp()
        import web_ui
        self.web_ui = web_ui
        self.case.app.OTA_IMAGE = os.path.join(self.tmp, 'fw.bin')
        self.case.app.OTA_LOG = os.path.join(self.tmp, 'ota.log')
        self.started = []
        case = self

        class FakeProc(object):
            returncode = 0

            def __init__(self, argv, **kw):
                case.started.append(argv)

            def poll(self):
                return 0
        self.orig_popen = web_ui.subprocess.Popen
        web_ui.subprocess.Popen = FakeProc

    def tearDown(self):
        self.web_ui.subprocess.Popen = self.orig_popen

    def upload(self, data):
        import test_web_ui
        h = dict(test_web_ui.CSRF, **test_web_ui.basic(self.case.PASSWORD))
        h['content-type'] = 'application/octet-stream'
        req = self.web_ui.Request('POST', '/api/esp32_ota', {}, h, data, ('9.9.9.9', 1))
        return self.case.app.handle(req)

    def set_key(self, key):
        import config_tools as ct
        with open(self.case.config) as f:
            text = f.read()
        with open(self.case.config, 'w') as f:
            f.write(ct.set_values(text, 'esp32_hydro', {'ota_key': key}))

    def test_needs_key(self):
        r = self.upload(make_image())
        self.assertEqual(r.status, 400)
        self.assertIn(b'OTA key', r.body)
        self.assertEqual(self.started, [])

    def test_rejects_bad_file_and_wrong_id(self):
        self.set_key(KEY)
        r = self.upload(b'not firmware' * 10000)
        self.assertEqual(r.status, 400)
        r = self.upload(make_image(device_id=7))
        self.assertEqual(r.status, 400)
        self.assertIn(b'DEVICE_ID 7', r.body)
        self.assertEqual(self.started, [])

    def test_starts_update(self):
        self.set_key(KEY)
        img = make_image()
        r = self.upload(img)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(len(self.started), 1)
        self.assertIn('--section', self.started[0])
        with open(self.case.app.OTA_IMAGE, 'rb') as f:
            self.assertEqual(f.read(), img)
        r = self.case.call('GET', '/api/esp32_ota_log', headers=self.case_auth())
        self.assertIn(b'"success":true', r.body)

    def test_locked_without_password(self):
        import test_web_ui
        req = self.web_ui.Request('POST', '/api/esp32_ota', {}, dict(test_web_ui.CSRF), make_image(), ('9.9.9.9', 1))
        self.assertEqual(self.case.app.handle(req).status, 401)

    def test_body_limit(self):
        client = self.web_ui.Client.__new__(self.web_ui.Client)
        big = 2 * 1024 * 1024
        client.buf = ('POST /api/esp32_ota HTTP/1.1\r\nContent-Length: %d\r\n\r\n' % big).encode()
        self.assertIsNone(client.parse())                      # allowed, waits for the body
        client.buf = ('POST /api/raw HTTP/1.1\r\nContent-Length: %d\r\n\r\n' % big).encode()
        self.assertEqual(client.parse().status, 413)

    def case_auth(self):
        import test_web_ui
        return test_web_ui.basic(self.case.PASSWORD)


if __name__ == '__main__':
    unittest.main()
