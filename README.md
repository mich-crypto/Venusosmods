# Venus OS mods: custom BLE tank sensors for the camper van

Two drivers that make sensors Venus OS does not support show up as normal
tanks in the Remote Console, on VRM and on D-Bus (and therefore Modbus/MQTT):

| Sensor | D-Bus service | Fluid |
|---|---|---|
| Mopeka Check **Standard / XL** (original, not Pro) | `com.victronenergy.tank.mopeka_standard` | LPG (8) |
| Self-built ESP32-S3 hydrostatic sender | `com.victronenergy.tank.esp32_hydro` | Fresh water (1) |

```
data/                       -> copy to /data on the Pi
  shared_drivers/
    venus_tank_base.py      base class: vedbus service, settings, timeout, GLib loop
    ble_scanner.py          passive BLE listener via BlueZ D-Bus (no bleak)
    mopeka_std_parser.py    Mopeka Standard packet decoder
    mopeka_venus.py         Mopeka driver
    esp32_hydro_protocol.py ESP32 packet format (shared spec with the firmware)
    esp32_hydro_tank.py     ESP32 driver
    config.ini.example      per-sensor settings
    install.sh / uninstall.sh
  etc/init.d/<service>/run  daemontools services (+ log/run)
firmware/esp32_hydro_tank/  Arduino sketch for the ESP32-S3
docs/wiring.md              schematics for buck converter, sensor and DS18B20
tests/                      off-device unit tests (python3 -m unittest discover tests)
```

## Changes compared to the original plan

* **mopeka-iot-ble cannot decode the original Mopeka Standard.** It only knows
  the Pro series (company id 0x0059). The Standard sends a different format
  under company id 0x000D. `mopeka_std_parser.py` is a port of ESPHome's
  `mopeka_std_check` decoder, which does support it.
* **No bleak / asyncio.** BlueZ already publishes every advertisement on the
  system D-Bus, so the drivers subscribe to it from the same GLib loop that
  vedbus uses. That removes the event-loop conflict completely, needs no
  `pip install` and does not break when a Venus OS update replaces the root
  file system.
* **FluidType for LPG is 8**, not 2 (2 is waste water).
* **Symlinks in `/service` do not survive a reboot.** `install.sh` adds them
  to `/data/rc.local`, which Venus OS runs at every boot.
* Device instance and custom name are stored in Venus OS localsettings, so
  you can rename the tanks in the GUI and VRM keeps the same instance.

## 1. Prepare Venus OS

1. Get root SSH access: set the access level to *Superuser*, set a root
   password and enable *SSH on LAN* in *Settings → General*. The unlock
   procedure differs between GUI versions; Victron's guide "Venus OS: Root
   Access" describes it.
2. *Settings → Bluetooth*: enabled.
3. Find your Mopeka's MAC address. Press the sync button on the sensor and run:
   ```sh
   ssh root@venus.local
   bluetoothctl scan le        # look for a device that appears when you press it
   ```

## 2. Install the drivers

From your PC, in this repo:
```sh
scp -r data/shared_drivers data/etc root@venus.local:/data/
ssh root@venus.local sh /data/shared_drivers/install.sh
```
The first run creates `/data/shared_drivers/config.ini`. Edit it
(`nano` or `vi`): set the Mopeka `mac`, bottle size and propane ratio. Then
enable only the services you use:
```sh
sh /data/shared_drivers/install.sh mopeka_standard esp32_hydro
```

Check that they are running:
```sh
svstat /service/mopeka_standard /service/esp32_hydro
tail -F /var/log/mopeka_standard/current | tai64nlocal
dbus-spy                                    # browse com.victronenergy.tank.*
```
For verbose packet output, stop the service and run it by hand:
```sh
svc -d /service/mopeka_standard
python3 /data/shared_drivers/mopeka_venus.py --section mopeka_standard --debug
svc -u /service/mopeka_standard
```

**Several sensors of the same kind:** copy the config section (for example
`[mopeka_bottle2]` with its own `service = mopeka_bottle2` and `mac`), copy the
service folder `data/etc/init.d/mopeka_standard` to `.../mopeka_bottle2`,
change `--section` in its `run` file and the log path in `log/run`, then run
`install.sh mopeka_bottle2`.

Uninstall: `sh /data/shared_drivers/uninstall.sh [service ...]`.

## 3. Calibrate the Mopeka

The level is calculated from the measured liquid height. Watch
`/Mopeka/DistanceMm` in `dbus-spy`:
* with an almost empty bottle → `empty_mm`
* with a freshly filled bottle (filled to ~80 %) → `full_mm`

Upright bottles are not cylinders at the top and bottom. For a better curve,
use `level_table = distance:percent, ...` instead. `/Mopeka/EchoQuality`
shows the echo strength. If it is low, put more coupling gel on the sensor and
clean the bottle bottom.

## 4. ESP32-S3 water tank sender

Hardware and wiring: see [docs/wiring.md](docs/wiring.md).

Arduino IDE:
1. Boards Manager: install **esp32 by Espressif** (2.0.x or 3.x both work).
2. Library Manager: **OneWire** and **DallasTemperature** (only if you use
   the DS18B20; otherwise set `USE_DS18B20 0`).
3. Open `firmware/esp32_hydro_tank/esp32_hydro_tank.ino`, board
   *ESP32S3 Dev Module*, enable *USB CDC On Boot* for serial output over the
   native USB-C port.
4. Edit the configuration block at the top: input type, sensor range, and
   the `TANK_TABLE` for your tank.

Calibrating the tank:
1. Empty tank: read `height=` in the serial monitor and set `ZERO_OFFSET_MM`
   so that it reads ~0.
2. Fill the tank in known steps (a 10 L bucket works well) and note the
   height after each one. Those pairs become `TANK_TABLE`. The last row is
   the capacity (100 %).

The filter: every 250 ms the ESP takes 31 ADC readings and uses their median,
which removes noise and spikes. An exponential moving average then removes
waves. When the readings start jumping (driving), the averaging switches to a
~25 s time constant and the `Sloshing` flag is set. When parked, it settles
back to ~1.6 s. The height trend over the last 60 s sets the `Filling` and
`Draining` flags.

Extra D-Bus paths: `/Hydro/HeightMm`, `/Hydro/Filling`, `/Hydro/Draining`,
`/Hydro/Sloshing`, `/Hydro/Fault`. A sensor fault (open wire) shows as tank
status *error* rather than an empty tank.

## Tests

```sh
python3 -m unittest discover tests -v
```
These test the packet decoders and driver logic with fake D-Bus objects, so
they run on any PC.

`tests/integration_dbus.py` goes further: it runs both real drivers with the
real velib_python on a private D-Bus, against a fake BlueZ and a fake
localsettings, and checks the published tank values (see the file header for
how to run it).
