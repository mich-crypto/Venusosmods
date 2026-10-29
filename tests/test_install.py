"""install.sh and get.sh in a scratch folder: what a first-time user runs."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, '..')


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.data = os.path.join(self.t, 'data')
        self.service = os.path.join(self.t, 'service')
        os.makedirs(self.service)
        shutil.copytree(os.path.join(REPO, 'data', 'shared_drivers'), os.path.join(self.data, 'shared_drivers'),
                        ignore=shutil.ignore_patterns('__pycache__', 'state', 'config.ini'))
        shutil.copytree(os.path.join(REPO, 'data', 'etc'), os.path.join(self.data, 'etc'), ignore=shutil.ignore_patterns('__pycache__'))
        bindir = os.path.join(self.t, 'bin')
        os.makedirs(bindir)
        for name, body in (('svc', 'echo "svc $@" >> %s/svc.log' % self.t),
                           ('ip', 'echo "    inet 192.168.1.29/24 metric 100 brd 192.168.1.255 scope global wlan0"; '
                                  'echo "    inet 100.79.242.32/32 scope global tailscale0"; echo "    inet 127.0.0.1/8 scope host lo"')):
            path = os.path.join(bindir, name)
            with open(path, 'w') as f:
                f.write('#!/bin/sh\n%s\n' % body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ['PATH'], VENUS_DATA=self.data, VENUS_SERVICE_DIR=self.service)

    def run_install(self, *args):
        p = subprocess.run(['sh', os.path.join(self.data, 'shared_drivers', 'install.sh'), '--force'] + list(args), env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return p.returncode, p.stdout.decode()

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def config(self):
        return self.read(os.path.join(self.data, 'shared_drivers', 'config.ini'))

    def test_first_install_sets_up_only_the_settings_page(self):
        rc, out = self.run_install()
        self.assertEqual(rc, 0, out)
        text = self.config()
        pw = re.search(r'^password = (\S{10})$', text, re.M)
        self.assertTrue(pw, text)
        self.assertEqual(os.listdir(self.service), ['web_ui'])                     # nothing else is switched on yet
        self.assertIn('ln -s', self.read(os.path.join(self.data, 'rc.local')))
        self.assertIn('http://192.168.1.29:8088', out)
        self.assertIn('http://100.79.242.32:8088', out)                             # Tailscale address too
        self.assertNotIn('127.0.0.1:8088', out)
        self.assertIn('Password: ' + pw.group(1), out)
        self.assertNotIn('[mopeka_standard]', text)                                 # no example devices to edit by hand
        self.assertNotIn('11:22:33:44:55:77', text)

    def test_running_it_again_keeps_the_password_and_settings(self):
        self.run_install()
        first = self.config()
        with open(os.path.join(self.data, 'shared_drivers', 'config.ini'), 'a') as f:
            f.write('\n[starlink]\ncap_gb = 100\n')
        rc, out = self.run_install()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.config(), first + '\n[starlink]\ncap_gb = 100\n')
        self.assertNotIn('Password: ' + re.search(r'password = (\S+)', first).group(1), out)     # not printed again
        self.assertIn('see [web]', out)
        self.assertIn('svc -t', self.read(os.path.join(self.t, 'svc.log')))                   # restarted, not duplicated

    def test_named_services_still_work(self):
        rc, out = self.run_install('web_ui', 'fridge')
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(os.listdir(self.service)), ['fridge', 'web_ui'])

    def test_scripts_are_valid_shell(self):
        for path in ('get.sh', 'data/shared_drivers/install.sh', 'data/shared_drivers/update.sh', 'data/shared_drivers/uninstall.sh'):
            p = subprocess.run(['sh', '-n', os.path.join(REPO, path)], stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 0, path + p.stderr.decode())


if __name__ == '__main__':
    unittest.main()
