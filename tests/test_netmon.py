"""Bookkeeping behind the Starlink tabs."""

import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import netmon  # noqa: E402

BUSYBOX = """PING 192.168.100.1 (192.168.100.1): 56 data bytes
64 bytes from 192.168.100.1: seq=0 ttl=63 time=4.383 ms

--- 192.168.100.1 ping statistics ---
2 packets transmitted, 2 packets received, 0% packet loss
round-trip min/avg/max = 4.383/9.965/15.548 ms
"""
IPUTILS = """--- 1.1.1.1 ping statistics ---
4 packets transmitted, 3 received, 25% packet loss, time 3004ms
rtt min/avg/max/mdev = 20.1/25.5/30.9/4.4 ms
"""
ALL_LOST = "--- x ping statistics ---\n3 packets transmitted, 0 packets received, 100% packet loss\n"


def at(y, m, d, h=12):
    return time.mktime((y, m, d, h, 0, 0, 0, 0, -1))


class ParseTests(unittest.TestCase):
    def test_ping(self):
        self.assertEqual(netmon.parse_ping(BUSYBOX), {'loss': 0.0, 'min': 4.383, 'avg': 9.965, 'max': 15.548})
        self.assertEqual(netmon.parse_ping(IPUTILS)['loss'], 25.0)
        self.assertEqual(netmon.parse_ping(IPUTILS)['avg'], 25.5)
        lost = netmon.parse_ping(ALL_LOST)
        self.assertEqual((lost['loss'], lost['avg']), (100.0, None))
        self.assertIsNone(netmon.parse_ping('ping: bad address'))

    def test_trace(self):
        t = netmon.parse_trace('fl=1f\nip=203.0.113.7\ncolo=AMS\nloc=NL\n')
        self.assertEqual((t['ip'], t['colo']), ('203.0.113.7', 'AMS'))

    def test_states(self):
        s = netmon.rtt_state
        self.assertEqual(s({'loss': 0, 'avg': 20}), 'good')
        self.assertEqual(s({'loss': 0, 'avg': 70}), 'warn')
        self.assertEqual(s({'loss': 0, 'avg': 150}), 'bad')
        self.assertEqual(s({'loss': 30, 'avg': 10}), 'bad')                 # loss wins over latency
        self.assertEqual(s({'loss': 100, 'avg': None}), 'down')
        self.assertEqual(s(None), 'down')

    def test_jitter(self):
        self.assertEqual(netmon.jitter([10, 12, 9]), 2.5)
        self.assertIsNone(netmon.jitter([10]))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'stats.json')
        self.now = at(2026, 10, 29)
        self.clock = lambda: self.now

    def store(self):
        return netmon.Store(self.path, clock=self.clock, save_every=300)

    def test_energy_and_usage(self):
        st = self.store()
        st.add_dish_samples(self.now, [30.0] * 3600, [8e6] * 3600, [1e6] * 3600)          # one hour
        e = netmon.energy_summary(st, price=0.30, now=self.now)
        self.assertAlmostEqual(e['today_wh'], 30.0, places=3)
        self.assertAlmostEqual(e['month_wh'], 30.0, places=3)
        self.assertAlmostEqual(e['avg_w'], 30.0, places=3)
        self.assertAlmostEqual(e['cost']['today'], 0.009, places=5)
        self.assertEqual((e['peak_w'], e['idle_w']), (30.0, 30.0))
        self.assertAlmostEqual(e['projected_month_wh'], 30.0 * 31, places=2)
        u = netmon.usage_summary(st, cycle_day=1, cap_gb=1, now=self.now)
        self.assertAlmostEqual(u['down'], 3600 * 8e6 / 8)
        self.assertAlmostEqual(u['cap_used'], (3600 * 9e6 / 8) / 1e9, places=6)

    def test_cycle_start(self):
        self.assertEqual(time.localtime(netmon.cycle_start(at(2026, 10, 29), 15))[:3], (2026, 10, 15))
        self.assertEqual(time.localtime(netmon.cycle_start(at(2026, 10, 3), 15))[:3], (2026, 9, 15))
        self.assertEqual(time.localtime(netmon.cycle_start(at(2026, 1, 3), 15))[:3], (2025, 12, 15))

    def test_usage_only_counts_the_current_cycle(self):
        st = self.store()
        st.add_dish_samples(at(2026, 9, 20), [10.0], [8e9], [0.0])               # last cycle
        st.add_dish_samples(at(2026, 10, 20), [10.0], [8e9], [0.0])
        st.add_test_bytes(at(2026, 10, 20), 5e6)
        u = netmon.usage_summary(st, cycle_day=15, now=at(2026, 10, 29))
        self.assertAlmostEqual(u['down'], 1e9)
        self.assertEqual(u['test_bytes'], 5e6)

    def test_persistence_is_atomic_and_throttled(self):
        st = self.store()
        st.add_test({'time': 1, 'ok': True, 'down_mbps': 100})
        self.assertTrue(st.save(force=True))
        self.assertFalse(st.save())                                      # nothing new
        st.add_test({'time': 2, 'ok': False})
        self.assertFalse(st.save())                                      # too soon
        self.now += 400
        self.assertTrue(st.save())
        again = self.store()
        self.assertEqual([t['time'] for t in again.data['tests']], [1, 2])
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_broken_file_starts_fresh(self):
        with open(self.path, 'w') as f:
            f.write('{not json')
        self.assertEqual(self.store().data['tests'], [])

    def test_outages_are_not_added_twice(self):
        st = self.store()
        o = {'cause': 'obstructed', 'start_ns': int((self.now - 100) * 1e9), 'duration_s': 5.0, 'switched': False}
        self.assertEqual(st.add_outages([o]), 1)
        self.assertEqual(st.add_outages([o]), 0)
        self.assertEqual(len(st.data['outages']), 1)

    def test_availability_ignores_time_the_dish_was_unreachable(self):
        st = self.store()
        st.data['first_seen'] = int(self.now - 1000)
        st.add_outages([{'cause': 'obstructed', 'start_ns': int((self.now - 500) * 1e9), 'duration_s': 50.0, 'switched': False}])
        st.add_unreachable(self.now - 900, self.now - 800)
        a = netmon.availability(st, 86400, now=self.now)
        self.assertEqual((a['count'], round(a['outage_s'])), (1, 50))
        self.assertAlmostEqual(a['unreachable_s'], 100.0)
        self.assertAlmostEqual(a['availability'], 1 - 50 / 900.0, places=4)
        self.assertEqual(a['causes'], {'obstructed': 50.0})

    def test_wan_history_records_changes_only(self):
        st = self.store()
        self.assertEqual(st.add_wan('203.0.113.7', {'colo': 'AMS'}), '')
        self.assertIsNone(st.add_wan('203.0.113.7'))
        self.assertEqual(st.add_wan('203.0.113.9'), '203.0.113.7')
        self.assertEqual([w['ip'] for w in st.data['wan']], ['203.0.113.7', '203.0.113.9'])

    def test_old_days_are_trimmed(self):
        st = self.store()
        for i in range(450):
            st.add_dish_samples(self.now - i * 86400, [1.0], [0.0], [0.0])
        self.assertEqual(len(st.data['days']), netmon.KEEP_DAYS)


if __name__ == '__main__':
    unittest.main()
