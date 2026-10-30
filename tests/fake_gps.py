"""A made-up GPS on D-Bus, for trying the settings page's Geofence tab. It sits 60 m north of 52.0, 5.0."""

import os
import sys

sys.path.insert(0, os.environ['VELIB'])
import dbus  # noqa: E402,F401
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402
from vedbus import VeDbusService  # noqa: E402

DBusGMainLoop(set_as_default=True)
svc = VeDbusService('com.victronenergy.gps.ve_ttyACM0', dbus.SystemBus(private=True), register=False)
svc.add_mandatory_paths('fake_gps.py', '1.0', 'USB', 1, 0, 'GPS (test)', None, None, 1)
svc.add_path('/Fix', 1)
svc.add_path('/NrOfSatellites', 7)
svc.add_path('/Position/Latitude', float(os.environ.get('FAKE_LAT', '52.00054')))
svc.add_path('/Position/Longitude', float(os.environ.get('FAKE_LON', '5.0')))
svc.register()
GLib.MainLoop().run()
