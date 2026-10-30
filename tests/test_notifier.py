import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'data', 'shared_drivers'))

import notifier  # noqa: E402


class Sink(object):
    def __init__(self):
        self.t = 0
        self.sent = []

    def clock(self):
        return self.t

    def send(self, key, text, cooldown):
        self.sent.append(text)
        return True

    def make(self, **cfg):
        return notifier.Notifier(cfg, self.send, clock=self.clock)


def bat(soc):
    return {'batteries': [{'name': 'JK BMS', 'soc': soc}]}


class BatteryTests(unittest.TestCase):
    def test_warning_then_alarm_then_quiet_until_recovered(self):
        s = Sink()
        n = s.make()
        for soc in (60, 25, 21):
            n.check(bat(soc))
        self.assertEqual(s.sent, [])
        n.check(bat(19))
        self.assertEqual(len(s.sent), 1)
        self.assertIn('getting low', s.sent[0])
        n.check(bat(18))
        self.assertEqual(len(s.sent), 1)                       # still the same level: quiet
        n.check(bat(9))
        self.assertEqual(len(s.sent), 2)
        self.assertIn('almost empty', s.sent[1])
        s.t += 3600
        n.check(bat(8))
        self.assertEqual(len(s.sent), 2)
        s.t += 3 * 3600
        n.check(bat(8))
        self.assertEqual(len(s.sent), 3)                       # a battery that stays empty is repeated
        n.check(bat(22))                                       # within 5 % of the limit: not recovered yet
        n.check(bat(19))
        self.assertEqual(len(s.sent), 3)
        n.check(bat(40))
        n.check(bat(15))
        self.assertEqual(len(s.sent), 4)                       # and a new dip is reported again

    def test_custom_thresholds_and_switch(self):
        s = Sink()
        n = s.make(battery_warn_pct='50', battery_alarm_pct='30')
        n.check(bat(45))
        self.assertIn('getting low', s.sent[-1])
        n = s.make(alert_battery='no')
        s.sent[:] = []
        n.check(bat(3))
        self.assertEqual(s.sent, [])


class AlarmTests(unittest.TestCase):
    def alarm(self, value, path='/Alarms/LowVoltage'):
        return {'alarms': [{'device': 'JK BMS', 'path': path, 'value': value}] if value is not None else []}

    def test_confirmed_then_cleared(self):
        s = Sink()
        n = s.make()
        n.check(self.alarm(1))
        self.assertEqual(s.sent, [])                           # one look is a blip
        n.check(self.alarm(1))
        self.assertEqual(s.sent, ['🟠 JK BMS: Low voltage (warning)'])
        n.check(self.alarm(1))
        self.assertEqual(len(s.sent), 1)
        n.check(self.alarm(2))
        self.assertEqual(s.sent[-1], '🔴 JK BMS: Low voltage (alarm)')
        n.check(self.alarm(None))
        self.assertIn('back to normal', s.sent[-1])
        self.assertEqual(n.view()['active_alarms'], [])

    def test_a_blip_is_never_reported(self):
        s = Sink()
        n = s.make()
        n.check(self.alarm(2))
        n.check(self.alarm(None))
        self.assertEqual(s.sent, [])

    def test_switch_off(self):
        s = Sink()
        n = s.make(alert_alarms='no')
        n.check(self.alarm(2))
        n.check(self.alarm(2))
        self.assertEqual(s.sent, [])

    def test_pretty(self):
        self.assertEqual(notifier.pretty('/Alarms/HighChargeCurrent'), 'High charge current')
        self.assertEqual(notifier.pretty('/Alarm'), 'Alarm')


class FenceTests(unittest.TestCase):
    HOME = dict(alert_geofence='yes', fence_lat='52.000000', fence_lon='5.000000', fence_m='200')

    def at(self, metres_north):
        return {'gps': {'lat': 52.0 + metres_north / 111195.0, 'lon': 5.0}}

    def test_distance(self):
        self.assertAlmostEqual(notifier.distance_m(52, 5, 52.001, 5), 111.2, delta=0.5)

    def test_leaving_and_returning(self):
        s = Sink()
        n = s.make(**self.HOME)
        n.check(self.at(10))
        n.check(self.at(150))
        self.assertEqual(s.sent, [])
        n.check(self.at(260))
        self.assertEqual(s.sent, [])                           # GPS jitter: it has to hold for two looks
        n.check(self.at(270))
        self.assertEqual(len(s.sent), 1)
        self.assertIn('left its 200 m area', s.sent[0])
        self.assertIn('maps.google.com/?q=52.00', s.sent[0])
        n.check(self.at(400))
        self.assertEqual(len(s.sent), 1)
        s.t += 700
        n.check(self.at(800))
        self.assertIn('Still away', s.sent[-1])                # an update every ten minutes while it is away
        n.check(self.at(20))
        n.check(self.at(20))
        self.assertIn('back inside', s.sent[-1])

    def test_nothing_without_a_spot_a_fix_or_the_switch(self):
        s = Sink()
        n = s.make(alert_geofence='yes')
        n.check(self.at(5000))
        n.check(self.at(5000))
        n = s.make(**self.HOME)
        n.check({'gps': None})
        n.check({})
        n = s.make(**dict(self.HOME, alert_geofence='no'))
        n.check(self.at(5000))
        n.check(self.at(5000))
        self.assertEqual(s.sent, [])

    def test_set_home_uses_the_current_position(self):
        s = Sink()
        n = s.make(alert_geofence='yes')
        n.set_home(52.0, 5.0)
        n.check(self.at(10))
        n.check(self.at(500))
        n.check(self.at(500))
        self.assertEqual(len(s.sent), 1)


class OrionTests(unittest.TestCase):
    def orion(self, state, mode=1, volts=13.8, amps=20.0):
        return {'dcdc': [{'name': 'Orion XS', 'state': state, 'mode': mode, 'volts': volts, 'amps': amps}]}

    def test_switching_on(self):
        s = Sink()
        n = s.make()
        n.check(self.orion(0, mode=4))                          # first look: not a change
        n.check(self.orion(0, mode=4))
        self.assertEqual(s.sent, [])
        n.check(self.orion(3))
        self.assertEqual(s.sent, ['⚡ Orion XS: charging was switched on, bulk (13.8 V, 20.0 A).'])
        n.check(self.orion(4))
        n.check(self.orion(5))
        self.assertEqual(len(s.sent), 1)
        n.check(self.orion(0, mode=4))
        n.check(self.orion(3))
        self.assertEqual(len(s.sent), 2)

    def test_the_mode_switch_counts_too_and_it_can_be_turned_off(self):
        s = Sink()
        n = s.make()
        n.check(self.orion(1, mode=4))
        n.check(self.orion(3, mode=1))
        self.assertEqual(len(s.sent), 1)
        s2 = Sink()
        n = s2.make(alert_orion='no')
        n.check(self.orion(0, mode=4))
        n.check(self.orion(3))
        self.assertEqual(s2.sent, [])


class RecordTests(unittest.TestCase):
    def test_only_what_was_sent_is_recorded(self):
        rec = []
        n = notifier.Notifier({}, lambda k, t, c: False, record=rec.append)
        n.check(bat(5))
        self.assertEqual(rec, [])
        n = notifier.Notifier({}, lambda k, t, c: True, record=rec.append)
        n.check(bat(5))
        self.assertEqual(len(rec), 1)


if __name__ == '__main__':
    unittest.main()
