#!/usr/bin/env python3
"""
Venus OS driver for 12 V compressor fridges with the "Alpicool" Bluetooth
controller (Alpicool, Brass Monkey, BougeRV, Vevor and probably Hyckes - check
with diagnostics/fridge_probe.py first).

Every poll_interval seconds it connects, sends a status query, publishes the
answer and disconnects again. Each zone becomes a
com.victronenergy.temperature service of type fridge or freezer, so it shows
up with the other environment sensors in the GUI and on VRM.

Control (control = yes, the default): a com.victronenergy.switch service with
Venus OS "switchable outputs", which the GUI shows on its switch pane: power
and eco mode as toggles, and a temperature set point per zone. A change connects to
the fridge right away, sends the command and reads the status back.

The fridge accepts only one Bluetooth connection at a time. Between polls the
phone app can connect; while the phone is connected the poll fails and the
values age out after `timeout` seconds.
"""

import logging

import alpicool_protocol as proto
from ble_gatt import GattClient
from gi.repository import GLib
from venus_service import (BleDriver, SwitchDevice, TemperatureDevice, TEMP_FREEZER,
                           TEMP_FRIDGE, TEMPERATURE_TYPES)

log = logging.getLogger('fridge')

EXTRA_PATHS = ('/Fridge/TargetTemperature', '/Fridge/PoweredOn', '/Fridge/EcoMode',
               '/Fridge/Locked', '/Fridge/BatterySaver', '/Fridge/SupplyVoltage',
               '/Fridge/BatteryPercent', '/Fridge/RunningStatus')


class FridgeDriver(BleDriver):
    DEFAULT_SECTION = 'fridge'
    USES_SCANNER = True   # keeps BlueZ aware of the fridge between connections

    def __init__(self, section, config):
        super(FridgeDriver, self).__init__(section, config)
        if not self.mac:
            raise SystemExit('[%s] mac = ... is required for the fridge' % section)
        self.poll_s = max(10, config.getint('poll_interval', 60))
        persistent = config.getboolean('stay_connected', False)
        self.uid = config.get('service', 'fridge')
        self.zone_types = [config.get('zone1_type', 'auto'), config.get('zone2_type', 'auto')]
        self.zone_names = [config.get('zone1_name', 'Fridge'), config.get('zone2_name', 'Freezer')]
        self.timeout = config.getint('timeout', max(300, 4 * self.poll_s))
        self.zones = []
        self._assembler = proto.FrameAssembler()
        self._failures = 0
        self.control = config.getboolean('control', True)
        self.control_group = config.get('control_group', 'Fridge')
        self._logged_ranges = set()
        self.switch = None       # SwitchDevice with the controls, made after the first status
        self._status = None      # last FridgeStatus, needed to build commands
        self._want = {}          # pending changes: powered_on, eco_mode, 0/1 = set point C
        self._send_tries = 0
        self.gatt = GattClient(self.bus, self.mac, proto.WRITE_UUID, proto.NOTIFY_UUID,
                               self._on_notify, adapter=config.get('adapter', 'hci0'),
                               keep_connected=persistent)
        # Zone 1 always exists; zone 2 is created once the fridge reports it
        self._zone(0)

    def handle_advertisement(self, mac, manufacturer_data, rssi):
        pass   # the scanner only keeps the device object alive in BlueZ

    def _zone(self, i):
        while len(self.zones) <= i:
            n = len(self.zones)
            dev = TemperatureDevice(
                self.bus, '%s_zone%d' % (self.uid, n + 1),
                product_name='Compressor fridge',
                connection='Bluetooth LE %s' % self.mac,
                temperature_type=TEMPERATURE_TYPES.get(self.zone_types[n], TEMP_FRIDGE),
                default_instance=self.cfg.getint('device_instance', 20) + n,
                custom_name=self.zone_names[n],
                timeout_s=self.timeout,
                extra_paths=EXTRA_PATHS)
            self.zones.append(dev)
            self.devices.append(dev)
        return self.zones[i]

    # ------------------------------------------------------------- controls

    SO = '/SwitchableOutput/%s/%s'
    TYPE_TOGGLE, TYPE_SETPOINT = 1, 3

    def _add_channel(self, svc, ch, name, otype, writable_path, callback):
        so = lambda p: self.SO % (ch, p)
        svc.add_path(so('Name'), name)
        svc.add_path(so('Status'), 0)
        svc.add_path(so('State'), 1 if otype == self.TYPE_SETPOINT else 0,
                     writeable=writable_path == 'State',
                     onchangecallback=callback if writable_path == 'State' else None)
        svc.add_path(so('Settings/Type'), otype)
        svc.add_path(so('Settings/ValidTypes'), 1 << otype)
        svc.add_path(so('Settings/CustomName'), name)
        svc.add_path(so('Settings/Group'), self.control_group)
        svc.add_path(so('Settings/ShowUIControl'), 1)
        svc.add_path(so('Settings/Adjustable'), 0)     # type/name fixed by the driver
        if otype == self.TYPE_SETPOINT:
            svc.add_path(so('Dimming'), None, writeable=True, onchangecallback=callback)
            svc.add_path(so('Settings/DimmingMin'), None)   # the GUI looks for these under Settings/
            svc.add_path(so('Settings/DimmingMax'), None)
            svc.add_path(so('Settings/StepSize'), 1)
            svc.add_path(so('Settings/Decimals'), 0)
            svc.add_path(so('Measurement'), None)

    @staticmethod
    def channel(zone):
        return 'setpoint' if zone == 0 else 'setpoint%d' % (zone + 1)

    def _add_controls(self, svc, zones):
        self._add_channel(svc, 'power', 'Fridge power', self.TYPE_TOGGLE, 'State',
                          lambda p, v: self._request('powered_on', bool(int(v))))
        self._add_channel(svc, 'eco', 'Fridge eco mode', self.TYPE_TOGGLE, 'State',
                          lambda p, v: self._request('eco_mode', bool(int(v))))
        for zone in range(zones):
            self._add_channel(svc, self.channel(zone), '%s set point' % self.zone_names[zone],
                              self.TYPE_SETPOINT, 'Dimming',
                              lambda p, v, z=zone: self._request_setpoint(z, v))

    def _ensure_switch(self, zones):
        """The switch pane lists outputs of com.victronenergy.switch services only. Paths must
        exist before a service registers, so it is made once the number of zones is known."""
        if self.switch is None:
            self.switch = SwitchDevice(
                self.bus, '%s_controls' % self.uid, product_name='Compressor fridge controls',
                connection='Bluetooth LE %s' % self.mac,
                default_instance=self.cfg.getint('device_instance', 20),
                custom_name=self.control_group, timeout_s=self.timeout,
                setup=lambda svc: self._add_controls(svc, zones))
            self.devices.append(self.switch)
        return self.switch

    def _request_setpoint(self, zone, value):
        if self._status is None or zone >= len(self._status.zones):
            return False
        z = self._status.zones[zone]
        lo, hi = self._range(zone, z)
        t = max(lo, min(hi, float(value)))
        return self._request(zone, int(round(t)))

    # From the manual of the dual-zone fridges: the fridge compartment is set 0...8 C and the freezer
    # -20...-12 C; single-compartment models take -20...20 C.
    RANGES = {'fridge': (0.0, 8.0), 'freezer': (-20.0, -12.0), 'single': (-20.0, 20.0)}

    def _range(self, i, zone):
        """Set point range of a zone: zoneN_min / zoneN_max from config.ini, else the manual's range
        for that kind of compartment. What the fridge itself reports is only logged: it has been
        seen to disagree with the manual."""
        kind = self.zone_types[i] if i < len(self.zone_types) else 'auto'
        if self._status is not None and len(self._status.zones) < 2:
            kind = 'single'
        elif kind not in ('fridge', 'freezer'):
            target = zone.target
            kind = 'freezer' if (target < 0 if target is not None else i > 0) else 'fridge'
        lo, hi = self.RANGES[kind]
        lo = self.cfg.getfloat('zone%d_min' % (i + 1), fallback=lo)
        hi = self.cfg.getfloat('zone%d_max' % (i + 1), fallback=hi)
        if lo >= hi:
            lo, hi = self.RANGES[kind]
        if (i, lo, hi) not in self._logged_ranges:
            self._logged_ranges.add((i, lo, hi))
            log.info('Zone %d (%s) set point range %g...%g C (the fridge itself reports %s...%s)',
                     i + 1, kind, lo, hi, zone.min, zone.max)
        return lo, hi

    def _request(self, key, value):
        """A control changed in the GUI: queue it and contact the fridge now."""
        if self._status is None:
            log.warning('Fridge not read yet - ignoring %s = %s', key, value)
            return False
        log.info('Fridge change requested: %s = %s', key, value)
        self._want[key] = value
        self._send_tries = 0
        GLib.timeout_add(300, self._send)     # collect a few slider steps first
        return True

    def _send(self):
        if not self._want:
            return False
        if self.gatt.busy:
            return True                        # try again in 300 ms
        st = self._status
        frames = []
        others = dict((k, self._want[k]) for k in ('powered_on', 'eco_mode') if k in self._want)
        if others:
            frames.append(proto.set_others_frame(st.raw, **others))
        for zone in (0, 1):
            if zone in self._want:
                frames.append(proto.set_target_frame(zone, self._want[zone], st.fahrenheit))
        frames.append(proto.query_frame())     # read the result back
        sent = dict(self._want)
        self._want = {}
        self._send_tries += 1
        self._assembler = proto.FrameAssembler()
        chunks = [c for f in frames for c in proto.chunks(f)]
        self.gatt.exchange(chunks, lambda ok, err: self._on_sent(ok, err, sent))
        return False

    def _on_sent(self, ok, error, sent):
        if ok:
            log.info('Fridge updated: %s', sent)
            return
        for k, v in sent.items():
            self._want.setdefault(k, v)
        if self._send_tries < 3:
            log.warning('Could not send to the fridge (%s), retrying', error)
            GLib.timeout_add_seconds(5, self._send)
        else:
            log.warning('Giving up sending %s to the fridge: %s', sent, error)
            self._want = {}

    # ---------------------------------------------------------------- poll

    def start(self):
        self.gatt.scanner = self.scanner    # pause scanning while connecting
        GLib.timeout_add_seconds(3, self._poll_once)          # first poll soon
        GLib.timeout_add_seconds(self.poll_s, self._poll)

    def _poll_once(self):
        self._poll()
        return False

    def _poll(self):
        if not self.gatt.busy:
            self._assembler = proto.FrameAssembler()
            self.gatt.exchange(proto.chunks(proto.query_frame()), self._on_done)
        return True

    def _on_done(self, ok, error):
        if ok:
            self._failures = 0
            return
        self._failures += 1
        # The phone app being connected is the normal reason; don't spam the log
        level = logging.WARNING if self._failures in (1, 10) or self._failures % 60 == 0 else logging.DEBUG
        log.log(level, 'fridge poll failed (%d in a row): %s', self._failures, error)

    def _on_notify(self, data):
        for cmd, payload in self._assembler.feed(data):
            if cmd not in (proto.CMD_QUERY, proto.CMD_SET_OTHERS):
                continue
            status = proto.parse_status(payload)
            if status is None:
                log.warning('short status frame: %s', payload.hex())
                continue
            log.debug('%r', status)
            self._status = status
            self._publish(status)
            self.gatt.finish()

    def _publish(self, st):
        for i, zone in enumerate(st.zones):
            dev = self._zone(i)
            ttype = None
            if self.zone_types[i] == 'auto' and zone.target is not None:
                ttype = TEMP_FREEZER if zone.target < 0 else TEMP_FRIDGE
            if zone.current is None:
                continue   # no sensor reading for this zone - let it time out
            dev.publish(zone.current, temperature_type=ttype, extra={
                '/Fridge/TargetTemperature': zone.target,
                '/Fridge/PoweredOn': int(st.powered_on),
                '/Fridge/EcoMode': int(st.eco_mode),
                '/Fridge/Locked': int(st.locked),
                '/Fridge/BatterySaver': st.battery_saver,
                '/Fridge/SupplyVoltage': st.supply_voltage,
                '/Fridge/BatteryPercent': st.battery_percent,
                '/Fridge/RunningStatus': st.running_status,
            })
        if self.control and st.zones:
            sw = self._ensure_switch(len(st.zones))
            for i, zone in enumerate(st.zones):
                self._publish_controls(sw, i, zone, st)
            sw.publish()

    def _publish_controls(self, dev, i, zone, st):
        so = self.SO
        ch = self.channel(i)
        # A change from the GUI that is still waiting to be sent wins over
        # the fridge's (older) value, so the control doesn't jump back.
        if i == 0:
            for toggle, key, on in (('power', 'powered_on', st.powered_on),
                                    ('eco', 'eco_mode', st.eco_mode)):
                if key not in self._want:
                    dev[so % (toggle, 'State')] = int(on)
                dev[so % (toggle, 'Status')] = 0x09 if on else 0x00
        if i not in self._want:
            dev[so % (ch, 'Dimming')] = zone.target
        dev[so % (ch, 'Measurement')] = zone.current
        lo, hi = self._range(i, zone)
        dev[so % (ch, 'Settings/DimmingMin')] = lo
        dev[so % (ch, 'Settings/DimmingMax')] = hi
        dev[so % (ch, 'Status')] = 0x09 if st.powered_on else 0x00

    def shutdown(self):
        self.gatt.close()


if __name__ == '__main__':
    FridgeDriver.main()
