"""
Stand-ins for dbus, GLib and Victron's velib so the drivers can be imported
and tested on any PC. Importing this module installs them in sys.modules.
"""

import os
import sys
import tempfile
import types

os.environ.setdefault('VENUSOSMODS_RAWLOG_DIR', tempfile.mkdtemp(prefix='rawlog_'))


class FakeService(dict):
    def __init__(self, name, bus=None, register=None):
        super().__init__()
        self.name = name
        self.registered = register is not False
        self._dbusname = None
        self.paths_at_register = None

    def add_mandatory_paths(self, **kw):
        self['/DeviceInstance'] = kw['deviceinstance']
        self['/ProductName'] = kw['productname']
        self['/Connected'] = kw['connected']

    def add_path(self, path, value, **kw):
        assert not self.registered, 'path %s added after register()' % path
        self[path] = value

    def register(self):
        self.registered = True
        self.paths_at_register = set(self)


class FakeSettings(dict):
    def __init__(self, bus, supported, callback, timeout=0):
        super().__init__({k: v[1] for k, v in supported.items()})


class FakeBus(object):
    def add_signal_receiver(self, *a, **kw):
        return types.SimpleNamespace(remove=lambda: None)


def _install():
    clock = {'us': 0}
    glib = types.SimpleNamespace(
        get_monotonic_time=lambda: clock['us'],
        timeout_add_seconds=lambda *a: 1,
        timeout_add=lambda *a: 1,
        source_remove=lambda *a: None,
        unix_signal_add=lambda *a: 1,
        io_add_watch=lambda *a: 1,
        PRIORITY_DEFAULT=0, PRIORITY_HIGH=-100, IO_IN=1, IO_OUT=4, IO_ERR=8, IO_HUP=16,
        MainLoop=None)
    dbus = types.ModuleType('dbus')
    dbus.SystemBus = lambda **kw: FakeBus()
    dbus.exceptions = types.SimpleNamespace(DBusException=Exception)
    # the value types that unwrap() looks at; like the real ones they accept
    # variant_level= and signature=
    def value_type(name, base):
        def new(cls, value=base(), variant_level=0, signature=None):
            return base.__new__(cls, value)
        return type(name, (base,), {'__new__': new})

    def container_type(name, base):
        def init(self, value=(), variant_level=0, signature=None, **kw):
            base.__init__(self, value)
        return type(name, (base,), {'__init__': init})

    dbus.Boolean = value_type('Boolean', int)
    dbus.Array = container_type('Array', list)
    dbus.Dictionary = container_type('Dictionary', dict)
    for name in ('Byte', 'Int16', 'UInt16', 'Int32', 'UInt32', 'Int64', 'UInt64'):
        setattr(dbus, name, value_type(name, int))
    dbus.Double = value_type('Double', float)
    for name in ('String', 'ObjectPath', 'Signature'):
        setattr(dbus, name, value_type(name, str))
    mainloop = types.ModuleType('dbus.mainloop')
    mainloop_glib = types.ModuleType('dbus.mainloop.glib')
    mainloop_glib.DBusGMainLoop = lambda **kw: None
    gi = types.ModuleType('gi')
    gi_repo = types.ModuleType('gi.repository')
    gi_repo.GLib = glib
    vedbus = types.ModuleType('vedbus')
    vedbus.VeDbusService = FakeService
    settingsdevice = types.ModuleType('settingsdevice')
    settingsdevice.SettingsDevice = FakeSettings
    sys.modules.update({
        'dbus': dbus, 'dbus.mainloop': mainloop, 'dbus.mainloop.glib': mainloop_glib,
        'gi': gi, 'gi.repository': gi_repo,
        'vedbus': vedbus, 'settingsdevice': settingsdevice})
    return clock, glib


CLOCK, GLIB = _install()      # modules are imported once, so this runs once
