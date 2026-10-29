# Step-by-step installation guide

This guide takes you from parts on the table to tanks and the fridge on your
Venus OS screen and in VRM. Work through it in order. Each part ends with a
check, so you know it works before moving on.

| Part | What | Time |
|---|---|---|
| 1 | Prepare Venus OS (root access, SSH, Bluetooth) | 15 min |
| 2 | Build and wire the ESP32 water sensor | 1–2 h |
| 3 | Upload the firmware to the ESP32 | 20 min |
| 4 | Calibrate the water tank | 30 min |
| 5 | Install the drivers on Venus OS | 20 min |
| 6 | Mopeka gas sensor | 10 min |
| 7 | Fridge | 10 min |
| 8 | Updates, troubleshooting, removal | – |

You need: the Raspberry Pi running Venus OS on the same network (Wi-Fi or
LAN) as a laptop, a USB-C **data** cable (some cables only charge), and
the parts from [wiring.md](wiring.md).

---

## Part 1 – Prepare Venus OS

1. Open the Remote Console: on the Pi's screen, or in a browser at
   `http://venus.local` (or the Pi's IP address).
2. **Enable superuser access.** In *Settings → General*:
   1. Set *Access level* to *User & Installer* (password `ZZZ`).
   2. Highlight *Access level* without opening it, then press and hold the
      **right arrow** key for 5 seconds. *Superuser* now appears.
   3. Choose *Superuser*, then *Set root password*.
   4. Turn on *SSH on LAN*.

   Menus differ slightly between Venus OS versions. Victron's
   "Venus OS: Root Access" documentation has the details for each.
3. **Bluetooth:** *Settings → Bluetooth* → enabled.
4. **Check:** on the laptop, open a terminal (Windows: PowerShell) and run:
   ```sh
   ssh root@venus.local
   ```
   If `venus.local` isn't found, use the IP address from *Settings →
   Ethernet / Wi-Fi*. You should get a `root@raspberrypi...:~#` prompt.
   Type `exit` to leave.

---

## Part 2 – Build and wire the ESP32 water sensor

Follow [wiring.md](wiring.md). In short:

1. Set the buck converter to **5.0 V** with a multimeter *before* connecting
   anything to it.
2. Wire buck → diode → ESP32 `5V`, plus GND.
3. Connect the pressure sensor (option C: 0–3.3 V like the QDY30A, A: 0.5–4.5 V,
   or B: 4–20 mA) to GPIO4
   through the resistors.
4. Optional: DS18B20 to GPIO5 with the 4.7 kΩ pull-up to 3V3.
5. Don't put the sensor in the tank yet; it's easier to test on the table.

**Check:** with the 12 V on, the ESP32's power LED is on and the buck
converter still reads 5.0 V (about 4.7 V after the diode).

---

## Part 3 – Upload the firmware to the ESP32

### 3.1 Install the Arduino IDE (once)

1. Download **Arduino IDE 2** from arduino.cc and install it.
2. *File → Preferences → Additional boards manager URLs*, add:
   ```
   https://espressif.github.io/arduino-esp32/package_esp32_index.json
   ```
3. *Tools → Board → Boards Manager*: search **esp32**, install
   **esp32 by Espressif Systems** (3.x; 2.0.17 works too).
4. *Tools → Manage Libraries*: install **OneWire** (Paul Stoffregen) and
   **DallasTemperature** (Miles Burton). Only needed with the DS18B20.

### 3.2 Open and configure the sketch

1. Download this repository (GitHub: *Code → Download ZIP*) and unzip it.
2. *File → Open* → `firmware/esp32_hydro_tank/esp32_hydro_tank.ino`.
3. In the **CONFIGURATION** block at the top, set:
   * `INPUT_CURRENT_LOOP` and the `SIGNAL_*` values for your sensor. The
     defaults are for the QDY30A 0–3.3 V, 2 m range (option C). The values
     for the other options are in [wiring.md](wiring.md).
   * `SENSOR_RANGE_MM`: the sensor's full scale in mm of water. From the
     datasheet: 1 m H₂O = 1000 mm, 10 kPa ≈ 1020 mm, 0.1 bar ≈ 1020 mm.
   * `USE_DS18B20`: `1` with the probe, `0` without
   * `DEVICE_ID`: leave at `1` unless you build a second sender
   * `DEFAULT_TABLE`: leave as it is. You calibrate later over USB, without
     editing code.

### 3.3 Board settings (*Tools* menu)

| Setting | Value |
|---|---|
| Board | **ESP32S3 Dev Module** |
| USB CDC On Boot | **Enabled** (otherwise you see no serial output) |
| Flash Size | match your board (4 MB / 8 MB / 16 MB; 4 MB always works) |
| Partition Scheme | Default |
| Upload Speed | 921600 (use 115200 if uploads fail) |
| Port | the new COM / `/dev/ttyACM*` / `/dev/cu.usbmodem*` port |

Many S3 boards have **two** USB-C ports, labelled *USB* and *UART* (or
*COM*). Either works for uploading. With *USB CDC On Boot: Enabled* the
Serial Monitor works on the **USB** port. On the *UART* port, set *USB CDC On
Boot* to *Disabled*.

### 3.4 Upload

1. Unplug the buck converter's 5 V if you didn't fit the diode.
2. Connect the ESP32 by USB, choose the port, click **Upload** (→).
3. If it hangs at `Connecting....`: hold the **BOOT** button, tap **RST**,
   release BOOT, and upload again. After uploading, tap **RST** once.

### 3.5 Check

*Tools → Serial Monitor*, **115200 baud**, line ending **Both NL & CR**.
You should see:
```
Using the default tank table from the sketch (no calibration stored)
Advertising as VHT, MAC 34:85:18:xx:xx:xx
Type 'help' for calibration commands.
signal=0.512 height=3.0mm litres=0.4 level=0.5% temp=19.6 activity=0.4
```
* `signal` should be close to `SIGNAL_AT_ZERO` with the sensor in air
  (0 V for the QDY30A 0–3.3 V, 0.5 V or 4 mA for the other types).
* Dip the sensor into a bucket: `height` should rise by roughly the depth.
* `FAULT` = signal outside the fault limits: check wiring and the
  `SIGNAL_*` settings.
* `temp=nan` = no DS18B20 found (wiring, pull-up, or `USE_DS18B20 0`).

Note the **MAC** for later (optional in config.ini).

---

## Part 4 – Calibrate the water tank

**Your wheel-arch tank:** the firmware already contains its filling chart,
calculated from the technical drawing ([tank-filling-chart.md](tank-filling-chart.md)).
Put the sensor at the bottom of the tank's leg, park level, empty the tank and
type `zero`. Then check with a few buckets as described in that chapter.

For any other tank, or to measure yours exactly, follow
[calibration.md](calibration.md). Short version, with the van level and the
tank empty:

```
zero
cal start
   (pour 10 L in, wait 30 s)
cal add 10
   (repeat until full)
cal save
```

No measuring container? Use the dimension calculator in calibration.md,
method B.

---

## Part 5 – Install the drivers on Venus OS

### 5.1 Copy the files

**From an iPad or phone (Termius or another SSH app), no computer needed:**
log in to the Pi and let it download the files from GitHub itself:
```sh
cd /tmp
wget -O vom.tar.gz https://github.com/mich-crypto/Venusosmods/archive/refs/heads/master.tar.gz
tar -xzf vom.tar.gz
cp -r Venusosmods-master/data/shared_drivers Venusosmods-master/data/etc /data/
rm -rf vom.tar.gz Venusosmods-master
```
The Pi needs internet access for this (it has it if VRM works). Then continue
with 5.2.

**From a computer**, in the folder where you unzipped the repository:

**macOS / Linux / Windows PowerShell:**
```sh
scp -r data/shared_drivers data/etc root@venus.local:/data/
```
On Windows you can also use **WinSCP** (SCP protocol, user `root`): copy
the folders `data/shared_drivers` and `data/etc` into `/data/` on the Pi.

### 5.2 Create the configuration

```sh
ssh root@venus.local
sh /data/shared_drivers/install.sh
```
The first run creates `/data/shared_drivers/config.ini` and stops.

### 5.3 Find your sensors

```sh
python3 /data/shared_drivers/diagnostics/test_venus_compatibility.py --scan 30
```
Press the Mopeka's sync button during the scan. The scan lists:
* `PASS Mopeka Standard  AA:BB:...` → the gas sensor is received. You don't
  need to note the MAC: the driver pairs with the sync button (Part 6)
* `PASS ESP32 tank id 1  ...` → the water sensor is received (MAC optional)
* `PASS Fridge candidate ...` → see Part 7

The upper part of the output checks the environment (Python, D-Bus,
velib_python, Bluetooth, config). Fix any **FAIL** before continuing.

### 5.4 Edit config.ini

```sh
nano /data/shared_drivers/config.ini
```
(`Ctrl+O`, `Enter` saves, `Ctrl+X` quits.) Set the fridge MAC, the bottle size and
`propane_ratio` for the Mopeka, and names. Every option is explained in the
file. Delete or ignore sections for sensors you don't have.

### 5.5 Enable the services

Only the ones you use:
```sh
sh /data/shared_drivers/install.sh mopeka_standard esp32_hydro fridge
```
This runs the diagnostics first and refuses to enable anything if one
reports FAIL. It links the services into `/service` and adds them to
`/data/rc.local` so they come back after reboots and Venus OS updates.

### 5.6 Check

* Remote Console: the **tanks** appear on the overview page and under
  *Device list*. The water temperature and fridge zones appear as
  temperature sensors in the *Device list*.
* Logs:
  ```sh
  tail -F /var/log/esp32_hydro/current | tai64nlocal
  ```
* Live D-Bus values: `dbus-spy` (arrow keys, `Enter` opens a service,
  `q` quits).
* **VRM:** after a few minutes, the tanks and temperatures appear on the VRM
  dashboard and in *Advanced* widgets. To rename a device, use its *Device*
  page on the GX; VRM picks the new name up.
* Reboot the Pi once (`reboot`) and check everything comes back.

---

## Part 6 – Mopeka gas sensor

**Pairing (no MAC needed):** leave `mac =` empty in config.ini. After
enabling the service, press the **sync button** on the Mopeka once, with the
bottle within Bluetooth range of the Pi. The log shows
`Paired with Mopeka AA:BB:...`, and the tank appears. The MAC is stored in
Venus OS settings, so it survives reboots and updates. Other Mopeka sensors
nearby (e.g. at a campsite) are ignored.
```sh
tail -F /var/log/mopeka_standard/current | tai64nlocal    # watch it pair
```
To pair a different sensor later:
```sh
dbus -y com.victronenergy.settings /Settings/Devices/mopeka_standard/Mac SetValue ''
svc -t /service/mopeka_standard          # then press sync on the new sensor
```

1. Put the sensor on the **centre** of the bottle's bottom, with plenty of
   coupling gel (or the supplied pad).
2. In `dbus-spy`, open `com.victronenergy.tank.mopeka_standard` and watch
   `/Mopeka/DistanceMm` and `/Mopeka/EchoQuality`.
   * EchoQuality stays low (<3) or the distance jumps around: re-seat the
     sensor, use more gel, clean the bottle bottom.
3. Note the distance with a nearly **empty** bottle → `empty_mm`, and with a
   freshly **filled** bottle → `full_mm`. Gas bottles are filled to about 80 %,
   so "full" is what a new bottle reads.
4. `propane_ratio`: 1.0 for pure propane (most of Northern Europe, and
   winter gas), around 0.3–0.4 for summer propane/butane mixes (e.g.
   southern Europe). This corrects the speed of sound in the liquid.
5. `capacity_l`: liquid volume, e.g. 11 kg ≈ 27 L, 5 kg ≈ 12 L.
6. `svc -t /service/mopeka_standard` restarts the driver after changes.

---

## Part 7 – Fridge (Hyckes and other Bluetooth compressor fridges)

Many 12 V compressor fridges use the same Bluetooth controller, the one the
"Alpicool / CAR FRIDGE FREEZER" app talks to. The driver reads the
temperatures of both zones, the set points, the supply voltage and the
eco/power state. It does **not** change any fridge settings.

Hyckes's own app could not be checked here, so first test whether your
fridge uses this controller:

1. **Close the fridge app on your phone**, or switch off the phone's
   Bluetooth. The fridge accepts only one connection at a time.
2. On the Pi:
   ```sh
   python3 /data/shared_drivers/diagnostics/fridge_probe.py
   ```
   Look for `-> LIKELY FRIDGE` (names like `WT-0001`, `A1-...`, `AK1-...`).
   No candidate? Note the devices with the strongest signal while standing
   next to the fridge.
3. Test it:
   ```sh
   python3 /data/shared_drivers/diagnostics/fridge_probe.py AA:BB:CC:DD:EE:FF
   ```
   * `OK - this fridge speaks the Alpicool protocol` plus temperatures: put
     the MAC in `[fridge]` in config.ini and enable it with
     `install.sh fridge`.
   * `characteristics ... not found`: Hyckes uses a different controller.
     Keep the printed *GATT characteristics* list: with it (and ideally a
     Bluetooth capture from the phone app), the protocol module can be
     extended.

**Sharing with the phone app:** by default the Pi connects for about 2 s
every 60 s (`poll_interval`) and then disconnects, so the phone app can still
connect in between. While the phone app is connected, the Pi can't read the
fridge; after `timeout` seconds the zones show as disconnected and recover
automatically.

---

## Part 8 – Updates, troubleshooting, removal

### Updating the drivers
On the Pi (from Termius too):
```sh
sh /data/shared_drivers/update.sh
```
It downloads the latest version from GitHub, copies it over the old files and
restarts the running services. `config.ini` is not overwritten (it isn't in
the repository).

### Venus OS firmware updates
Nothing to do: everything lives in `/data`, and `/data/rc.local` restores
the services at boot. Afterwards, run the diagnostics once to confirm.

### Troubleshooting

| Symptom | Check |
|---|---|
| Tank not shown | `svstat /service/<name>` should say `up`; read the log in `/var/log/<name>/current` |
| Service keeps restarting | run it by hand to see the error: `svc -d /service/<n>` then `python3 /data/shared_drivers/<driver>.py --section <n> --debug` |
| Tank shows "disconnected" | the sensor isn't heard: run `--scan 30`, check the range (metal lockers block Bluetooth) |
| Water level wrong | redo `zero` and the calibration; check the sensor lies flat and the vent tube is open |
| Water level slow while driving | intended (anti-slosh), settles once parked |
| Mopeka level jumps | coupling gel, sensor position, `median_window = 9` |
| Fridge disconnected | phone app connected? Bluetooth range? `fridge_probe.py <MAC>` |
| Two devices with the same instance | *Settings → VRM online portal → VRM device instances* (Venus OS 3.x), or `device_instance` in config.ini |

Test without hardware: the `tests/` folder contains a simulation that runs
all drivers against a fake Bluetooth stack (see the README).

### Removing
```sh
sh /data/shared_drivers/uninstall.sh            # all services
sh /data/shared_drivers/uninstall.sh fridge     # just one
```
This stops the services and removes them from `/data/rc.local`. Delete
`/data/shared_drivers` and `/data/etc/init.d/<name>` to remove the files.
