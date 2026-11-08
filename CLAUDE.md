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
- User settings: `config.ini` in that folder, holding only the keys that differ
  from `config.default.ini`.
- Bluetooth/CAN changes (`BLUETOOTH_BMS`, `BLUETOOTH_USE_USB`, `CAN_PORT`) need a
  reboot; other changes need a driver restart.
