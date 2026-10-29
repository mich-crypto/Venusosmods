"""
Everything behind the Starlink tabs: polls the dish (status and the 900-second history),
runs ICMP probes, speed tests and the WAN IP check, keeps the books (energy, data use,
outages) and sends Telegram alerts.

All commands (curl, ping) run through `run_async`, which starts the process and hands the
result to a callback from the GLib main loop, so nothing here ever blocks the page.
"""

import collections
import logging
import os
import subprocess
import time

from gi.repository import GLib

import netmon
import starlink

log = logging.getLogger('starlink')

DEFAULT_TARGETS = [('Cloudflare', '1.1.1.1'), ('Google', '8.8.8.8'), ('Quad9', '9.9.9.9'), ('Dish', '192.168.100.1')]
INFO_ALERTS = starlink.INFO_ALERTS
TRUE = ('1', 'yes', 'true', 'on')


def run_async(argv, callback, input_bytes=None, timeout=10):
    """Run a command; callback(returncode, stdout) later, from the main loop (returncode None = timed out)."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if input_bytes is not None:
            proc.stdin.write(input_bytes)          # small (a request, a curl config): fits the pipe buffer
            proc.stdin.close()
    except (OSError, ValueError) as e:
        GLib.idle_add(lambda: (callback(127, str(e).encode()), False)[1])
        return
    deadline = time.time() + timeout

    def check():
        if proc.poll() is None:
            if time.time() < deadline:
                return True
            proc.kill()
            proc.wait()
            out = proc.stdout.read()
            proc.stdout.close()
            callback(None, out)
            return False
        out = proc.stdout.read()
        proc.stdout.close()
        callback(proc.returncode, out)
        return False

    GLib.timeout_add(100, check)


def _bool(v, default):
    return default if v in (None, '') else str(v).strip().lower() in TRUE


def _num(v, default, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def parse_targets(text):
    """'Name=1.2.3.4, 8.8.8.8' -> [(name, host)]; empty -> the defaults."""
    out = []
    for part in str(text or '').split(','):
        part = part.strip()
        if not part:
            continue
        name, _, host = part.partition('=')
        if not host:
            name, host = part, part
        host = host.strip()
        if host and all(c.isalnum() or c in '.-:_' for c in host) and not host.startswith('-'):
            out.append((name.strip() or host, host))
    return out[:8] or list(DEFAULT_TARGETS)


class Hub(object):
    def __init__(self, cfg, telegram, store, clock=time.time):
        cfg, telegram = dict(cfg or {}), dict(telegram or {})
        self.clock = clock
        self.store = store
        self.enabled = _bool(cfg.get('enabled'), True)
        self.host = cfg.get('host') or '192.168.100.1'
        self.interval = int(_num(cfg.get('interval'), 5, 2, 300))
        self.port = int(_num(cfg.get('port'), 9201, 1, 65535))
        self.history_interval = int(_num(cfg.get('history_interval'), 60, 10, 600))
        self.icmp_interval = int(_num(cfg.get('icmp_interval'), 30, 5, 3600))
        self.icmp_good, self.icmp_warn = _num(cfg.get('icmp_good_ms'), 40, 1), _num(cfg.get('icmp_warn_ms'), 100, 1)
        self.targets = parse_targets(cfg.get('icmp_targets'))
        self.speedtest_every = int(_num(cfg.get('speedtest_interval'), 0, 0, 1440)) * 60
        self.test_down_mb = int(_num(cfg.get('speedtest_down_mb'), 25, 1, 200))
        self.test_up_mb = int(_num(cfg.get('speedtest_up_mb'), 8, 1, 100))
        self.min_sane = _num(cfg.get('min_sane_mbps'), 5, 0)
        self.cycle_day = int(_num(cfg.get('cycle_day'), 1, 1, 28))
        self.cap_gb = _num(cfg.get('cap_gb'), 0, 0)
        self.price = _num(cfg.get('price_per_kwh'), 0, 0)
        self.currency = cfg.get('currency') or 'EUR'
        self.wan_check = _bool(cfg.get('wan_check'), True)
        self.test_server = (cfg.get('test_server') or 'https://speed.cloudflare.com').rstrip('/')      # only changed by the tests
        self.wan_url = cfg.get('wan_url') or 'https://1.1.1.1/cdn-cgi/trace'
        self.tg = {'enabled': _bool(telegram.get('enabled'), False), 'token': telegram.get('token', ''),
                   'chat_id': telegram.get('chat_id', ''), 'drop_pct': _num(telegram.get('drop_pct'), 5, 0.1),
                   'offline_min': _num(telegram.get('offline_min'), 5, 1),
                   'kinds': dict((k, _bool(telegram.get('alert_' + k), True)) for k in
                                 ('offline', 'drop', 'hardware', 'wan', 'test'))}
        # live state
        self.status = None
        self.time = None
        self.error = None
        self.via = None
        self.method = 0
        self.fails = 0
        self.busy = False
        self.skip_until = 0
        self.unreachable_since = None
        self.history = starlink.History(interval=self.interval, clock=clock)
        self.hist_busy = False
        self.hist_error = None
        self.hist_time = None
        self.hist_skip_until = 0
        self.icmp = {}                                     # name -> {...}
        self.icmp_busy = False
        self.test = {'running': False, 'step': '', 'started': None}
        self.wan = {'ip': None, 'colo': None, 'loc': None, 'time': None, 'error': None}
        self.alert_state = {}                              # key -> last time sent (cool-down)
        self.queue = []                                    # unsent Telegram messages
        self.known_alerts = None
        self.drop_run = 0
        self.offline_sent = False
        self.last_test_start = 0
        self.power_now = None
        self.sending = False

    # ------------------------------------------------------------ the dish
    def candidates(self):
        web = ['--http1.1', '-H', 'Content-Type: application/grpc-web+proto', '-H', 'X-Grpc-Web: 1',
               '-H', 'Accept: application/grpc-web+proto']
        native = ['--http2-prior-knowledge', '-H', 'Content-Type: application/grpc', '-H', 'TE: trailers']
        path = starlink.SERVICE_PATH
        return [('gRPC-web port %d' % self.port, web, 'http://%s:%d%s' % (self.host, self.port, path)),
                ('gRPC-web port 80', web, 'http://%s%s' % (self.host, path)),
                ('gRPC port 9200', native, 'http://%s:9200%s' % (self.host, path))]

    def _dish_call(self, request, decode, done, tried=0, last_error=None):
        cands = self.candidates()
        idx = (self.method + tried) % len(cands)
        name, args, url = cands[idx]

        def finished(rc, out):
            error, result = None, None
            if rc != 0:
                error = 'timed out' if rc is None else 'curl exit code %s' % rc
            else:
                try:
                    result = decode(out)
                except ValueError as e:
                    error = str(e)
            if result is not None:
                self.method, self.via = idx, name
                done(result, None)
            elif tried + 1 < len(cands):
                self._dish_call(request, decode, done, tried + 1, '%s: %s' % (name, error))
            else:
                done(None, '%s: %s' % (name, error))

        run_async(['curl', '-s', '-m', '5', '-X', 'POST'] + args + ['--data-binary', '@-', url], finished,
                  input_bytes=request, timeout=8)

    def check_offline(self):
        """Runs on every tick, also while polling is backed off, so the alert is not delayed by the backoff."""
        now = self.clock()
        if self.unreachable_since is None or self.offline_sent or self.time is None or not self.tg['kinds']['offline']:
            return
        offline_for = now - self.unreachable_since
        if offline_for >= self.tg['offline_min'] * 60:
            self.offline_sent = True
            self.alert('offline', 'Starlink dish not reachable for %d min (%s).' % (offline_for // 60, self.error), cooldown=0)

    def poll_status(self):
        if not self.enabled:
            return True
        self.check_offline()
        if self.busy or self.clock() < self.skip_until:
            return True
        self.busy = True
        self._dish_call(starlink.status_request(), starlink.decode_reply, self.status_done)
        return True

    def status_done(self, status, error):
        now = self.clock()
        self.busy = False
        if status is not None:
            self.status, self.time, self.error = status, now, None
            self.fails = 0
            self.history.add(status, now=now)
            if self.unreachable_since is not None:
                if now - self.unreachable_since >= 3 * self.interval:
                    self.store.add_unreachable(self.unreachable_since, now)
                if self.offline_sent:
                    self.alert('online', 'Starlink dish is reachable again.', cooldown=0)
                self.unreachable_since, self.offline_sent = None, False
            self.check_status_alerts(status)
            return
        self.error = error
        self.fails += 1
        if self.unreachable_since is None:
            self.unreachable_since = self.time if self.time is not None else now
        if self.fails >= 5:
            self.skip_until = now + 60                     # nobody home: look once a minute
        self.check_offline()

    def poll_history(self):
        if not self.enabled or self.hist_busy or self.status is None or self.clock() < self.hist_skip_until:
            return True
        self.hist_busy = True
        self._dish_call(starlink.history_request(), starlink.decode_history_reply, self.history_done)
        return True

    def history_done(self, h, error):
        self.hist_busy = False
        now = self.clock()
        if h is None:
            self.hist_error = error
            self.hist_skip_until = now + 300               # e.g. a firmware that answers differently: do not hammer it
            return
        self.hist_error, self.hist_time = None, now
        dish = self.store.data['dish']
        last = dish.get('current')
        cur = h['current']
        if last is not None and cur < last:                # the dish rebooted and counts from zero again
            last = None
        power = starlink.new_samples(h['power'], cur, last)
        down = starlink.new_samples(h['down'], cur, last)
        up = starlink.new_samples(h['up'], cur, last)
        self.store.add_dish_samples(now, power, down, up)
        self.store.add_outages(h['outages'])
        dish['current'] = cur
        self.store.dirty = True
        if power:
            self.power_now = power[-1]
        self.store.save()

    # ---------------------------------------------------------------- ICMP
    def poll_icmp(self):
        if not self.enabled or self.icmp_busy:
            return True
        self.icmp_busy = True
        pending = [len(self.targets)]

        def one(name, host):
            def done(rc, out):
                res = netmon.parse_ping(out.decode('utf-8', 'replace')) if rc is not None else None
                entry = self.icmp.setdefault(name, {'host': host, 'strip': collections.deque(maxlen=48)})
                entry['host'], entry['result'] = host, res
                entry['state'] = netmon.rtt_state(res, self.icmp_good, self.icmp_warn)
                entry['strip'].append((int(self.clock()), None if res is None else res['avg'], None if res is None else res['loss']))
                pending[0] -= 1
                if pending[0] <= 0:
                    self.icmp_busy = False
            run_async(['ping', '-c', '2', '-W', '2', host], done, timeout=8)
        for name, host in self.targets:
            one(name, host)
        return True

    # ---------------------------------------------------------- speed test
    def start_speedtest(self, manual=True):
        if self.test['running']:
            return False
        self.test = {'running': True, 'step': 'starting', 'started': self.clock()}
        self.last_test_start = self.clock()
        self._test_attempt(1, {'bytes': 0})
        return True

    def maybe_speedtest(self):
        """Timer: start a scheduled test (never when the interval is 0)."""
        if self.speedtest_every and not self.test['running'] and self.clock() - self.last_test_start >= self.speedtest_every:
            self.start_speedtest(manual=False)
        return True

    def _test_attempt(self, attempt, acc):
        trace_url = self.test_server + '/cdn-cgi/trace'
        self.test['step'] = 'locating the test server'

        def got_trace(rc, out):
            trace = netmon.parse_trace(out.decode('utf-8', 'replace')) if rc == 0 else {}
            acc.update(colo=trace.get('colo'), ip=trace.get('ip'), loc=trace.get('loc'))
            self._test_latency(attempt, acc)
        run_async(['curl', '-s', '-m', '8', trace_url], got_trace, timeout=10)

    def _latency_cmd(self, count):
        url = self.test_server + '/__down?bytes=0'
        cmd = ['curl', '-s', '-m', '8']
        for _ in range(count):
            cmd += ['-o', '/dev/null', '-w', '%{time_starttransfer}\\n', url]
        return cmd

    @staticmethod
    def _times(out):
        vals = []
        for line in out.decode('utf-8', 'replace').split():
            try:
                vals.append(float(line) * 1000.0)
            except ValueError:
                pass
        return vals[1:] if len(vals) > 1 else vals        # the first includes the TLS handshake

    def _test_latency(self, attempt, acc):
        self.test['step'] = 'measuring latency'

        def got(rc, out):
            vals = self._times(out) if rc == 0 else []
            if vals:
                acc['latency_ms'] = sum(vals) / len(vals)
                acc['jitter_ms'] = netmon.jitter(vals)
            self._test_download(attempt, acc)
        run_async(self._latency_cmd(9), got, timeout=15)

    def _test_download(self, attempt, acc):
        self.test['step'] = 'download'
        size = self.test_down_mb * 1000000

        def got(rc, out):
            parts = out.decode('utf-8', 'replace').split()
            try:
                speed, nbytes = float(parts[0]), float(parts[1])
            except (IndexError, ValueError):
                speed = nbytes = 0.0
            acc['bytes'] += int(nbytes)
            acc['down_mbps'] = speed * 8 / 1e6 if rc == 0 and nbytes else None
            if acc['down_mbps'] is None:
                self._test_done(attempt, acc, 'download failed' + (' (timed out)' if rc is None else ''))
            else:
                self._test_upload(attempt, acc)
        run_async(['curl', '-s', '-m', '40', '-o', '/dev/null', '-w', '%{speed_download} %{size_download}',
                   self.test_server + '/__down?bytes=%d' % size], got, timeout=45)

        def got_loaded(rc, out):
            vals = self._times(out) if rc == 0 else []
            if vals:
                acc['loaded_ms'] = sum(vals) / len(vals)
        run_async(self._latency_cmd(6), got_loaded, timeout=20)          # latency while the line is busy

    def _upload_file(self):
        path = '/tmp/venusosmods-upload.bin'
        want = self.test_up_mb * 1000000
        try:
            if not os.path.exists(path) or os.path.getsize(path) != want:
                with open(path, 'wb') as f:
                    f.write(bytes(want))
        except OSError:
            return None
        return path

    def _test_upload(self, attempt, acc):
        self.test['step'] = 'upload'
        path = self._upload_file()
        if path is None:
            self._test_done(attempt, acc, 'could not write the upload file in /tmp')
            return

        def got(rc, out):
            parts = out.decode('utf-8', 'replace').split()
            try:
                speed, nbytes = float(parts[0]), float(parts[1])
            except (IndexError, ValueError):
                speed = nbytes = 0.0
            acc['bytes'] += int(nbytes)
            acc['up_mbps'] = speed * 8 / 1e6 if rc == 0 and nbytes else None
            self._test_done(attempt, acc, None if acc['up_mbps'] is not None else 'upload failed')
        run_async(['curl', '-s', '-m', '40', '-o', '/dev/null', '-X', 'POST', '--data-binary', '@' + path, '-w',
                   '%{speed_upload} %{size_upload}', self.test_server + '/__up'], got, timeout=45)

    @staticmethod
    def bloat_grade(idle, loaded):
        if idle is None or loaded is None:
            return None
        extra = max(0.0, loaded - idle)
        for limit, grade in ((5, 'A+'), (30, 'A'), (60, 'B'), (200, 'C'), (400, 'D')):
            if extra < limit:
                return grade
        return 'F'

    def _test_done(self, attempt, acc, error):
        now = self.clock()
        down = acc.get('down_mbps')
        if error is None and down is not None and down < self.min_sane and attempt < 2:
            self._test_attempt(attempt + 1, {'bytes': acc['bytes']})          # a result this low is probably a hiccup: once more
            return
        result = {'time': int(now), 'ok': error is None, 'down_mbps': down, 'up_mbps': acc.get('up_mbps'),
                  'latency_ms': acc.get('latency_ms'), 'jitter_ms': acc.get('jitter_ms'), 'loaded_ms': acc.get('loaded_ms'),
                  'bloat': self.bloat_grade(acc.get('latency_ms'), acc.get('loaded_ms')), 'colo': acc.get('colo'),
                  'ip': acc.get('ip'), 'bytes': acc['bytes'], 'error': error}
        self.store.add_test(result)
        self.store.add_test_bytes(now, acc['bytes'])
        self.store.save(force=True)
        self.test = {'running': False, 'step': '', 'started': None}
        if error and self.tg['kinds']['test']:
            self.alert('test', 'Speed test failed: %s' % error, cooldown=3600)

    # ----------------------------------------------------------------- WAN
    def poll_wan(self):
        if not self.wan_check:
            return True

        def got(rc, out):
            trace = netmon.parse_trace(out.decode('utf-8', 'replace')) if rc == 0 else {}
            if not trace.get('ip'):
                self.wan['error'] = 'no answer'
                return
            self.wan.update(ip=trace['ip'], colo=trace.get('colo'), loc=trace.get('loc'), time=int(self.clock()), error=None)
            previous = self.store.add_wan(trace['ip'], {'colo': trace.get('colo'), 'loc': trace.get('loc')})
            if previous and self.tg['kinds']['wan']:
                self.alert('wan', 'Public IP changed: %s -> %s' % (previous, trace['ip']), cooldown=0)
            self.store.save()
        run_async(['curl', '-s', '-m', '8', self.wan_url], got, timeout=10)
        return True

    # -------------------------------------------------------------- alerts
    def check_status_alerts(self, st):
        if self.tg['kinds']['hardware']:
            bad = set(a for a in (st.get('alerts') or []) if a not in INFO_ALERTS)
            if self.known_alerts is not None:
                for a in sorted(bad - self.known_alerts):
                    self.alert('hw-' + a, 'Starlink alert: %s' % a, cooldown=3600)
            self.known_alerts = bad
        if self.tg['kinds']['drop']:
            drop = (st.get('drop_rate') or 0) * 100
            self.drop_run = self.drop_run + 1 if drop >= self.tg['drop_pct'] else 0
            if self.drop_run >= 3:
                self.alert('drop', 'Starlink packet loss is %.1f %%.' % drop, cooldown=1800)

    def alert(self, key, text, cooldown=1800):
        now = self.clock()
        if cooldown and now - self.alert_state.get(key, 0) < cooldown:
            return False
        self.alert_state[key] = now
        if not self.tg['enabled'] or not self.tg['token'] or not self.tg['chat_id']:
            return False
        self.queue.append(text)
        del self.queue[:-20]
        self.flush()
        return True

    def flush(self):
        if not self.queue or self.sending:
            return True
        text = self.queue[0]
        self.sending = True
        cfg = 'url = "https://api.telegram.org/bot%s/sendMessage"\ndata = "chat_id=%s"\ndata-urlencode = "text=%s"\n' % (
            self.tg['token'], self.tg['chat_id'], text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' '))

        def done(rc, out):
            self.sending = False
            if rc == 0 and b'"ok":true' in out.replace(b' ', b''):
                self.queue.pop(0)
            else:
                log.warning('Telegram message not delivered, will retry')
        run_async(['curl', '-s', '-m', '10', '-K', '-'], done, input_bytes=cfg.encode(), timeout=14)
        return True

    def test_message(self):
        self.queue.append('Venus OS mods: test message.')
        self.flush()

    # ---------------------------------------------------------- the answers
    def summary(self):
        age = None if self.time is None else int(self.clock() - self.time)
        s = self.status or {}
        return {'enabled': self.enabled, 'reachable': self.error is None and self.status is not None,
                'error': self.error, 'age': age, 'via': self.via, 'state': s.get('state'),
                'down_bps': s.get('down_bps'), 'up_bps': s.get('up_bps'), 'latency_ms': s.get('latency_ms'),
                'obstructed_pct': s.get('obstructed_pct'), 'alerts': s.get('alerts'),
                'throttled': bool(s.get('throttled_down') or s.get('throttled_up'))}

    def icmp_view(self):
        out = []
        for name, host in self.targets:
            e = self.icmp.get(name)
            out.append({'name': name, 'host': host, 'state': e['state'] if e else 'stale',
                        'result': e.get('result') if e else None,
                        'strip': [list(x) for x in e['strip']] if e else []})
        return out

    def dashboard(self):
        tests = self.store.data['tests']
        ok = [t for t in tests if t.get('ok')]
        return {'tests': tests[-120:], 'last': tests[-1] if tests else None, 'last_ok': ok[-1] if ok else None,
                'previous_ok': ok[-2] if len(ok) > 1 else None, 'running': self.test, 'icmp': self.icmp_view(),
                'wan': self.wan, 'speedtest_every': self.speedtest_every,
                'icmp_good': self.icmp_good, 'icmp_warn': self.icmp_warn,
                'tg': {'enabled': self.tg['enabled']},
                'test_mb': self.test_down_mb + self.test_up_mb}

    def energy(self):
        e = netmon.energy_summary(self.store, self.price, now=self.clock())
        e['now_w'] = self.power_now
        e['price'] = self.price
        e['currency'] = self.currency
        return e

    def usage(self):
        u = netmon.usage_summary(self.store, self.cycle_day, self.cap_gb, now=self.clock())
        u['cycle_day'] = self.cycle_day
        return u

    def log_view(self, window_s):
        now = self.clock()
        since = now - window_s
        outages = [o for o in self.store.data['outages'] if o['start'] + o['duration_s'] >= since]
        gone = [u for u in self.store.data['unreachable'] if u['end'] >= since]
        return {'since': int(since), 'now': int(now), 'outages': outages[-200:], 'unreachable': gone[-200:],
                'stats': netmon.availability(self.store, window_s, now=now), 'wan': self.store.data['wan'][-50:],
                'first_seen': self.store.data['first_seen']}
