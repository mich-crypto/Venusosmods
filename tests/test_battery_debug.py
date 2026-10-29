"""Battery history for the debug graphs: pure Python, no D-Bus."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import battery_debug as bd  # noqa: E402


def pack(n=4, v=13.2, base=3.30, soc=80, fmt=1, spread=0.0):
    d = {'/Dc/0/Voltage': v, '/Dc/0/Current': -4.5, '/Dc/0/Power': -59.4, '/Soc': soc,
         '/Dc/0/Temperature': 21.0, '/System/MOSTemperature': 30.5,
         '/System/NrOfCellsPerBattery': n, '/Info/MaxChargeCurrent': 50.0,
         '/Alarms/LowVoltage': 0, '/Io/AllowToCharge': 1, '/Connected': 1, '/ProductName': 'JK BMS'}
    for i in range(1, n + 1):
        volts = base + (spread if i == 2 else 0)
        if fmt == 1:
            d['/Voltages/Cell%d' % i] = volts
            d['/Balances/Cell%d' % i] = 1 if i == 2 else 0
        else:
            d['/Cell/%d/Volts' % i] = volts
            d['/Cell/%d/Balance' % i] = 1 if i == 2 else 0
    return d


class ExtractTests(unittest.TestCase):
    def test_both_cell_formats(self):
        for fmt in (1, 2):
            s = bd.extract(pack(fmt=fmt, spread=0.02))
            self.assertEqual(s['cells'], [3.3, 3.32, 3.3, 3.3])
            self.assertEqual(s['bal'], [0, 1, 0, 0])
            self.assertEqual(s['diff'], 20.0)

    def test_scalars_and_no_battery(self):
        s = bd.extract(pack())
        self.assertEqual((s['v'], s['i'], s['soc'], s['mos']), (13.2, -4.5, 80, 30.5))
        self.assertNotIn('cmin', s)
        self.assertIsNone(bd.extract({'/Mgmt/ProcessName': 'x'}))

    def test_without_cell_data(self):
        d = pack()
        for k in [k for k in d if k.startswith(('/Voltages', '/Balances'))]:
            del d[k]
        s = bd.extract(d)
        self.assertNotIn('cells', s)
        self.assertEqual(s['v'], 13.2)

    def test_missing_cell_in_the_middle_keeps_its_place(self):
        d = pack(n=4)
        del d['/Voltages/Cell2']
        self.assertEqual(bd.extract(d)['cells'], [3.3, None, 3.3, 3.3])

    def test_details_filter(self):
        d = dict(pack(), **{'/Secret/Thing': 1, '/Mgmt/ProcessName': 'x'})
        out = bd.details(d)
        self.assertIn('/Alarms/LowVoltage', out)
        self.assertIn('/ProductName', out)
        self.assertNotIn('/Secret/Thing', out)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.now = 10000.0
        self.rec = bd.Recorder(interval=10, keep_s=100, clock=lambda: self.now)

    def feed(self, n, **kw):
        for i in range(n):
            self.rec.add('com.victronenergy.battery.ttyUSB0', pack(**kw), now=self.now)
            self.now += 10

    def test_keeps_only_the_newest_samples(self):
        self.feed(30)
        self.assertEqual(self.rec.info('com.victronenergy.battery.ttyUSB0')['samples'], 11)

    def test_history_window_and_decimation(self):
        rec = bd.Recorder(interval=10, keep_s=6 * 3600, clock=lambda: self.now)
        for i in range(1000):
            rec.add('b', pack(soc=i % 100), now=self.now)
            self.now += 10
        h = rec.history('b', window_s=3600, max_points=100)
        self.assertLessEqual(len(h['t']), 100)
        self.assertEqual(len(h['series']['soc']), len(h['t']))
        self.assertEqual(len(h['cells']), 4)
        self.assertEqual(len(h['cells'][0]), len(h['t']))
        self.assertEqual(h['bal'][1][0], 1)
        short = rec.history('b', window_s=120, max_points=100)
        self.assertLessEqual(len(short['t']), 13)

    def test_unknown_service_and_cleanup(self):
        self.assertEqual(self.rec.history('nope')['t'], [])
        self.feed(2)
        self.rec.forget_missing(['com.victronenergy.battery.ttyUSB0'])
        self.assertEqual(len(self.rec.services()), 1)
        self.now += 1000
        self.rec.forget_missing([])
        self.assertEqual(self.rec.services(), [])


if __name__ == '__main__':
    unittest.main()
