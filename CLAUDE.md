# CLAUDE.md

## Battery driver: dbus-serialbattery

The JK BMS is read by **dbus-serialbattery** by mr-manuel. It is not part of this
repo; the web UI only reads and edits its settings.

- Documentation (how to connect a BMS, incl. JK BMS over Bluetooth):
  https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/connect/
- Source: https://github.com/mr-manuel/venus-os_dbus-serialbattery
- All settings and their help text: `dbus-serialbattery/config.default.ini` in that repo.

Use these as the reference for anything about the driver's settings or behaviour.

On the Pi:
- Install folder: `/data/apps/dbus-serialbattery/` (`config_tools.find_serialbattery()`
  also checks `/data/etc/` and `/data/`).
- Two files there:
  - `config.ini`: the working settings, holding only the keys that differ from the
    defaults. This is the only file the web UI writes.
  - `config.default.ini`: the driver's defaults. **Never write, edit or replace it.**
    It is read only; `config_tools.write_file()` refuses it.

## Battery safety

Wrong driver settings can damage the LiFePO4 cells, so every save of `config.ini`
(settings form and raw editor) goes through `config_tools.battery_safety()` and
`safety_gate()`:
- `block`: can damage the cells (e.g. > 3.65 V per cell, charging at or below 0 °C,
  charge current > 1 C or above the BMS rating). Refused, no override.
- `danger`: risky (e.g. full charge within 50 mV of the BMS OVP, > 0.5 C, a
  protection switched off). Saved only with the `confirm` token after the user
  confirms in the page.
- `warn`: shown, saved normally.

Only findings the change brings in count, so an old finding never blocks
unrelated saves. The checks use the battery facts the user enters (cells, Ah,
cell OVP/UVP, BMS rated current), stored in `state/battery_facts.json`, and fall
back to what the BMS reports on D-Bus. `config_tools.venus_checks()` checks the
Venus OS side: DVCC on, dbus-serialbattery as controlling BMS and battery monitor,
and no charge current allowed at or below 0 °C.

When changing these rules, keep `docs/prototypes/jk-bms-setup-guide.html`
(`checks()`) in step, and back new limits with the cell datasheet or the driver docs.
- Bluetooth/CAN changes (`BLUETOOTH_BMS`, `BLUETOOTH_USE_USB`, `CAN_PORT`) need a
  reboot; other changes need a driver restart.
