"""The Starlink hub with a fake command runner: no curl, no ping, no clock."""

import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import fakes  # noqa: E402,F401
import netmon  # noqa: E402
import starlink  # noqa: E402
import starlink_hub  # noqa: E402
import test_starlink as ts  # noqa: E402

PING_OK = b'--- x ping statistics ---\n2 packets transmitted, 2 packets received, 0% packet loss\nround-trip min/avg/max = 10.0/12.0/14.0 ms\n'
PING_LOST = b'--- x ping statistics ---\n2 packets transmitted, 0 packets received, 100% packet loss\n'


class Runner(object):
    """Stands in for run_async: remembers the commands, the test answers them by hand."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, callback, input_bytes=None, timeout=10):
        self.calls.append({'argv': argv, 'cb': callback, 'input': input_bytes})

    def pop(self, match=None):
        for i, c in enumerate(self.calls):
            if match is None or match(c['argv']):
                return self.calls.pop(i)
        raise AssertionError('no such command: %s' % [c['argv'][:3] for c in self.calls])

    def answer(self, rc, out=b'', match=None):
        c = self.pop(match)
        c['cb'](rc, out)
        return c


class HubTests(unittest.TestCase):
    def setUp(self):
        self.runner = Runner()
        self.saved = starlink_hub.run_async
        starlink_hub.run_async = self.runner
        self.addCleanup(lambda: setattr(starlink_hub, 'run_async', self.saved))
        self.now = [1_700_000_000.0]
        self.dir = tempfile.mkdtemp()

    def hub(self, cfg=None, tg=None):
        store = netmon.Store(os.path.join(self.dir, 's.json'), clock=lambda: self.now[0])
        return starlink_hub.Hub(dict({'host': '10.9.9.9', 'port': '9201', 'interval': '5'}, **(cfg or {})), tg or {},
                                store, clock=lambda: self.now[0])

    def good_status(self, **kw):
        return sl_frame(ts.status_message(**kw))

    # ------------------------------------------------------------ the dish
    def test_status_good_then_remembered_route(self):
        h = self.hub()
        h.poll_status()
        c = self.runner.answer(0, self.good_status(alerts=(3,)))
        self.assertEqual(c['input'], starlink.status_request())
        self.assertIn('http://10.9.9.9:9201/SpaceX.API.Device.Device/Handle', c['argv'])
        self.assertEqual((h.status['state'], h.via, h.error, h.busy), ('Connected', 'gRPC-web port 9201', None, False))
        self.assertEqual(h.summary()['alerts'], ['thermal throttle'])

    def test_throttled_only_for_a_real_limit(self):
        h = self.hub()
        h.poll_status()
        self.runner.answer(0, starlink.frame(ts.sub(2004, ts.f32(1007, 1e6), ts.vint(1044, 1), ts.vint(1045, 1))))      # "no limit"
        self.assertEqual((h.summary()['throttled'], h.summary()['limit']), (False, None))
        h.poll_status()
        self.runner.answer(0, starlink.frame(ts.sub(2004, ts.f32(1007, 1e6), ts.vint(1044, 5))))
        self.assertEqual((h.summary()['throttled'], h.summary()['limit']), (True, 'overage limit'))

    def test_falls_back_to_the_next_way_and_sticks_with_it(self):
        h = self.hub()
        h.poll_status()
        self.runner.answer(7)                                      # port 9201: connection refused
        self.runner.answer(0, self.good_status())                  # port 80 works
        self.assertEqual((h.via, h.method), ('gRPC-web port 80', 1))
        h.poll_status()
        self.assertIn('http://10.9.9.9/SpaceX.API.Device.Device/Handle', self.runner.calls[0]['argv'])

    def test_all_ways_fail_then_back_off(self):
        h = self.hub()
        for _ in range(5):
            h.poll_status()
            for _ in range(3):
                self.runner.answer(7)
            self.now[0] += 5
        self.assertEqual((h.fails, h.busy), (5, False))
        self.assertIn('curl exit code 7', h.error)
        self.assertFalse(h.summary()['reachable'])
        self.runner.calls.clear()
        h.poll_status()
        self.assertEqual(self.runner.calls, [])                    # quiet minute
        self.now[0] += 61
        h.poll_status()
        self.assertEqual(len(self.runner.calls), 1)

    def test_disabled_never_asks(self):
        h = self.hub({'enabled': 'no'})
        h.poll_status()
        h.poll_history()
        h.poll_icmp()
        self.assertEqual(self.runner.calls, [])

    def test_unreachable_time_is_recorded_and_alerts_go_out(self):
        h = self.hub(tg={'enabled': 'yes', 'token': 'TOKEN123', 'chat_id': '42', 'offline_min': '1'})
        h.poll_status()
        self.runner.answer(0, self.good_status())
        for _ in range(14):                                         # 70 s of failures
            self.now[0] += 5
            h.poll_status()
            for _ in range(3):
                if any('SpaceX' in ' '.join(c['argv']) for c in self.runner.calls):
                    self.runner.answer(7, match=lambda argv: 'SpaceX' in ' '.join(argv))
        sent = [c for c in self.runner.calls if '-K' in c['argv']]
        self.assertEqual(len(sent), 1)
        self.assertIn(b'TOKEN123', sent[0]['input'])
        self.assertNotIn('TOKEN123', ' '.join(sent[0]['argv']))     # the token never shows in `ps`
        self.assertIn(b'not reachable', sent[0]['input'])
        sent[0]['cb'](0, b'{"ok":true,"result":{}}')
        self.assertEqual(h.queue, [])
        h.skip_until = 0
        h.poll_status()
        self.runner.answer(0, self.good_status(), match=lambda argv: 'SpaceX' in ' '.join(argv))
        self.assertEqual(len(h.store.data['unreachable']), 1)
        self.assertTrue(any(b'reachable again' in c['input'] for c in self.runner.calls if '-K' in c['argv']))

    def test_telegram_failure_is_retried(self):
        h = self.hub(tg={'enabled': 'yes', 'token': 'T', 'chat_id': '1'})
        h.alert('x', 'hello', cooldown=0)
        self.runner.answer(0, b'{"ok":false}')
        self.assertEqual(h.queue, ['hello'])
        h.flush()
        self.runner.answer(0, b'{"ok": true}')
        self.assertEqual(h.queue, [])

    def test_alert_cooldown_and_hardware_alerts(self):
        h = self.hub(tg={'enabled': 'yes', 'token': 'T', 'chat_id': '1'})
        h.poll_status()
        self.runner.answer(0, self.good_status())                   # first look: nothing is "new"
        self.assertEqual([c for c in self.runner.calls if '-K' in c['argv']], [])
        h.poll_status()
        self.runner.answer(0, self.good_status(alerts=(3, 7)))      # thermal throttle (+ roaming: information only)
        msgs = [c['input'] for c in self.runner.calls if '-K' in c['argv']]
        self.assertEqual(len(msgs), 1)
        self.assertIn(b'thermal throttle', msgs[0])
        self.assertFalse(h.alert('hw-thermal throttle', 'again', cooldown=3600))

    # ------------------------------------------------------------- history
    def test_history_counts_new_seconds_once(self):
        h = self.hub()
        h.status = {'state': 'Connected'}
        h.poll_history()
        self.assertEqual(self.runner.pop()['input'], starlink.history_request())
        h.hist_busy = False
        h.history_done(starlink.decode_history(ts.history_message(current=1000)), None)
        d = h.store.data['days'][netmon.day_key(self.now[0])]
        self.assertAlmostEqual(d['wh'], 30.0 / 3600)                 # first look: one second only
        self.now[0] += 60
        h.history_done(starlink.decode_history(ts.history_message(current=1060)), None)
        self.assertAlmostEqual(d['wh'], 30.0 * 61 / 3600)
        h.history_done(starlink.decode_history(ts.history_message(current=1060)), None)   # nothing new
        self.assertAlmostEqual(d['wh'], 30.0 * 61 / 3600)
        h.history_done(starlink.decode_history(ts.history_message(current=20)), None)     # the dish rebooted
        self.assertAlmostEqual(d['wh'], 30.0 * 62 / 3600)
        self.assertEqual(h.store.data['dish']['current'], 20)
        self.assertEqual(h.power_now, 30.0)

    def test_history_remembers_where_it_stopped_across_restarts(self):
        h = self.hub()
        h.history_done(starlink.decode_history(ts.history_message(current=500)), None)
        h.store.save(force=True)
        h2 = self.hub()                                              # the page restarted
        h2.history_done(starlink.decode_history(ts.history_message(current=530)), None)
        d = h2.store.data['days'][netmon.day_key(self.now[0])]
        self.assertAlmostEqual(d['wh'], 30.0 * 31 / 3600)

    def test_history_error_backs_off(self):
        h = self.hub()
        h.status = {}
        h.hist_busy = True
        h.history_done(None, 'gRPC-web port 9201: the reply has no dish history')
        self.assertIn('no dish history', h.hist_error)
        self.runner.calls.clear()
        h.poll_history()
        self.assertEqual(self.runner.calls, [])

    def test_outages_are_collected(self):
        h = self.hub()
        msg = ts.history_message(current=900, outages=[(6, int((self.now[0] - 60) * 1e9), 7 * 10 ** 9)])
        h.history_done(starlink.decode_history(msg), None)
        self.assertEqual(h.log_view(86400)['outages'][0]['cause'], 'obstructed')

    # ---------------------------------------------------------------- ICMP
    def test_icmp_tiles(self):
        h = self.hub({'icmp_targets': 'Cloudflare=1.1.1.1, Dish=192.168.100.1'})
        h.poll_icmp()
        self.assertEqual(len(self.runner.calls), 2)
        self.runner.answer(0, PING_OK, match=lambda a: '1.1.1.1' in a)
        self.runner.answer(0, PING_LOST, match=lambda a: '192.168.100.1' in a)
        v = h.icmp_view()
        self.assertEqual([(t['name'], t['state']) for t in v], [('Cloudflare', 'good'), ('Dish', 'down')])
        self.assertEqual(v[0]['strip'][0][1], 12.0)
        self.assertFalse(h.icmp_busy)
        self.assertEqual(starlink_hub.parse_targets('')[0][0], 'Cloudflare')
        self.assertEqual(starlink_hub.parse_targets('a b, -x=-evil, ok=1.2.3.4'), [('ok', '1.2.3.4')])

    # ---------------------------------------------------------- speed test
    def run_test(self, h, down=b'11250000 25000000', up=b'2000000 8000000', trace=None):
        self.assertTrue(h.start_speedtest())
        self.assertFalse(h.start_speedtest())                        # one at a time
        self.runner.answer(0, trace or b'ip=203.0.113.7\ncolo=AMS\nloc=NL\n')
        lat = b'0.120\n0.030\n0.032\n0.028\n0.030\n0.031\n0.029\n0.030\n0.030\n'
        self.runner.answer(0, lat)
        self.runner.answer(0, down, match=lambda a: any('__down?bytes=25000000' in x for x in a))
        loaded = b'0.2\n0.1\n0.1\n0.1\n0.1\n0.1\n'
        self.runner.answer(0, loaded)
        self.runner.answer(0, up, match=lambda a: 'https://speed.cloudflare.com/__up' in a)

    def test_speed_test_full_run(self):
        h = self.hub()
        self.run_test(h)
        t = h.store.data['tests'][-1]
        self.assertTrue(t['ok'])
        self.assertAlmostEqual(t['down_mbps'], 90.0)
        self.assertAlmostEqual(t['up_mbps'], 16.0)
        self.assertEqual((t['colo'], t['ip']), ('AMS', '203.0.113.7'))
        self.assertAlmostEqual(t['latency_ms'], 30.0, places=0)
        self.assertEqual(t['bloat'], 'C')                           # 100 ms loaded vs 30 idle: +70 ms is a C
        self.assertEqual(t['bytes'], 33000000)
        self.assertFalse(h.test['running'])
        self.assertEqual(h.usage()['test_bytes'], 33000000)

    def test_slow_result_is_retried_once_then_kept(self):
        h = self.hub()
        h.start_speedtest()
        self.runner.answer(0, b'ip=1.2.3.4\ncolo=AMS\n')
        self.runner.answer(0, b'0.1\n0.03\n')
        self.runner.answer(0, b'100000 25000000', match=lambda a: any('__down' in x for x in a if 'bytes=' in x))
        self.runner.answer(0, b'0.1\n')
        self.runner.answer(0, b'100000 8000000', match=lambda a: '__up' in ' '.join(a))
        self.assertTrue(h.test['running'])                          # 0.8 Mbps is below the sane minimum: second try
        self.assertEqual(self.runner.calls[0]['argv'][:2], ['curl', '-s'])
        self.assertEqual(len(h.store.data['tests']), 0)

    def test_failed_test_is_recorded_as_a_failure(self):
        h = self.hub(tg={'enabled': 'yes', 'token': 'T', 'chat_id': '1'})
        h.start_speedtest()
        self.runner.answer(28)                                      # trace times out
        self.runner.answer(28)
        self.runner.answer(28, match=lambda a: any('__down' in x for x in a if 'bytes=' in x))
        self.runner.answer(28)
        t = h.store.data['tests'][-1]
        self.assertEqual((t['ok'], t['down_mbps']), (False, None))
        self.assertIn('download failed', t['error'])
        self.assertTrue(any(b'Speed test failed' in c['input'] for c in self.runner.calls if '-K' in c['argv']))

    def test_scheduled_tests_only_when_asked_for(self):
        h = self.hub()
        h.maybe_speedtest()
        self.assertEqual(self.runner.calls, [])
        h = self.hub({'speedtest_interval': '30'})
        h.maybe_speedtest()
        self.assertTrue(h.test['running'])
        self.assertEqual(starlink_hub.Hub.bloat_grade(30, 33), 'A+')
        self.assertEqual(starlink_hub.Hub.bloat_grade(30, 800), 'F')
        self.assertIsNone(starlink_hub.Hub.bloat_grade(None, 5))

    # ----------------------------------------------------------------- WAN
    def test_wan_change_alert(self):
        h = self.hub(tg={'enabled': 'yes', 'token': 'T', 'chat_id': '1'})
        h.poll_wan()
        self.runner.answer(0, b'ip=203.0.113.7\ncolo=AMS\n')
        self.assertEqual(h.wan['ip'], '203.0.113.7')
        self.assertEqual([c for c in self.runner.calls if '-K' in c['argv']], [])    # the first one is no change
        h.poll_wan()
        self.runner.answer(0, b'ip=203.0.113.9\ncolo=FRA\n')
        self.assertTrue(any(b'203.0.113.7 -> 203.0.113.9' in c['input'] for c in self.runner.calls if '-K' in c['argv']))
        self.assertEqual([w['ip'] for w in h.store.data['wan']], ['203.0.113.7', '203.0.113.9'])

    def test_views(self):
        h = self.hub({'price_per_kwh': '0.3', 'cap_gb': '50', 'cycle_day': '10'})
        self.assertEqual(h.energy()['currency'], 'EUR')
        self.assertIn('cost', h.energy())
        self.assertEqual((h.usage()['cycle_day'], h.usage()['cap_bytes']), (10, 50e9))
        self.assertIn('stats', h.log_view(86400))
        self.assertEqual(h.dashboard()['icmp'][0]['state'], 'stale')


def sl_frame(message):
    return starlink.frame(message)


if __name__ == '__main__':
    unittest.main()
