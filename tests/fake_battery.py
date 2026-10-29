"""A made-up 16-cell LiFePO4 battery on D-Bus, for trying the settings page's Battery tab.
Cell 7 is a little weak, cell 3 is balancing, the current swings between charging and discharging."""

import math
import os
import sys
import time

sys.path.insert(0, os.environ['VELIB'])
import dbus  # noqa: E402,F401
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402
from vedbus import VeDbusService  # noqa: E402

DBusGMainLoop(set_as_default=True)
N = 16
svc = VeDbusService('com.victronenergy.battery.ttyUSB0', dbus.SystemBus(private=True), register=False)
svc.add_mandatory_paths('fake_battery.py', '1.0', 'USB', 1, 0, 'JK BMS (test)', None, None, 1)
for path in ['/Dc/0/Voltage', '/Dc/0/Current', '/Dc/0/Power', '/Dc/0/Temperature', '/Soc',
             '/System/MOSTemperature', '/System/Temperature1', '/System/Temperature2',
             '/System/MinCellVoltage', '/System/MaxCellVoltage', '/System/NrOfCellsPerBattery',
             '/Info/MaxChargeCurrent', '/Info/MaxDischargeCurrent', '/Info/MaxChargeVoltage',
             '/Io/AllowToCharge', '/Io/AllowToDischarge', '/Io/AllowToBalance',
             '/Alarms/LowVoltage', '/Alarms/HighTemperature', '/Alarms/CellImbalance',
             '/Capacity', '/InstalledCapacity']:
    svc.add_path(path, None)
for i in range(1, N + 1):
    svc.add_path('/Voltages/Cell%d' % i, None)
    svc.add_path('/Balances/Cell%d' % i, None)
svc.register()
t0 = time.time()


def tick():
    t = time.time() - t0
    soc = 70 + 20 * math.sin(t / 40.0)
    cur = 18 * math.sin(t / 25.0)
    base = 3.18 + soc / 100.0 * 0.2
    cells = [base + 0.004 * math.sin(i + t / 9.0) - (0.028 if i == 6 else 0) + (0.004 if i == 2 else 0) for i in range(N)]
    svc['/Dc/0/Voltage'] = round(sum(cells), 2)
    svc['/Dc/0/Current'] = round(cur, 1)
    svc['/Dc/0/Power'] = round(cur * sum(cells))
    svc['/Dc/0/Temperature'] = round(22 + 2 * math.sin(t / 60.0), 1)
    svc['/System/Temperature1'] = 21.5
    svc['/System/Temperature2'] = round(23 + math.sin(t / 50.0), 1)
    svc['/System/MOSTemperature'] = round(31 + 6 * abs(math.sin(t / 25.0)), 1)
    svc['/Soc'] = round(soc, 1)
    svc['/System/MinCellVoltage'] = round(min(cells), 3)
    svc['/System/MaxCellVoltage'] = round(max(cells), 3)
    svc['/System/NrOfCellsPerBattery'] = N
    svc['/Info/MaxChargeCurrent'] = 50.0 if soc < 85 else 20.0
    svc['/Info/MaxDischargeCurrent'] = 60.0
    svc['/Info/MaxChargeVoltage'] = 55.2
    svc['/Io/AllowToCharge'] = 1
    svc['/Io/AllowToDischarge'] = 1
    svc['/Io/AllowToBalance'] = 1
    svc['/Alarms/LowVoltage'] = 0
    svc['/Alarms/HighTemperature'] = 0
    svc['/Alarms/CellImbalance'] = 0
    svc['/Capacity'] = round(280 * soc / 100.0, 1)
    svc['/InstalledCapacity'] = 280
    for i, v in enumerate(cells):
        svc['/Voltages/Cell%d' % (i + 1)] = round(v, 3)
        svc['/Balances/Cell%d' % (i + 1)] = 1 if i == 2 else 0
    return True


tick()
GLib.timeout_add_seconds(1, tick)
GLib.MainLoop().run()
