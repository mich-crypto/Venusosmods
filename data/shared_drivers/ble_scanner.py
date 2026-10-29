"""
Passive BLE advertisement listener that talks to BlueZ directly over D-Bus.

Why not bleak?
  bleak needs its own asyncio loop, while Venus OS drivers (vedbus) need the
  GLib main loop. Running both means threads and hand-off queues. BlueZ already
  exposes every advertisement on the system D-Bus, so we can subscribe to it
  with dbus-python from inside the *same* GLib loop that vedbus uses:
  no asyncio, no threads, no pip packages, nothing that breaks on a firmware
  update.

Coexistence:
  BlueZ reference-counts discovery sessions per D-Bus client, so this scanner
  does not interfere with Venus OS's own dbus-ble-sensors or VictronConnect.
"""

import logging
import os
import time

import dbus
from gi.repository import GLib

log = logging.getLogger(__name__)

BLUEZ = 'org.bluez'
ADAPTER_IFACE = 'org.bluez.Adapter1'
DEVICE_IFACE = 'org.bluez.Device1'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
OM_IFACE = 'org.freedesktop.DBus.ObjectManager'


# Cross-process pause: a driver that is about to open a GATT connection writes
# an expiry time here, and every scanner (in all our driver processes) stops
# its discovery until the file is gone or expired. The Raspberry Pi's
# built-in controller rejects connections ("Busy",
# le-connection-abort-by-local) while any process keeps it scanning.
PAUSE_FILE = '/tmp/venusosmods-ble-pause'


def request_global_pause(seconds):
    try:
        with open(PAUSE_FILE, 'w') as f:
            f.write('%.1f' % (time.time() + seconds))
    except OSError as e:
        log.warning('Could not write %s: %s', PAUSE_FILE, e)


def clear_global_pause():
    try:
        os.remove(PAUSE_FILE)
    except OSError:
        pass


def global_pause_active():
    try:
        with open(PAUSE_FILE) as f:
            return float(f.read().strip() or 0) > time.time()
    except (OSError, ValueError):
        return False


def _mac_from_path(path):
    # /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF -> AA:BB:CC:DD:EE:FF
    tail = str(path).rsplit('/', 1)[-1]
    if not tail.startswith('dev_'):
        return None
    return tail[4:].replace('_', ':').upper()


def _convert_mfr_data(value):
    # dbus.Dictionary({UInt16: Array(Byte)}) -> {int: bytes}
    return {int(k): bytes(bytearray(v)) for k, v in value.items()}


class BlueZScanner(object):
    """
    callback(mac, manufacturer_data, rssi) is invoked from the GLib main loop
    for every advertisement that carries manufacturer data.
      mac               'AA:BB:CC:DD:EE:FF'
      manufacturer_data {company_id(int): payload(bytes)}  (company id stripped)
      rssi              int or None
    """

    WATCHDOG_S = 30

    def __init__(self, bus, callback, adapter='hci0', mac_filter=None):
        self._bus = bus
        self._callback = callback
        self._adapter_name = adapter
        self._mac_filter = set(m.upper() for m in mac_filter) if mac_filter else None
        self._adapter_path = None
        self._rssi = {}
        self._paused = False         # paused by our own GATT client
        self._ext_paused = False     # paused because another process connects
        self._stuck = 0              # checks in a row where BlueZ said "in progress" but was not scanning

    def start(self):
        self._bus.add_signal_receiver(
            self._on_properties_changed,
            dbus_interface=PROPS_IFACE, signal_name='PropertiesChanged',
            bus_name=BLUEZ, arg0=DEVICE_IFACE, path_keyword='path')
        self._bus.add_signal_receiver(
            self._on_interfaces_added,
            dbus_interface=OM_IFACE, signal_name='InterfacesAdded',
            bus_name=BLUEZ)
        self._ensure_discovery(force=True)
        # Re-arm discovery if bluetoothd restarts or the adapter is power-cycled
        GLib.timeout_add_seconds(self.WATCHDOG_S, self._watchdog)
        GLib.timeout_add(1000, self._check_global_pause)

    # ---------------------------------------------------------------- discovery

    def _find_adapter(self):
        om = dbus.Interface(self._bus.get_object(BLUEZ, '/'), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if ADAPTER_IFACE in ifaces and str(path).endswith('/' + self._adapter_name):
                return str(path)
        return None

    def _ensure_discovery(self, force=False):
        try:
            if self._adapter_path is None:
                self._adapter_path = self._find_adapter()
                if self._adapter_path is None:
                    log.warning('Bluetooth adapter %s not found (is Bluetooth enabled '
                                'in Settings > General?)', self._adapter_name)
                    return
            obj = self._bus.get_object(BLUEZ, self._adapter_path)
            props = dbus.Interface(obj, PROPS_IFACE)
            if not props.Get(ADAPTER_IFACE, 'Powered'):
                log.warning('Bluetooth adapter %s is not powered', self._adapter_name)
                return
            # Advertisement signals are broadcast to every D-Bus client, so as
            # long as anyone keeps the adapter scanning we receive them. Only
            # (re)start our own session when scanning has stopped, to avoid
            # resetting the scan filter of other clients every watchdog tick.
            if not force and props.Get(ADAPTER_IFACE, 'Discovering'):
                return
            adapter = dbus.Interface(obj, ADAPTER_IFACE)
            # DuplicateData=True: report every advertisement, not only changes
            adapter.SetDiscoveryFilter({
                'Transport': dbus.String('le'),
                'DuplicateData': dbus.Boolean(True),
            })
            adapter.StartDiscovery()
            log.info('BLE discovery started on %s', self._adapter_path)
        except dbus.exceptions.DBusException as e:
            name = e.get_dbus_name() or ''
            if name.endswith('InProgress'):
                self._in_progress()
                return
            log.warning('Could not start BLE discovery: %s', e)
            self._adapter_path = None

    def _in_progress(self):
        """
        StartDiscovery said "in progress". Normally that is our own session. But BlueZ can also get
        stuck (a scan it believes is pending while the adapter does nothing, after many stop/start
        cycles): then no advertisement ever arrives. Try stop+start, then power-cycle the adapter.
        """
        try:
            obj = self._bus.get_object(BLUEZ, self._adapter_path)
            props = dbus.Interface(obj, PROPS_IFACE)
            if props.Get(ADAPTER_IFACE, 'Discovering'):
                self._stuck = 0
                return
            self._stuck += 1
            log.warning('BlueZ reports a scan in progress but the adapter is not scanning (%d)', self._stuck)
            adapter = dbus.Interface(obj, ADAPTER_IFACE)
            if self._stuck == 1:
                try:
                    adapter.StopDiscovery()
                except dbus.exceptions.DBusException:
                    pass
                adapter.StartDiscovery()
                self._stuck = 0
                log.info('BLE discovery restarted on %s', self._adapter_path)
            else:
                log.warning('Power-cycling Bluetooth adapter %s to unstick it', self._adapter_name)
                props.Set(ADAPTER_IFACE, 'Powered', dbus.Boolean(False))
                GLib.timeout_add_seconds(2, self._power_on)
        except dbus.exceptions.DBusException as e:
            log.warning('Could not unstick BLE discovery: %s', e)

    def _power_on(self):
        try:
            obj = self._bus.get_object(BLUEZ, self._adapter_path)
            dbus.Interface(obj, PROPS_IFACE).Set(ADAPTER_IFACE, 'Powered', dbus.Boolean(True))
            self._stuck = 0
            GLib.timeout_add_seconds(2, self._rearm)
        except dbus.exceptions.DBusException as e:
            log.warning('Could not power the adapter back on: %s', e)
        return False

    def _rearm(self):
        self._ensure_discovery(force=True)
        return False

    def _watchdog(self):
        if not (self._paused or self._ext_paused):
            self._ensure_discovery()
        return True

    def _stop_discovery(self):
        if self._adapter_path is None:
            return
        try:
            obj = self._bus.get_object(BLUEZ, self._adapter_path)
            dbus.Interface(obj, ADAPTER_IFACE).StopDiscovery()
        except dbus.exceptions.DBusException:
            pass   # not running, or not ours

    def _check_global_pause(self):
        if self._paused:
            return True   # our own connection, handled by pause()/resume()
        active = global_pause_active()
        if active and not self._ext_paused:
            log.debug('another driver is connecting: pausing discovery')
            self._ext_paused = True
            self._stop_discovery()
        elif not active and self._ext_paused:
            self._ext_paused = False
            self._ensure_discovery(force=True)
        return True

    def pause(self, seconds=60):
        """
        Stop scanning in this and all other driver processes while a GATT
        connection is being made (see PAUSE_FILE). `seconds` is a safety
        expiry in case this process dies before resume().
        """
        self._paused = True
        request_global_pause(seconds)
        self._stop_discovery()

    def resume(self):
        if self._paused:
            self._paused = False
            clear_global_pause()
            self._ensure_discovery(force=True)

    # ------------------------------------------------------------------ signals

    def _on_interfaces_added(self, path, interfaces):
        props = interfaces.get(DEVICE_IFACE)
        if props:
            self._handle(path, props)

    def _on_properties_changed(self, interface, changed, invalidated, path=None):
        self._handle(path, changed)

    def _handle(self, path, props):
        mac = _mac_from_path(path)
        if mac is None:
            return
        if self._mac_filter is not None and mac not in self._mac_filter:
            return
        if 'RSSI' in props:
            self._rssi[mac] = int(props['RSSI'])
        if 'ManufacturerData' not in props:
            return
        try:
            self._callback(mac, _convert_mfr_data(props['ManufacturerData']),
                           self._rssi.get(mac))
        except Exception:
            # Never let a parser bug kill the main loop
            log.exception('Error handling advertisement from %s', mac)
