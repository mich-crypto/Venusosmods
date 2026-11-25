"""
Truma Combi heater (CP plus panel) through the water level sensor's ESP32 - in preparation.

The ESP32 will sit on the heater's LIN bus like Truma's iNet box (docs/truma.md). This part,
inside the water driver, already does everything around it:

  * at each check-in the driver also sends "truma"; the answer "truma {json}" is published:
      com.victronenergy.temperature  "Truma room"   room temperature
      com.victronenergy.switch       "Truma heater" Heating (on/off), Room set point (5..30 C),
                                                     Hot water (0 off, 1 eco, 2 hot, 3 boost)
  * a change on the switch pane or the mods page becomes one command for the sensor:
      "truma set heat=on room=21 water=eco"
  * frost guard: room below heater_frost_c with the heating off -> heating on at frost_c + 2 C

Until the firmware has the LIN part the sensor answers {"lin": "no LIN data yet ..."} and
nothing is published as measured; the controls are there but greyed out (no reading).
"""

import json
import logging

from venus_service import DigitalInputDevice, SwitchDevice, TemperatureDevice, TEMPERATURE_TYPES

log = logging.getLogger('truma')

WATER_MODES = ('off', 'eco', 'hot', 'boost')

# Meanings of the codes the CP plus shows ("W 42 H") for the gas Combi (4, 6, 4 E, 6 E), from the
# troubleshooting guide of the Truma CP plus manual.
BUILTIN_CODES = {
    'W17': 'Water overtemperature (hot water mode with an empty water container)',
    'W18': 'Air overtemperature: warm-air ducts not connected, outlets or recirculation intake blocked',
    'W24': 'Imminent undervoltage: battery below 10.4 V',
    'E29': 'FrostControl heating element draws too much current',
    'W42': 'Window above the flue open',
    'E43': 'Overvoltage: 12 V supply above 16.4 V',
    'E44': 'Undervoltage: battery below 10.0 V',
    'W45': 'No 230 V supply or 230 V overheating protection tripped (Combi E)',
    'E111': 'Fault in the electronics',
    'E112': 'Flame out or not detected, gas supply interrupted',
    'E121': 'Flame unstable or out, gas supply interrupted',
    'E122': 'Flame not detected, gas supply interrupted',
    'E202': 'Flame out or not detected, gas supply interrupted',
    'E211': 'Flame out or not detected, gas supply interrupted',
    'E212': 'Flame not detected: gas, flue or electronics',
    'W255': 'No 12 V at the heater or no connection to the control panel',
    'W301': 'Overvoltage: 12 V supply too high',
    'W417': 'Overvoltage: 12 V supply too high',
    'W302': 'Undervoltage: battery too low',
    'W418': 'Undervoltage: battery too low',
    'W303': 'Imminent undervoltage: battery below 10.4 V',
    'W411': 'Imminent undervoltage: battery below 10.4 V',
    'W401': 'Hot water mode with an empty water container',
    'W402': 'Air overtemperature',
    'W407': 'No 230 V supply (Combi E)',
    'W408': 'Gas supply interrupted in mixed mode (Combi E)',
    'W412': 'Window above the flue open',
    'E507': 'Gas supply interrupted',
    'E516': 'Gas supply interrupted',
    'E517': 'Gas supply interrupted',
    'E607': 'Heater locked: more than four fault resets within 15 minutes',
    'E621': 'Room temperature sensor or its cable faulty',
    'E624': 'Short circuit in the FrostControl heating element',
    'E632': '230 V overheating protection tripped (Combi E)',
}
# what to do, for the settings page (Venus OS only shows the short meaning above)
BUILTIN_HELP = {
    'W17': 'Switch the heater off, let it cool down completely, fill the water container, then switch it on again.',
    'W18': 'Connect all warm-air ducts and check them for kinks, clear the outlets and the recirculation intake, ventilate the installation space.',
    'W24': 'Check with the engine running; charge the battery, check the contacts, replace an old battery.',
    'E29': 'The FrostControl heating element needs replacing.',
    'W42': 'Close and lock the window above or next to the flue cowl: its window switch stops the heater.',
    'E43': 'Make sure the heater gets a proper 12 V under load; as a test, disconnect the charger and the solar charger.',
    'E44': 'Charge the battery, check the contacts, replace an old battery.',
    'W45': 'Make sure the heater has 230 V; have the 230 V fuse checked by a qualified person; reset the 230 V overheating protection.',
    'E111': 'Make sure the heater gets a proper 12 V under load; as a test, disconnect the charger and the solar charger. If it stays: Truma Service.',
    'E112': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer.',
    'E121': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer.',
    'E122': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer. Also possible: flue blocked, regulator or electronics faulty (then Truma Service).',
    'E202': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer.',
    'E211': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer.',
    'E212': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer. Also possible: flue blocked, regulator or electronics faulty (then Truma Service).',
    'W255': "Switch the heater's 12 V back on (often on the vehicle's own control panel), check that the panel cable sits firmly at the CP plus and the heater, check the fuses. Do NOT run RESET → PR SET. The warning goes away by itself.",
    'W301': 'Make sure the heater gets a proper 12 V under load; as a test, disconnect the charger and the solar charger.',
    'W417': 'Make sure the heater gets a proper 12 V under load; as a test, disconnect the charger and the solar charger.',
    'W302': 'Charge the battery, check the contacts, replace an old battery.',
    'W418': 'Charge the battery, check the contacts, replace an old battery.',
    'W303': 'Check with the engine running; charge the battery, check the contacts, replace an old battery.',
    'W411': 'Check with the engine running; charge the battery, check the contacts, replace an old battery.',
    'W401': 'Switch the heater off, let it cool down completely, fill the water container, then switch it on again.',
    'W402': 'Connect all warm-air ducts and check them for kinks, clear the outlets and the recirculation intake, ventilate the installation space.',
    'W407': 'Make sure the heater has 230 V; have the 230 V fuse checked by a qualified person.',
    'W408': 'Restore the gas supply, start in gas mode, acknowledge the fault, then switch back to mixed mode.',
    'W412': 'Close and lock the window above or next to the flue cowl: its window switch stops the heater.',
    'E507': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer.',
    'E516': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer.',
    'E517': 'Check the gas: bottle not empty and open, quick-closing valve in the gas line open, regulator not iced up (a regulator heater such as EisEx helps), propane rather than butane (butane does not work well below 10 °C), and the flue cowl outside free of snow, ice or leaves. A faulty gas regulator is a job for a gas engineer.',
    'E607': 'Remove the actual cause first, wait 15 minutes, then acknowledge the fault.',
    'E621': 'If the code flashes, wait out the 15-minute lockout and acknowledge. If it stays: check the cable and connectors; the sensor reads about 12.6 kΩ at 20 °C; replace it if faulty.',
    'E624': 'The FrostControl heating element needs replacing.',
    'E632': 'Reset the 230 V overheating protection (reset button, see the Combi manual).',
}
def load_codes(path=None):
    """{'W42': 'meaning', ...}: the built-in Truma list (fixed)."""
    return dict(BUILTIN_CODES)


def code_text(d, codes=None):
    """'W 42 H: window above the heater cowl is open' (or just the code), '' without a code."""
    if not d.get('err'):
        return ''
    cls = d.get('ecls') or 'W'
    meaning = (codes if codes is not None else load_codes()).get('%s%s' % (cls, d['err']))
    return '%s %s H%s' % (cls, d['err'], ': ' + meaning if meaning else '')
ROOM_MIN, ROOM_MAX = 5, 30


def parse(reply):
    """'truma {...}' -> dict, or None for anything else."""
    if not reply.startswith('truma {'):
        return None
    try:
        d = json.loads(reply[6:])
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


FAN_MODES = ('eco', 'high')                       # heating fan; Truma's "boost" is shown as High
ENERGY = ('gas', 'electricity', 'mix')            # Combi E only
EL_POWER = (0, 900, 1800)                         # W, Combi E only


def set_command(heat=None, room=None, water=None, fan=None, energy=None, power=None):
    """The sensor command for a change ('truma set heat=on room=21 fan=eco water=eco'), or None.
    The CP plus wants heating on/off, room temperature and fan together: pass them together."""
    parts = []
    if heat is not None:
        parts.append('heat=%s' % ('on' if heat else 'off'))
    if room is not None:
        parts.append('room=%d' % max(ROOM_MIN, min(ROOM_MAX, int(round(float(room))))))
    if fan is not None:
        if fan not in FAN_MODES:
            raise ValueError('fan %r' % fan)
        parts.append('fan=%s' % fan)
    if water is not None:
        if water not in WATER_MODES:
            raise ValueError('water mode %r' % water)
        parts.append('water=%s' % water)
    if energy is not None:
        if energy not in ENERGY:
            raise ValueError('energy %r' % energy)
        parts.append('energy=%s' % energy)
    if power is not None:
        if int(power) not in EL_POWER:
            raise ValueError('power %r' % power)
        parts.append('power=%d' % int(power))
    return ('truma set ' + ' '.join(parts)) if parts else None


class TrumaHeater(object):
    SO = '/SwitchableOutput/%s/%s'
    TYPE_TOGGLE, TYPE_SETPOINT = 1, 3

    def __init__(self, bus, config, uid, connection, timeout, send):
        """send(command) hands a sensor command to the water driver."""
        self.send = send
        self.state = None                     # last 'truma' answer
        self.frost_guard = config.getboolean('heater_frost_guard', False)
        self.frost_c = config.getfloat('heater_frost_c', 5.0)
        self._frost_sent = False
        name = config.get('heater_name', 'Truma heater') or 'Truma heater'
        self.room = TemperatureDevice(
            bus, uid + '_truma_room', product_name='Truma Combi (room)', connection=connection,
            temperature_type=TEMPERATURE_TYPES['generic'],
            default_instance=config.getint('heater_instance', 26),
            custom_name=name + ' room', timeout_s=timeout)
        self.switch = SwitchDevice(
            bus, uid + '_truma', product_name='Truma Combi heater', connection=connection,
            default_instance=config.getint('heater_instance', 26),
            custom_name=name, timeout_s=timeout, setup=self._add_controls)
        # the code as a warning (W) or alarm (E) in Venus OS: a digital input in alarm, named after it
        self.name = name
        self.alarm = DigitalInputDevice(
            bus, uid + '_truma_alarm', product_name='Truma Combi warnings and errors', connection=connection,
            default_instance=config.getint('heater_instance', 26), custom_name=name, timeout_s=timeout)
        self._last_code = None
        self.devices = [self.room, self.switch, self.alarm]

    # ------------------------------------------------------------ Venus OS
    def _add_channel(self, svc, ch, name, otype, callback, lo=None, hi=None):
        so = lambda p: self.SO % (ch, p)
        svc.add_path(so('Name'), name)
        svc.add_path(so('Status'), 0)
        svc.add_path(so('State'), 1 if otype == self.TYPE_SETPOINT else 0,
                     writeable=otype == self.TYPE_TOGGLE,
                     onchangecallback=callback if otype == self.TYPE_TOGGLE else None)
        svc.add_path(so('Settings/Type'), otype)
        svc.add_path(so('Settings/ValidTypes'), 1 << otype)
        svc.add_path(so('Settings/CustomName'), name)
        svc.add_path(so('Settings/Group'), 'Truma heater')
        svc.add_path(so('Settings/ShowUIControl'), 1)
        svc.add_path(so('Settings/Adjustable'), 0)
        if otype == self.TYPE_SETPOINT:
            svc.add_path(so('Dimming'), None, writeable=True, onchangecallback=callback)
            svc.add_path(so('Settings/DimmingMin'), lo)
            svc.add_path(so('Settings/DimmingMax'), hi)
            svc.add_path(so('Settings/StepSize'), 1)
            svc.add_path(so('Settings/Decimals'), 0)
            svc.add_path(so('Measurement'), None)

    def _add_controls(self, svc):
        # the switch pane sorts by name
        self._add_channel(svc, 'heat', 'Heating', self.TYPE_TOGGLE, lambda p, v: self._heat(heat=bool(int(v))))
        self._add_channel(svc, 'fan', 'Heating fan high', self.TYPE_TOGGLE,
                          lambda p, v: self._heat(fan='high' if int(v) else 'eco'))
        self._add_channel(svc, 'room', 'Room set point', self.TYPE_SETPOINT,
                          lambda p, v: self._heat(room=v), ROOM_MIN, ROOM_MAX)
        self._add_channel(svc, 'water', 'Hot water (0 off, 1 eco, 2 hot, 3 boost)', self.TYPE_SETPOINT,
                          lambda p, v: self.request(water=WATER_MODES[max(0, min(3, int(round(float(v)))))]), 0, 3)

    def _heat(self, heat=None, room=None, fan=None):
        """Heating, room or fan from the switch pane: the CP plus wants all three together."""
        d = self.state or {}
        return self.request(heat=bool(d.get('heat')) if heat is None else heat,
                            room=(d.get('set') or 20) if room is None else room,
                            fan=(d.get('fan') if d.get('fan') in FAN_MODES else 'eco') if fan is None else fan)

    def request(self, heat=None, room=None, water=None, fan=None, energy=None, power=None):
        """A change from the switch pane or the page: one command to the sensor."""
        try:
            cmd = set_command(heat, room, water, fan, energy, power)
        except (TypeError, ValueError) as e:
            log.warning('heater: %s', e)
            return False
        if not cmd:
            return False
        log.info('heater change requested: %s', cmd)
        return bool(self.send(cmd))

    # ------------------------------------------------------------ from the sensor
    def apply(self, d):
        """Publish one 'truma' answer."""
        self.state = d
        live = d.get('lin') == 'ok'
        s = self.switch.service
        so = lambda ch, p: self.SO % (ch, p)
        if live:
            if d.get('room') is not None:
                self.room.publish(d['room'])
            s[so('heat', 'State')] = 1 if d.get('heat') else 0
            s[so('fan', 'State')] = 1 if d.get('fan') == 'high' else 0
            s[so('room', 'Dimming')] = d.get('set')
            s[so('room', 'Measurement')] = d.get('room')
            water = d.get('water')
            s[so('water', 'Dimming')] = WATER_MODES.index(water) if water in WATER_MODES else None
            self.switch.publish()
            self._frost(d)
        for ch in ('heat', 'fan', 'room', 'water'):
            s[so(ch, 'Status')] = 0x09 if live else 0x20   # on/ok, or "not available"
        self._alarm(d if live else {})

    def _alarm(self, d):
        """W code -> warning, E code -> alarm in Venus OS (notification with the code and meaning)."""
        text = code_text(d)
        level = 0 if not text else (2 if (d.get('ecls') or 'W') == 'E' else 1)
        if text != self._last_code:
            if text:
                log.warning('heater %s: %s', 'error' if level == 2 else 'warning', text)
            elif self._last_code:
                log.info('heater code cleared (%s)', self._last_code)
            self._last_code = text
        self.alarm.publish(level, ('%s: %s' % (self.name, text)) if text else self.name)

    def _frost(self, d):
        """Frost guard: room too cold with the heating off -> switch it on, once per cold spell."""
        room = d.get('room')
        if not self.frost_guard or room is None:
            return
        if room < self.frost_c and not d.get('heat') and not d.get('pend'):
            if not self._frost_sent:
                self._frost_sent = True
                log.warning('frost guard: room %.1f C below %.1f C, heating on', room, self.frost_c)
                self.request(heat=True, room=self.frost_c + 2)
        elif room >= self.frost_c + 1:
            self._frost_sent = False
