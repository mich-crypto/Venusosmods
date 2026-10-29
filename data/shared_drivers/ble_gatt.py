"""
Minimal asynchronous BLE GATT client on top of BlueZ D-Bus, running in the
GLib main loop (same reasoning as ble_scanner.py: no asyncio, no threads).

One "exchange" = connect (if needed) -> wait for services -> enable notify
-> write one or more frames -> collect notifications until the caller says
finish() -> disconnect (unless keep_connected).
"""

import logging

import dbus
from gi.repository import GLib

log = logging.getLogger(__name__)

BLUEZ = 'org.bluez'
DEVICE_IFACE = 'org.bluez.Device1'
CHAR_IFACE = 'org.bluez.GattCharacteristic1'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
OM_IFACE = 'org.freedesktop.DBus.ObjectManager'


class GattClient(object):
    # BlueZ errors worth another attempt: the controller gave up, or the
    # peripheral did not answer this time
    RETRYABLE = ('abort', 'timeout', 'timed out', 'In Progress', 'Software caused')

    def __init__(self, bus, mac, write_uuid, notify_uuid, on_notify,
                 adapter='hci0', timeout_s=45, keep_connected=False,
                 scanner=None, connect_attempts=3):
        self._bus = bus
        self.mac = mac.upper()
        self._write_uuid = write_uuid.lower()
        self._notify_uuid = notify_uuid.lower()
        self._on_notify = on_notify
        self._timeout_s = timeout_s
        self.keep_connected = keep_connected
        self.scanner = scanner                  # paused while connecting
        self.connect_attempts = connect_attempts
        self._attempt = 0
        self.dev_path = '/org/bluez/%s/dev_%s' % (adapter, self.mac.replace(':', '_'))

        self.busy = False
        self._ready = False          # connected, notify enabled
        self._setting_up = False     # characteristic lookup / StartNotify in flight
        self._write_path = None
        self._notify_path = None
        self._pending = []
        self._done_cb = None
        self._timer = None
        self._receivers = []

        bus.add_signal_receiver(self._on_device_props, dbus_interface=PROPS_IFACE,
                                signal_name='PropertiesChanged', bus_name=BLUEZ,
                                path=self.dev_path, arg0=DEVICE_IFACE)

    # ------------------------------------------------------------- public

    def exchange(self, frames, done_cb):
        """
        Send frames (list of bytes, each <= 20 bytes) and keep receiving until
        finish() is called. done_cb(ok, error_text) is called exactly once.
        """
        if self.busy:
            done_cb(False, 'busy')
            return
        self.busy = True
        self._pending = list(frames)
        self._done_cb = done_cb
        self._timer = GLib.timeout_add_seconds(self._timeout_s, self._on_timeout)
        if self._ready:
            self._write_next()
            return
        try:
            obj = self._bus.get_object(BLUEZ, self.dev_path)
            props = dbus.Interface(obj, PROPS_IFACE)
            connected = props.Get(DEVICE_IFACE, 'Connected')
        except dbus.exceptions.DBusException:
            self._fail('device not known to BlueZ yet (not advertising or out of range)')
            return
        if connected:
            self._after_connect()
        else:
            self._attempt = 0
            self._connect()

    def _connect(self):
        if not self.busy:
            return False
        self._attempt += 1
        if self.scanner is not None:
            self.scanner.pause()
        log.debug('connecting to %s (attempt %d)', self.mac, self._attempt)
        obj = self._bus.get_object(BLUEZ, self.dev_path)
        dbus.Interface(obj, DEVICE_IFACE).Connect(
            reply_handler=self._after_connect,
            error_handler=self._on_connect_error,
            timeout=self._timeout_s)
        return False

    def _on_connect_error(self, e):
        if not self.busy:
            return
        text = str(e)
        if self._attempt < self.connect_attempts and any(r in text for r in self.RETRYABLE):
            log.debug('connect attempt %d failed (%s), retrying', self._attempt, text)
            GLib.timeout_add_seconds(2, self._connect)
            return
        self._fail('connect failed after %d attempt(s): %s' % (self._attempt, text))

    def finish(self):
        """Caller has what it needs: end the exchange successfully."""
        if self.busy:
            self._end(True, None)

    def close(self):
        """Disconnect, used on shutdown."""
        self.keep_connected = False
        if self.busy:
            self._end(False, 'shutdown')
        else:
            self._teardown()

    # ----------------------------------------------------------- sequence

    def _after_connect(self, *args):
        if not self.busy:
            return
        props = dbus.Interface(self._bus.get_object(BLUEZ, self.dev_path), PROPS_IFACE)
        if props.Get(DEVICE_IFACE, 'ServicesResolved'):
            self._setup_characteristics()
        # else: _on_device_props continues when ServicesResolved becomes True

    def _on_device_props(self, interface, changed, invalidated):
        if 'Connected' in changed and not changed['Connected']:
            if self._ready:
                log.debug('%s disconnected', self.mac)
            self._ready = False
            self._setting_up = False
            self._remove_receivers()
            if self.busy:
                self._fail('device disconnected')
        elif changed.get('ServicesResolved') and self.busy and not self._ready:
            self._setup_characteristics()

    def _setup_characteristics(self):
        # Connect's reply and the ServicesResolved signal can arrive in
        # either order; only run the setup once per connection.
        if self._setting_up:
            return
        self._setting_up = True
        om = dbus.Interface(self._bus.get_object(BLUEZ, '/'), OM_IFACE)
        self._write_path = self._notify_path = None
        for path, ifaces in om.GetManagedObjects().items():
            if not str(path).startswith(self.dev_path + '/') or CHAR_IFACE not in ifaces:
                continue
            uuid = str(ifaces[CHAR_IFACE]['UUID']).lower()
            if uuid == self._write_uuid:
                self._write_path = str(path)
            elif uuid == self._notify_uuid:
                self._notify_path = str(path)
        if not (self._write_path and self._notify_path):
            self._fail('GATT characteristics %s / %s not found - different fridge protocol?'
                       % (self._write_uuid[4:8], self._notify_uuid[4:8]))
            return
        self._receivers.append(self._bus.add_signal_receiver(
            self._on_char_props, dbus_interface=PROPS_IFACE,
            signal_name='PropertiesChanged', bus_name=BLUEZ,
            path=self._notify_path, arg0=CHAR_IFACE))
        char = dbus.Interface(self._bus.get_object(BLUEZ, self._notify_path), CHAR_IFACE)
        char.StartNotify(reply_handler=self._notify_started,
                         error_handler=lambda e: self._fail('StartNotify failed: %s' % e))

    def _notify_started(self):
        self._setting_up = False
        self._ready = True
        self._write_next()

    def _write_next(self):
        if not self.busy:
            return
        if not self._pending:
            return   # all written, waiting for notifications / finish()
        chunk = self._pending.pop(0)
        char = dbus.Interface(self._bus.get_object(BLUEZ, self._write_path), CHAR_IFACE)
        char.WriteValue(dbus.Array(bytearray(chunk), signature='y'),
                        dbus.Dictionary({'type': dbus.String('request')}, signature='sv'),
                        reply_handler=self._write_next,
                        error_handler=lambda e: self._fail('write failed: %s' % e))

    def _on_char_props(self, interface, changed, invalidated):
        if 'Value' in changed:
            try:
                self._on_notify(bytes(bytearray(changed['Value'])))
            except Exception:
                log.exception('notify handler failed')

    # ------------------------------------------------------------ cleanup

    def _on_timeout(self):
        self._timer = None
        self._fail('timeout after %d s' % self._timeout_s)
        return False

    def _fail(self, error):
        if self.busy:
            self._end(False, error)

    def _end(self, ok, error):
        self.busy = False
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if not ok or not self.keep_connected:
            self._teardown()
        if self.scanner is not None:
            self.scanner.resume()
        cb, self._done_cb = self._done_cb, None
        if cb:
            cb(ok, error)

    def _remove_receivers(self):
        for r in self._receivers:
            r.remove()
        self._receivers = []

    def _teardown(self):
        self._remove_receivers()
        self._ready = False
        self._setting_up = False
        try:
            obj = self._bus.get_object(BLUEZ, self.dev_path)
            dbus.Interface(obj, DEVICE_IFACE).Disconnect(
                reply_handler=lambda: None, error_handler=lambda e: None)
        except dbus.exceptions.DBusException:
            pass
