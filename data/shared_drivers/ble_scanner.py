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

import dbus
from gi.repository import GLib

log = logging.getLogger(__name__)

BLUEZ = 'org.bluez'
ADAPTER_IFACE = 'org.bluez.Adapter1'
DEVICE_IFACE = 'org.bluez.Device1'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
OM_IFACE = 'org.freedesktop.DBus.ObjectManager'


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
                return  # our session is already running
            log.warning('Could not start BLE discovery: %s', e)
            self._adapter_path = None

    def _watchdog(self):
        self._ensure_discovery()
        return True

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
