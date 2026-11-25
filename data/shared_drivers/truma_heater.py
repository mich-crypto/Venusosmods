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

from venus_service import SwitchDevice, TemperatureDevice, TEMPERATURE_TYPES

log = logging.getLogger('truma')

WATER_MODES = ('off', 'eco', 'hot', 'boost')
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


def set_command(heat=None, room=None, water=None):
    """The sensor command for a change ('truma set heat=on room=21 water=eco'), or None."""
    parts = []
    if heat is not None:
        parts.append('heat=%s' % ('on' if heat else 'off'))
    if room is not None:
        parts.append('room=%d' % max(ROOM_MIN, min(ROOM_MAX, int(round(float(room))))))
    if water is not None:
        if water not in WATER_MODES:
            raise ValueError('water mode %r' % water)
        parts.append('water=%s' % water)
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
        self.devices = [self.room, self.switch]

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
        self._add_channel(svc, 'heat', 'Heating', self.TYPE_TOGGLE,
                          lambda p, v: self.request(heat=bool(int(v))))
        self._add_channel(svc, 'room', 'Room set point', self.TYPE_SETPOINT,
                          lambda p, v: self.request(room=v), ROOM_MIN, ROOM_MAX)
        self._add_channel(svc, 'water', 'Hot water (0 off, 1 eco, 2 hot, 3 boost)', self.TYPE_SETPOINT,
                          lambda p, v: self.request(water=WATER_MODES[max(0, min(3, int(round(float(v)))))]), 0, 3)

    def request(self, heat=None, room=None, water=None):
        """A change from the switch pane or the page: one command to the sensor."""
        try:
            cmd = set_command(heat, room, water)
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
            s[so('room', 'Dimming')] = d.get('set')
            s[so('room', 'Measurement')] = d.get('room')
            water = d.get('water')
            s[so('water', 'Dimming')] = WATER_MODES.index(water) if water in WATER_MODES else None
            self.switch.publish()
            self._frost(d)
        for ch in ('heat', 'room', 'water'):
            s[so(ch, 'Status')] = 0x09 if live else 0x20   # on/ok, or "not available"

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
