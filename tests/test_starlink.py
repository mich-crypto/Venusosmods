"""Starlink dish status: hand-made protobuf in, readable numbers out."""

import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import starlink as sl  # noqa: E402


def f32(number, value):
    return sl.varint((number << 3) | 5) + struct.pack('<f', value)


def vint(number, value):
    return sl.varint(number << 3) + sl.varint(value)


def sub(number, *parts):
    return sl.field_bytes(number, b''.join(parts))


def text(number, value):
    return sl.field_bytes(number, value.encode())


def status_message(outage=None, obstructed=0.02, alerts=(), latency=31.5):
    parts = [
        sub(1, text(1, 'ut01000000-00000000-0012abcd'), text(2, 'rev3_proto2'), text(3, '2024.09.0.mr1234'),
            text(4, 'NL'), vint(12, 3)),
        sub(2, vint(1, 90061)),
        f32(1003, 0.0125), f32(1007, 184.5e6), f32(1008, 12.25e6), f32(1004 - 1004 + 1011, 12.5),
        sub(1004, f32(1, obstructed), vint(5, 0), f32(4, 3600.0)),
        sub(1015, vint(1, 1), vint(2, 14)),
        vint(1016, 1000), vint(1018, 1), vint(1021, 1),
        sub(1019, *[vint(i, 1) for i in range(1, 7)]),
    ]
    if latency is not None:
        parts.append(f32(1009, latency))
    if alerts:
        parts.append(sub(1005, *[vint(n, 1) for n in alerts]))
    if outage:
        parts.append(sub(1014, vint(1, outage[0]), vint(3, int(outage[1] * 1e9))))
    return sub(2004, *parts)


class ProtocolTests(unittest.TestCase):
    def test_request_is_the_expected_bytes(self):
        # field 1004, wire type 2, empty: tag varint(8034) = e2 3e, length 0, inside a gRPC frame
        self.assertEqual(sl.status_request(), b'\x00\x00\x00\x00\x03\xe2\x3e\x00')

    def test_frames_and_trailers(self):
        body = b'abc'
        trailer = b'grpc-status: 0\r\ngrpc-m: x\r\n'
        data = sl.frame(body) + b'\x80' + struct.pack('>I', len(trailer)) + trailer
        msgs, trailers = sl.split_frames(data)
        self.assertEqual((msgs, trailers['grpc-status']), ([b'abc'], '0'))
        with self.assertRaises(ValueError):
            sl.split_frames(sl.frame(body)[:-1])

    def test_varints_round_trip(self):
        for n in (0, 1, 127, 128, 300, 8034, 2 ** 40):
            self.assertEqual(sl._read_varint(sl.varint(n), 0)[0], n)

    def test_garbage_is_an_error_not_a_crash(self):
        for junk in (b'\xff\xff', b'\x0a\x10abc', b'\x0b'):
            with self.assertRaises(ValueError):
                sl.parse_message(junk)


class StatusTests(unittest.TestCase):
    def test_connected_dish(self):
        s = sl.decode_reply(sl.frame(status_message(alerts=(3, 7))))
        self.assertEqual(s['state'], 'Connected')
        self.assertAlmostEqual(s['down_bps'], 184.5e6, delta=100)
        self.assertAlmostEqual(s['latency_ms'], 31.5, places=3)
        self.assertAlmostEqual(s['drop_rate'], 0.0125, places=5)
        self.assertAlmostEqual(s['obstructed_pct'], 2.0, places=3)
        self.assertEqual((s['uptime_s'], s['software'], s['generation'], s['hardware']), (90061, '2024.09.0.mr1234', 3, 'rev3_proto2'))
        self.assertEqual(s['alerts'], ['roaming', 'thermal throttle'])
        self.assertEqual((s['gps_valid'], s['gps_sats'], s['eth_mbps'], s['update']), (True, 14, 1000, 'idle'))
        self.assertTrue(all(s['ready'].values()))
        self.assertIsNone(s['outage'])

    def test_outage_and_missing_latency(self):
        s = sl.decode_reply(sl.frame(status_message(outage=(6, 12.5), latency=None)))
        self.assertEqual(s['state'], 'Outage: obstructed')
        self.assertEqual(s['outage'], {'cause': 'obstructed', 'seconds': 12.5})
        self.assertIsNone(s['latency_ms'])

    def test_grpc_error_status(self):
        trailer = b'grpc-status: 12\r\ngrpc-message: nope\r\n'
        data = sl.frame(b'') + b'\x80' + struct.pack('>I', len(trailer)) + trailer
        with self.assertRaises(ValueError) as e:
            sl.decode_reply(data)
        self.assertIn('grpc-status 12', str(e.exception))

    def test_reply_without_status(self):
        with self.assertRaises(ValueError):
            sl.decode_reply(sl.frame(vint(1, 5)))
        with self.assertRaises(ValueError):
            sl.decode_reply(b'')

    def test_unknown_fields_are_ignored(self):
        s = sl.decode_reply(sl.frame(sub(2004, vint(9999, 5), f32(1007, 1e6), sub(7777, b'xx'))))
        self.assertEqual(s['down_bps'], 1e6)


class HistoryTests(unittest.TestCase):
    def test_window_and_decimation(self):
        now = [10000.0]
        h = sl.History(interval=5, keep_s=3600, clock=lambda: now[0])
        for i in range(600):
            h.add({'down_bps': i * 1e6, 'up_bps': 2e6, 'latency_ms': None, 'drop_rate': 0.01,
                   'obstructed_pct': 1.0}, now=now[0])
            now[0] += 5
        out = h.history(window_s=3600, max_points=100)
        self.assertLessEqual(len(out['t']), 100)
        self.assertNotIn('latency_ms', out['series'])               # never had a value
        self.assertEqual(len(out['series']['down_mbps']), len(out['t']))
        self.assertEqual(out['series']['up_mbps'][0], 2.0)
        self.assertEqual(h.history(window_s=1)['t'], [])


if __name__ == '__main__':
    unittest.main()
