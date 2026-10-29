"""The rolling raw-data log."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import fakes  # noqa: E402,F401
import rawlog  # noqa: E402


class RawLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_write_read_clear(self):
        log = rawlog.RawLog('gas', self.dir)
        log.write('adv', 'AA:BB data=0102')
        log.write('tx', 'query f0')
        lines = rawlog.read_tail('gas', 10, self.dir)
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} adv    AA:BB data=0102$')
        self.assertEqual(rawlog.read_tail('gas', 1, self.dir), [lines[1]])
        rawlog.clear('gas', self.dir)
        self.assertEqual(rawlog.read_tail('gas', 10, self.dir), [])
        self.assertEqual(rawlog.read_tail('missing', 10, self.dir), [])

    def test_file_is_cut_to_its_newest_half(self):
        log = rawlog.RawLog('fridge', self.dir, max_bytes=2000)
        for i in range(200):
            log.write('rx', 'chunk %04d' % i)
        size = os.path.getsize(os.path.join(self.dir, 'fridge.log'))
        self.assertLess(size, 2100)
        lines = rawlog.read_tail('fridge', 1000, self.dir)
        self.assertTrue(lines[-1].endswith('chunk 0199'))
        self.assertGreater(len(lines), 20)

    def test_unwritable_folder_never_raises(self):
        blocked = os.path.join(self.dir, 'file')
        open(blocked, 'w').close()
        rawlog.RawLog('gas', os.path.join(blocked, 'sub')).write('adv', 'x')      # a file in the way

    def test_tail_of_a_long_file(self):
        log = rawlog.RawLog('water', self.dir, max_bytes=10 ** 9)
        for i in range(2000):
            log.write('adv', 'n=%d' % i)
        lines = rawlog.read_tail('water', 50, self.dir)
        self.assertEqual(len(lines), 50)
        self.assertTrue(lines[-1].endswith('n=1999') and lines[0].endswith('n=1950'))


if __name__ == '__main__':
    unittest.main()
