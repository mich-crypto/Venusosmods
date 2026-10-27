"""Test helpers shared by the unit and integration tests."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'shared_drivers'))

import mopeka_std_parser  # noqa: E402


def mopeka_packet(pairs, sensor_type=0x02, raw_voltage=128, raw_temp=36):
    """Pack 12 (raw_time, amplitude) pairs the way the sensor's C bitfields do."""
    assert len(pairs) == 12
    data = bytearray([0x00, sensor_type, raw_voltage, raw_temp & 0x3F])
    for g in range(3):
        word = 0
        for i, (t, v) in enumerate(pairs[4 * g:4 * g + 4]):
            word |= (t & 0x1F) << (10 * i) | (v & 0x1F) << (10 * i + 5)
        data += word.to_bytes(5, 'little')
    data += bytes(4)   # trailing bytes not used by the decoder
    assert len(data) == mopeka_std_parser.DATA_LENGTH
    return bytes(data)
