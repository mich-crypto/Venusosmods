# Venus OS mods: tank sensors and fridge for the camper van

Drivers that make devices Venus OS does not support show up as normal tanks
and temperature sensors in the Remote Console, on VRM, and on D-Bus (and so
on Modbus-TCP/MQTT):

| Device | D-Bus service(s) | Shown as |
|---|---|---|
| Mopeka Check **Standard / XL** (original, not Pro) | `com.victronenergy.tank.mopeka_standard` | LPG tank |
| Self-built **ESP32-S3 hydrostatic** water sensor | `com.victronenergy.tank.esp32_hydro` (+ `.temperature.esp32_hydro_temp`) | fresh-water tank (+ water temperature) |
| 12 V compressor **fridge** with the Alpicool Bluetooth controller (Hyckes: check first) | `com.victronenergy.temperature.fridge_zone1` / `_zone2` | fridge / freezer temperature |

**→ Start with the [step-by-step guide](docs/GUIDE.md)**, also as a printable
**[PDF](docs/Venus-OS-Mods-Guide.pdf)** (guide + wiring + calibration in one file).
Also: [wiring](docs/wiring.md) · [tank calibration](docs/calibration.md)

The PDF is generated from the Markdown files: `cd tools/pdf && npm install && npm run build`.

## Layout

```
data/                              -> copied to /data on the Pi
  shared_drivers/
    venus_service.py               shared layer: VenusDevice, TankDevice, TemperatureDevice, BleDriver
    ble_scanner.py                 passive BLE listener via BlueZ D-Bus (GLib loop, no bleak)
    ble_gatt.py                    BLE GATT client via BlueZ D-Bus (for the fridge)
    mopeka_std_parser.py           decoder: Mopeka Standard advertisements
    esp32_hydro_protocol.py        decoder: ESP32 advertisement (shared spec with the firmware)
    alpicool_protocol.py           decoder: fridge frames
    mopeka_venus.py                driver: Mopeka  -> tank
    esp32_hydro_tank.py            driver: ESP32   -> tank (+ temperature)
    fridge_venus.py                driver: fridge  -> temperature per zone
    diagnostics/
      test_venus_compatibility.py  read-only pre-flight check, PASS/WARN/FAIL, optional BLE scan
      fridge_probe.py              does my fridge speak the supported protocol?
    config.ini.example
    install.sh / uninstall.sh
  etc/init.d/<service>/run         daemontools services (+ log/run)
firmware/esp32_hydro_tank/         Arduino sketch for the ESP32-S3 (calibration via serial)
tools/tank_table.py                height->litres table from tank dimensions
docs/                              guide, wiring, calibration
tests/                             unit tests + D-Bus integration test with simulated sensors
```

The layers are separate. Parsers (pure Python, no D-Bus) turn bytes into
readings. Devices publish one Victron D-Bus service each. Drivers connect
the two, one process per sensor. A new sensor type needs a parser and a
small driver; everything else is shared.

## Design decisions and review points

| Topic | How it is handled |
|---|---|
| Mopeka Standard | `mopeka-iot-ble` only decodes the Pro series. The Standard (company id 0x000D) decoder is a port of ESPHome's `mopeka_std_check`. |
| asyncio vs GLib | No bleak. BlueZ publishes advertisements and GATT on the system D-Bus, so everything runs in the GLib loop vedbus needs. No threads, no `pip install`, survives Venus OS updates. |
| Units | Config in litres. D-Bus `/Capacity` and `/Remaining` in m³ (60 L → 0.06), `/Level` in %. |
| Fluid types | Victron enum: 0 fuel, 1 fresh water, 2 waste water, 5 black water, 8 LPG, 11 raw water. |
| Metadata | velib_python `add_mandatory_paths` (Mgmt/*, DeviceInstance, ProductId, ProductName, Connected...), `/CustomName`, `/Status`. Each service is registered only after all its paths exist (`register=False` + `register()`). |
| Device instance | Stored in localsettings `/Settings/Devices/<id>/ClassAndVrmInstance`, which also resolves clashes. Stable across reboots, changeable in the GUI. |
| Several services | Each service has its own private D-Bus connection (vedbus object paths are per connection) and a unique, stable name from config. |
| velib path | Searched in `ext/velib_python`, `dbus-systemcalc-py` and `/opt/victronenergy/*/ext/velib_python`. The diagnostics report which one is used. |
| Sensor health | Per-device last-seen time. After `timeout`: `/Connected` = 0, `/Status` = disconnected, values set to invalid. Recovers on the next valid reading. Sensor faults show as `/Status` error, never as "empty". |
| Temperature | The tank `/Temperature` path is documented for Mopeka only, so the ESP32 water temperature is optionally also published as its own `com.victronenergy.temperature` service. DS18B20: CRC is checked, 85 °C power-on values are rejected, and the value is dropped after 3 bad reads. |
| Shutdown | SIGTERM (`svc -d`) is handled: the fridge connection is closed cleanly. |
| Persistence | Services in `/data/etc/init.d`, linked into `/service` and re-linked at boot by `/data/rc.local`. |
| Config format | INI rather than JSON: it can have comments and is easier to edit with `nano` over SSH. Checked by the diagnostics (MAC format, unique services, value types). |
| Non-square tank | Height→litres table measured by filling in steps (serial commands, stored in ESP32 flash) or calculated from dimensions with `tools/tank_table.py`. |

## Tests

```sh
python3 -m unittest discover tests -v
```
These test the decoders (including frames captured from real fridges) and the
driver logic with fake D-Bus objects, so they run on any PC.

`tests/integration_dbus.py` runs all three real drivers with the real
velib_python on a private D-Bus. A fake BlueZ sends Mopeka/ESP32
advertisements and plays a dual-zone fridge over GATT, and the test checks
every published value plus the clean shutdown:
```sh
git clone https://github.com/victronenergy/velib_python /tmp/velib_python
VELIB=/tmp/velib_python dbus-run-session -- python3 tests/integration_dbus.py
```

The firmware compiles warning-free on Arduino-ESP32 2.0.17 (Bluedroid) and
3.3.x (NimBLE).

Still to verify on the real installation: the GUI/VRM display on your Venus
OS version, Mopeka Standard decoding against your sensor, whether the Hyckes
fridge uses the Alpicool controller (`fridge_probe.py`), and BLE coexistence
with Venus OS's own Bluetooth services.
