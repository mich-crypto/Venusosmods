"""
The water level sensor over the Wi-Fi network instead of Bluetooth.

The sensor's own web server already answers everything the Bluetooth link does:
  GET  /api/status?pi=1        the reading (level, litres, temperatures, ...) - replaces the broadcast
  POST /api/cmd?pi=1  c=...    any serial command, the answer as text - replaces the link frames
  POST /update                 a firmware file (multipart) - replaces the Bluetooth update
'pi=1' tells the sensor the Pi is there (like a Bluetooth check-in).

WifiClient has the interface of ble_gatt.GattClient (exchange / finish / busy / keep_connected),
so the water driver's check-ins, commands, live view, logs and heater work unchanged over it.

Venus OS ships a trimmed Python (no urllib / threading guaranteed), so the requests are made
with curl, or busybox wget when curl is missing, as child processes (starlink_hub.run_async):
the GLib main loop never waits for the network.
"""

import json
import os
import time

from starlink_hub import run_async

EOT = b'\x04'
TIMEOUT_S = 6


def quote(text):
    """Percent-encode for a form body (urllib is not on every Venus OS)."""
    safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~'
    return ''.join(c if c in safe else ''.join('%%%02X' % b for b in c.encode('utf-8')) for c in text)


def request_argv(url, post_file=None, post_data=None, content_type=None, timeout=TIMEOUT_S):
    """sh script: curl, else wget; the answer on stdout, exit code != 0 on failure."""
    ct = content_type or 'application/x-www-form-urlencoded'
    if post_file:
        c = 'curl -fsS -m %d -H "Content-Type: %s" --data-binary @"$2" "$1"' % (timeout, ct)
        w = 'wget -q -T %d -O - --header "Content-Type: %s" --post-file "$2" "$1"' % (timeout, ct)
        arg = post_file
    elif post_data is not None:
        c = 'curl -fsS -m %d -H "Content-Type: %s" --data-binary "$2" "$1"' % (timeout, ct)
        w = 'wget -q -T %d -O - --header "Content-Type: %s" --post-data "$2" "$1"' % (timeout, ct)
        arg = post_data
    else:
        c = 'curl -fsS -m %d "$1"' % timeout
        w = 'wget -q -T %d -O - "$1"' % timeout
        arg = ''
    script = ('if command -v curl >/dev/null 2>&1; then %s; else %s; fi' % (c, w))
    return ['sh', '-c', script, 'http', url, arg]


def http(callback, url, timeout=TIMEOUT_S, **kw):
    """callback(ok, text) from the main loop."""
    def done(rc, out):
        text = (out or b'').decode('utf-8', 'replace')
        if rc is None:
            callback(False, 'no answer within %d s' % timeout)
        elif rc != 0:
            callback(False, text.strip().splitlines()[-1] if text.strip() else 'request failed (%d)' % rc)
        else:
            callback(True, text)
    run_async(request_argv(url, timeout=timeout, **kw), done, timeout=timeout + 2, with_errors=False)


def multipart_file(path, out_path):
    """The firmware as a multipart/form-data body (field "fw", like the sensor's page): content type."""
    boundary = '----venusosmods%d' % int(time.time() * 1000)
    with open(path, 'rb') as f:
        data = f.read()
    with open(out_path, 'wb') as f:
        f.write(('--%s\r\nContent-Disposition: form-data; name="fw"; filename="%s"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n' % (boundary, os.path.basename(path))).encode())
        f.write(data)
        f.write(('\r\n--%s--\r\n' % boundary).encode())
    return 'multipart/form-data; boundary=' + boundary


class WifiClient(object):
    """Stand-in for GattClient over HTTP. Frames: b'C<command>' each become one request,
    b'Cauth ...' is skipped (the sensor's web server has no password), data frames
    (Bluetooth update) are refused: over Wi-Fi the whole file goes to /update instead."""

    def __init__(self, host, on_notify):
        self.host = host
        self.mac = 'WIFI'
        self._on_notify = on_notify
        self.busy = False
        self.keep_connected = False
        self.frames_sent = 0
        self.trace = None
        self._done_cb = None
        self._t0 = 0
        self._frames = []

    def _t(self, text):
        if self.trace is not None:
            try:
                self.trace('%5.1fs %s' % (time.monotonic() - self._t0, text))
            except Exception:
                pass

    def exchange(self, frames, done_cb):
        if self.busy:
            done_cb(False, 'busy')
            return
        self.busy = True
        self._done_cb = done_cb
        self.frames_sent = 0
        self._t0 = time.monotonic()
        self._frames = list(frames)
        self._next()

    def _next(self):
        if not self.busy:
            return
        while self._frames and self._frames[0].startswith(b'Cauth '):
            self._frames.pop(0)
            self.frames_sent += 1
        if not self._frames:
            self._end(True, None)
            return
        f = self._frames.pop(0)
        if not f.startswith(b'C'):
            self._end(False, 'Bluetooth data packets cannot go over Wi-Fi')
            return
        cmd = f[1:].decode('utf-8', 'replace')
        self.frames_sent += 1
        http(self._answer, 'http://%s/api/cmd?pi=1' % self.host, post_data='c=' + quote(cmd))

    def _answer(self, ok, text):
        if not self.busy:
            return
        if not ok:
            self._end(False, 'Wi-Fi: %s' % text)
            return
        self._on_notify(text.encode('utf-8') + EOT)      # may call finish()
        self._next()

    def finish(self):
        self._end(True, None)

    def _end(self, ok, error):
        if self.busy:
            self.busy = False
            self._frames = []
            self._t('done' if ok else 'failed: %s' % error)
            cb, self._done_cb = self._done_cb, None
            if cb:
                cb(ok, error)

    def close(self):
        self.busy = False


class StatusPoller(object):
    """Reads /api/status every interval s; on_status(dict) or on_error(text) in the main loop."""

    def __init__(self, host, interval, on_status, on_error):
        self.host, self.interval = host, interval
        self.on_status, self.on_error = on_status, on_error
        self._running = False

    def start(self, glib):
        glib.timeout_add_seconds(self.interval, self.tick)
        glib.timeout_add(500, lambda: (self.tick(), False)[1])

    def tick(self):
        if not self._running:
            self._running = True
            http(self._done, 'http://%s/api/status?pi=1' % self.host)
        return True

    def _done(self, ok, text):
        self._running = False
        if not ok:
            self.on_error(text)
            return
        try:
            d = json.loads(text)
        except ValueError:
            self.on_error('not a sensor answer')
            return
        self.on_status(d)
