"""Tests for the settings file handling (config_tools). No D-Bus, no web."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import config_tools as ct  # noqa: E402

with open(os.path.join(HERE, 'data', 'serialbattery_config.default.ini')) as f:
    DEFAULT = f.read()
with open(os.path.join(HERE, '..', 'data', 'shared_drivers', 'config.ini.example')) as f:
    EXAMPLE = f.read()


class IniEditingTests(unittest.TestCase):
    TEXT = ('; my notes\n[one]\na = 1\n# keep me\nb = 2\n\n[two]\nb = 20\nc = 30\n')

    def test_parse(self):
        p = ct.parse_ini(self.TEXT)
        self.assertEqual(p, {'one': {'a': '1', 'b': '2'}, 'two': {'b': '20', 'c': '30'}})

    def test_replace_keeps_comments_and_other_sections(self):
        out = ct.set_values(self.TEXT, 'one', {'b': '99'})
        self.assertEqual(out, self.TEXT.replace('b = 2\n', 'b = 99\n', 1))
        self.assertIn('# keep me', out)
        self.assertEqual(ct.parse_ini(out)['two']['b'], '20')

    def test_add_delete_and_new_section(self):
        out = ct.set_values(self.TEXT, 'one', {'d': '4', 'a': None})
        self.assertEqual(ct.parse_ini(out)['one'], {'b': '2', 'd': '4'})
        # new keys go before the blank line that separates the next section
        self.assertIn('d = 4\n\n[two]', out)
        out = ct.set_values(self.TEXT, 'three', {'x': '1'})
        self.assertTrue(out.endswith('[three]\nx = 1\n'))
        self.assertEqual(ct.parse_ini(out)['three'], {'x': '1'})

    def test_case_insensitive_and_continuation_lines(self):
        text = '[DEFAULT]\nMy_Key = a\n    continued\nother = 1\n'
        out = ct.set_values(text, 'default', {'my_key': 'b'})
        self.assertEqual(out, '[DEFAULT]\nMy_Key = b\nother = 1\n')

    def test_check_syntax(self):
        self.assertEqual(ct.check_syntax('[a]\nb = 1\n'), '')
        self.assertIn('section', ct.check_syntax('b = 1\n'))
        self.assertIn('%', ct.check_syntax('[a]\nb = 50%\n') + '%')   # interpolation error

    def test_write_file_keeps_backup(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'config.ini')
        ct.write_file(p, 'one\n')
        ct.write_file(p, 'two\n')
        self.assertEqual(open(p).read(), 'two\n')
        self.assertEqual(open(p + '.bak').read(), 'one\n')


class DriverSchemaTests(unittest.TestCase):
    def test_example_config_is_understood(self):
        cfg = ct.read_driver_config(EXAMPLE)
        kinds = dict((c['section'], c['kind']) for c in cfg)
        self.assertEqual(kinds, {'mopeka_standard': 'mopeka', 'esp32_hydro': 'esp32',
                                 'fridge': 'fridge', 'web': 'web', 'starlink': 'starlink',
                                 'telegram': 'telegram'})
        mop = [c for c in cfg if c['kind'] == 'mopeka'][0]
        self.assertEqual(mop['values']['capacity_l'], '27')
        self.assertTrue(any(f['key'] == 'full_mm' for f in mop['fields']))

    def test_every_key_in_the_example_is_in_the_schema(self):
        for section, values in ct.parse_ini(EXAMPLE).items():
            keys = set(f['key'] for f in ct.SCHEMA[ct.kind_of(section, values)]['fields'])
            for key in values:
                self.assertIn(key, keys, '%s.%s missing from the schema' % (section, key))

    def test_kind_of(self):
        self.assertEqual(ct.kind_of('mopeka_bottle2'), 'mopeka')
        self.assertEqual(ct.kind_of('anything', {'driver': 'fridge'}), 'fridge')
        self.assertEqual(ct.kind_of('web'), 'web')
        self.assertIsNone(ct.kind_of('unrelated'))

    def test_validation(self):
        clean, errors = ct.validate_section('mopeka', {
            'capacity_l': '24', 'propane_ratio': '1.5', 'empty_mm': 'abc', 'mac': 'aa:bb:cc:dd:ee:ff',
            'median_window': '5.5', 'level_curve': '0:0, 1:100', 'service': 'hack'})
        self.assertEqual(clean, {'capacity_l': '24', 'mac': 'AA:BB:CC:DD:EE:FF',
                                 'level_curve': '0:0, 1:100'})
        self.assertEqual(sorted(errors), ['empty_mm', 'median_window', 'propane_ratio'])
        self.assertNotIn('service', clean)                           # read-only

    def test_validation_rules(self):
        _, errors = ct.validate_section('mopeka', {'empty_mm': '300', 'full_mm': '200'})
        self.assertIn('full_mm', errors)
        _, errors = ct.validate_section('mopeka', {'level_curve': '0:0'})
        self.assertIn('level_curve', errors)
        _, errors = ct.validate_section('fridge', {'mac': ''})
        self.assertIn('mac', errors)                                   # required
        clean, errors = ct.validate_section('fridge', {
            'control': 'TRUE', 'poll_interval': '300', 'zone1_type': 'freezer'})
        self.assertEqual((clean, errors), ({'control': 'yes', 'poll_interval': '300',
                                            'zone1_type': 'freezer'}, {}))
        _, errors = ct.validate_section('fridge', {'zone1_type': 'ufo', 'poll_interval': '5',
                                                   'zone1_name': 'a\nb', 'zone2_name': '50%'})
        self.assertEqual(sorted(errors), ['poll_interval', 'zone1_name', 'zone1_type', 'zone2_name'])
        clean, errors = ct.validate_section('web', {'port': '80'})
        self.assertIn('port', errors)                                  # below 1024


class SerialBatteryTests(unittest.TestCase):
    def setUp(self):
        self.groups = ct.parse_default_ini(DEFAULT)
        self.entries = dict((e['key'], e) for g in self.groups for e in g['entries'])

    def test_parse_default(self):
        titles = [g['title'] for g in self.groups]
        self.assertEqual(titles[:3], ['Set logging level', 'Battery Current Limits', 'Cell Voltages'])
        e = self.entries
        self.assertEqual(e['MAX_BATTERY_CHARGE_CURRENT']['default'], '50.0')
        self.assertEqual(e['MAX_BATTERY_CHARGE_CURRENT']['type'], 'float')
        self.assertEqual(e['SOC_CALCULATION']['type'], 'bool')
        self.assertEqual(e['SOC_RESET_AFTER_DAYS']['type'], 'int')          # empty default, known numeric
        self.assertEqual(e['CURRENT_REPORTED_BY_BMS']['type'], 'numlist')
        self.assertEqual(e['LOGGING']['options'], ['ERROR', 'WARNING', 'INFO', 'DEBUG'])
        self.assertEqual(e['BLUETOOTH_BMS']['special'], 'bms_list')
        self.assertIn('Jkbms_Ble', e['BLUETOOTH_BMS']['help'])
        self.assertIn('Maximum voltage', e['MAX_CELL_VOLTAGE']['help'])
        self.assertEqual(e['MIN_CELL_VOLTAGE']['label'], 'Min cell voltage')
        # every key appears once and belongs to a group
        self.assertEqual(len(self.entries), sum(len(g['entries']) for g in self.groups))

    def test_view_with_overrides(self):
        user = '[DEFAULT]\nmax_battery_charge_current = 30\nBLUETOOTH_BMS = Jkbms_Ble C8:47:8C:ED:1B:FB\nMY_OWN = x\n'
        v = ct.serialbattery_view(DEFAULT, user)
        by = dict((e['key'], e) for g in v['groups'] for e in g['entries'])
        self.assertEqual(by['MAX_BATTERY_CHARGE_CURRENT']['value'], '30')   # case-insensitive match
        self.assertIsNone(by['MAX_BATTERY_DISCHARGE_CURRENT']['value'])     # default in use
        self.assertEqual(v['unknown'], {'my_own': 'x'})
        self.assertEqual(v['essentials'][:3], ['BLUETOOTH_BMS', 'MAX_BATTERY_CHARGE_CURRENT',
                                               'MAX_BATTERY_DISCHARGE_CURRENT'])

    def test_apply_creates_file_with_only_changes(self):
        text, errors = ct.apply_serialbattery(DEFAULT, '', {
            'MAX_BATTERY_CHARGE_CURRENT': '40', 'SOC_CALCULATION': 'true',
            'MAX_CELL_VOLTAGE': '3.45',          # equals the default (3.450): not written
            'LOGGING': 'debug'})
        self.assertEqual(errors, {})
        self.assertEqual(ct.parse_ini(text)['DEFAULT'],
                         {'max_battery_charge_current': '40', 'soc_calculation': 'True',
                          'logging': 'DEBUG'})
        self.assertTrue(text.startswith('[DEFAULT]\n'))
        self.assertEqual(ct.check_syntax(text), '')

    def test_apply_keeps_comments_and_resets_to_default(self):
        user = '[DEFAULT]\n; my battery is 16s\nMAX_BATTERY_CHARGE_CURRENT = 30\nLOGGING = DEBUG\n'
        text, errors = ct.apply_serialbattery(DEFAULT, user, {
            'MAX_BATTERY_CHARGE_CURRENT': '35', 'LOGGING': None})
        self.assertEqual(errors, {})
        self.assertEqual(text, '[DEFAULT]\n; my battery is 16s\nMAX_BATTERY_CHARGE_CURRENT = 35\n')
        # setting a value equal to the default removes the override too
        text, _ = ct.apply_serialbattery(DEFAULT, text, {'MAX_BATTERY_CHARGE_CURRENT': '50'})
        self.assertEqual(text, '[DEFAULT]\n; my battery is 16s\n')

    def test_apply_rejects_bad_values_and_leaves_text_alone(self):
        user = '[DEFAULT]\nLOGGING = DEBUG\n'
        text, errors = ct.apply_serialbattery(DEFAULT, user, {
            'MAX_BATTERY_CHARGE_CURRENT': 'lots', 'SOC_CALCULATION': 'maybe', 'LOGGING': 'LOUD',
            'NOT_A_KEY': '1', 'SOC_RESET_AFTER_DAYS': '2.5', 'CURRENT_REPORTED_BY_BMS': '1;2'})
        self.assertEqual(text, user)
        self.assertEqual(sorted(errors), ['CURRENT_REPORTED_BY_BMS', 'LOGGING',
                                          'MAX_BATTERY_CHARGE_CURRENT', 'NOT_A_KEY',
                                          'SOC_CALCULATION', 'SOC_RESET_AFTER_DAYS'])

    def test_bluetooth_bms_format(self):
        ok, errors = ct.apply_serialbattery(DEFAULT, '', {
            'BLUETOOTH_BMS': 'Jkbms_Ble C8:47:8C:ED:1B:FB, Jkbms_Ble C8:47:8C:00:00:11'})
        self.assertEqual(errors, {})
        self.assertIn('BLUETOOTH_BMS = Jkbms_Ble C8:47:8C:ED:1B:FB, Jkbms_Ble C8:47:8C:00:00:11', ok)
        _, errors = ct.apply_serialbattery(DEFAULT, '', {'BLUETOOTH_BMS': 'Jkbms_Ble nonsense'})
        self.assertIn('BLUETOOTH_BMS', errors)

    def test_paired_lists_must_match(self):
        _, errors = ct.apply_serialbattery(DEFAULT, '', {
            'CELL_VOLTAGES_WHILE_CHARGING': '3.5, 3.4, 3.3'})       # fractions still has 5
        self.assertIn('CELL_VOLTAGES_WHILE_CHARGING', errors)
        text, errors = ct.apply_serialbattery(DEFAULT, '', {
            'CELL_VOLTAGES_WHILE_CHARGING': '3.5, 3.4, 3.3',
            'MAX_CHARGE_CURRENT_CV_FRACTION': '0, 0.5, 1'})
        self.assertEqual(errors, {})
        self.assertEqual(ct.parse_ini(text)['DEFAULT']['cell_voltages_while_charging'], '3.5, 3.4, 3.3')

    def test_same_value_comparison(self):
        e = self.entries
        self.assertTrue(ct.same_value(e['MAX_CELL_VOLTAGE'], '3.45', '3.450'))
        self.assertTrue(ct.same_value(e['CURRENT_REPORTED_BY_BMS'], '-300,300', '-300, 300.0'))
        self.assertTrue(ct.same_value(e['SOC_CALCULATION'], 'false', 'False'))
        self.assertFalse(ct.same_value(e['MAX_CELL_VOLTAGE'], '3.5', '3.450'))

    def test_secret_and_reboot_keys(self):
        self.assertTrue('BLUETOOTH_BMS' in ct.REBOOT_KEYS)
        self.assertEqual(ct.find_serialbattery(('/nonexistent',)), None)
        d = os.path.join(HERE, 'data')
        self.assertEqual(ct.find_serialbattery((d,)), None)          # no config.default.ini there


if __name__ == '__main__':
    unittest.main()
