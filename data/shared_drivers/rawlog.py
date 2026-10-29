"""
A small rolling log of the raw bytes the sensors send, for debugging.

Each driver appends lines to <dir>/<name>.log (in /tmp, so it costs no flash
writes and starts empty after a reboot); the settings page shows them. A file
is cut to its newest half when it passes `max_bytes`. Writing must never get in
the driver's way, so every error here is swallowed.
"""

import os
import time

DEFAULT_DIR = os.environ.get('VENUSOSMODS_RAWLOG_DIR', '/tmp/venusosmods-raw')
SOURCES = ('gas', 'fridge', 'water')


def stamp(now=None):
    now = time.time() if now is None else now
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)) + '.%03d' % int(now % 1 * 1000)


class RawLog(object):
    def __init__(self, name, directory=None, max_bytes=200000):
        self.path = os.path.join(directory or DEFAULT_DIR, name + '.log')
        self.max_bytes = max_bytes

    def write(self, kind, text):
        """One line: '<time> <kind> <text>'. kind is short, e.g. rx, tx, adv, status."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            line = '%s %-6s %s\n' % (stamp(), kind, text)
            with open(self.path, 'a') as f:
                f.write(line)
                size = f.tell()
            if size > self.max_bytes:
                self._cut()
        except OSError:
            pass

    def _cut(self):
        with open(self.path) as f:
            lines = f.readlines()
        keep = lines[len(lines) // 2:]
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            f.writelines(keep)
        os.replace(tmp, self.path)


def hexs(data):
    return bytes(data).hex()


def read_tail(name, lines=300, directory=None):
    """Newest `lines` lines of a source, oldest first ([] when nothing was logged yet)."""
    path = os.path.join(directory or DEFAULT_DIR, name + '.log')
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - lines * 400))
            data = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    out = data.splitlines()
    if size > lines * 400 and out:
        out = out[1:]                 # the first line may be cut in half
    return out[-lines:]


def clear(name, directory=None):
    try:
        os.remove(os.path.join(directory or DEFAULT_DIR, name + '.log'))
    except OSError:
        pass
