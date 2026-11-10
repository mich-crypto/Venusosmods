# CLAUDE.md

## Battery driver: dbus-serialbattery

The JK BMS is read by **dbus-serialbattery** by mr-manuel. It is not part of this
repo; the web UI only reads and edits its settings.

- Documentation (how to connect a BMS, incl. JK BMS over Bluetooth):
  https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/connect/
- Source: https://github.com/mr-manuel/venus-os_dbus-serialbattery
- All settings and their help text: `dbus-serialbattery/config.default.ini` in that repo.

Use these as the reference for anything about the driver's settings or behaviour.

Versions: the user runs the **stable** releases only, never the development branch.
The latest stable release is v2.0.20250729 (check GitHub releases before relying on
this). The setup guide prototype and `tests/data/serialbattery_config.full.ini` follow
that release exactly: same settings, same order, same defaults. When a new stable
release comes out, update both from its `config.default.ini`. Do not take settings or
defaults from master (2.1.x dev): it has 26 extra settings and 4 other defaults, and
a setting the installed driver does not know makes it block charging and discharging
(error 119). The Venus page always reads the installed driver's own
`config.default.ini`, so it only shows what that version has. The Overview tab shows
the installed version (D-Bus `/FirmwareVersion`, else `DRIVER_VERSION` in `utils.py`)
and can update to the latest stable release with the driver's own
`install.sh --stable`, which keeps `config.ini`; the page saves a copy in `state/` first.

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

## After a driver update

A setting in `config.ini` that the installed driver does not know makes dbus-serialbattery
block charging and discharging (error 119). So:
- `config_tools.settings_findings()` reports every unknown setting as `block` (so no save
  can add one, the raw editor included) and says when a setting the safety check needs is
  missing from the installed driver.
- The web UI keeps the defaults the user last reviewed in
  `state/serialbattery-defaults-reviewed.ini` (a copy, never the driver's file). When the
  driver's `config.default.ini` differs, the Battery tab shows what was added, removed and
  changed (`defaults_diff()`), with "Keep old value" for changed defaults the user relies on
  and "Mark as reviewed". Unknown settings get a Remove button
  (`POST /api/serialbattery_remove`, which only removes unknown keys). The Overview card
  links there.
- The setup guide never exports a setting it does not know.

## History and rollback

Every write of the driver's `config.ini` (settings form, raw editor, removing unknown
settings, a rollback) goes through `web_ui.write_sb_config()`, which first keeps the
version it replaces in `state/serialbattery-history/` (last 30). The Battery tab lists
them under "Earlier versions of config.ini"; a rollback
(`POST /api/serialbattery_rollback`) goes through the same safety gate as any save.

When changing these rules, keep `docs/prototypes/jk-bms-setup-guide.html`
(`checks()`) in step, and back new limits with the cell datasheet or the driver docs.
- Bluetooth/CAN changes (`BLUETOOTH_BMS`, `BLUETOOTH_USE_USB`, `CAN_PORT`) need a
  reboot; other changes need a driver restart.
