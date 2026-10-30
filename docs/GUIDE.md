# Step-by-step installation guide

## Quick start (most people stop here)

1. Open an SSH session to the Pi as `root` (Termius on an iPad works) and paste:

   ```sh
   wget -qO- https://raw.githubusercontent.com/mich-crypto/Venusosmods/master/get.sh | sh
   ```

2. It prints the address of the settings page and a password. Open the address on your phone and press **Unlock**.
3. The **setup guide** opens by itself the first time (later: the *Setup guide* button at the top). Pick what you want to add:
   - **Gas bottle:** press the sensor's button, the guide finds it, press *Use this*, pick the bottle.
   - **Fridge:** switch the fridge on, the guide finds it, press *Use this*.
   - **Water tank:** the ESP32 sender is found the same way (build it with parts 2–4 below).
   - **Starlink** and **Battery (JK BMS)** are one step each.
4. Calibrate in the popup on the Gas or Water tab (*Calibrate*): press "empty" or "full" when the tank is, or move a slider
   to what the Mopeka app shows. No numbers to type.
5. *Where do I see it in Venus OS?* in the guide shows where each device appears on the Remote Console.

Run the same command again to update; your settings are kept. Everything below is the manual way and the reference
for building the water sensor.

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
| 8 | Settings page in the browser | 5 min |
| 9 | Updates, troubleshooting, removal | – |

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
6. To make the level match the Mopeka app at this moment, open the settings
   page → *Gas*, type the percentage the app shows next to *Right now the
   Mopeka app says* and press *Match it and save*. The page works out
   "Reading when full" from the current sensor reading and your bottle curve
   and saves it. Redo it right after a refill for the best accuracy.
   `svc -t /service/mopeka_standard` restarts the driver after changes.
7. **Sensor battery:** the *Gas* tab shows the sensor's battery charge and
   voltage, a graph of the voltage (recorded every 10 minutes) and a "low" mark
   when it falls to 20 %. With Telegram alerts switched on you get a message
   once (and again after 12 hours if it stays low). Change the limit with
   *Gas sensor battery alert at* in the Telegram settings.
8. The sensor's temperature is also published as its own temperature sensor,
   `com.victronenergy.temperature.mopeka_standard_temp` ("Gas bottle
   temperature", type outdoor), so it shows up with the other environment
   sensors in the *Device list* and on VRM. Turn it off with
   `temperature_service = no`, or change the type with `temperature_type`
   (battery, fridge, generic, room, outdoor, waterheater, freezer).

---

## Part 7 – Fridge (Hyckes and other Bluetooth compressor fridges)

Many 12 V compressor fridges use the same Bluetooth controller, the one the
"Alpicool / CAR FRIDGE FREEZER" app talks to. The driver reads the
temperatures of both zones, the set points, the supply voltage and the
eco/power state. It does **not** change any fridge settings.

Hyckes fridges use this controller (confirmed with a dual-zone Hyckes).
For another brand, or to check yours, test it first:

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

**Where the fridge shows up in the Remote Console**
* **Temperatures:** each zone is a temperature sensor ("Fridge", "Freezer").
  They appear with the other environment sensors (on the *Levels* page's
  environment view in the newer GUI) and in the *Device list*.
* **Controls:** the *switch pane* (the switches icon in the top bar) shows a
  "Fridge" card with *Fridge power*, *Fridge eco mode* and a set point
  control per zone. A change is sent right away: the Pi connects, sends the
  command, reads the status back, and the control shows what the fridge
  actually accepted. Set points stay within the fixed range of the
  manual (0…8 °C for the fridge, −20…−12 °C for the freezer).
* Set point ranges follow the manual of the dual-zone fridge: fridge 0…8 °C, freezer
  −20…−12 °C (a single-compartment model: −20…20 °C). The ranges and the names
  (Fridge, Freezer) are fixed in the driver and not settings.
* The controls are their own device, `com.victronenergy.switch.fridge_controls`
  (the pane only lists switch devices). It appears after the first successful
  read of the fridge, so give it a minute. It needs a Venus OS version that
  has the switch pane (v3.5x or newer with the new GUI). If the icon is
  missing, check the *Device list* for "Compressor fridge controls".
* **Controls on the settings page:** the *Fridge* tab has the same controls
  (power, eco mode, a slider with − and + per zone, limited to the range the
  fridge allows). It shows "The fridge confirmed." when the fridge has taken the
  change. They use the same path as the switch pane, so they work only when the
  controls device exists and the phone app is not connected to the fridge.
* Don't want controls in Venus OS? Set `control = no` in the `[fridge]`
  section.

---

## Part 8 – Settings page in the browser

Instead of editing `config.ini` in nano, you get a web page on the Pi. It
shows the water tank, gas bottle and fridge live, and it changes the settings
of the fridge, gas, water and the battery driver
([dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery),
for example the JK BMS) with proper fields, units and explanations. It is
made for a phone or iPad.

**Turn it on** (Termius is fine):
```sh
sh /data/shared_drivers/update.sh
sh /data/shared_drivers/install.sh web_ui
```
The installer prints a random **password** and the address, for example
`http://192.168.1.50:8088`. The password is also in `config.ini` under `[web]`
if you forget it. Open the address in Safari and add it to the home screen.

**What is on the page**
* **Overview** – live values and the state of every driver (running, restarts,
  last log line). Anyone on your network can look; nobody can change anything
  without the password.
* **Fridge, Gas, Water** – one form per sensor. Bottle presets fill in volume
  and shape, *Use as empty* / *Use as full* copy the current sensor reading, a
  blank field means "use the default". Press *Save*, or *Save & restart driver*
  to apply it at once.
* **Battery** – three views for dbus-serialbattery (for example a JK BMS):
  * *Live & cells*: charge ring, voltage, current, power, temperatures, what the
    BMS allows, and a plain-language "What I notice" list (cells far apart,
    one cell always lowest, temperatures, current limits, alarms). Bar chart of
    every cell, graphs of current, charge, voltage, cell spread, every cell
    over time, each cell's difference from the average, temperatures and the
    current the BMS allows. Choose 15 min, 1 hour or 6 hours; touch or hover a
    graph for the exact values, tap a cell in the legend to hide it. The Pi
    records a point every 10 s while the settings page runs and keeps 6 hours
    in memory. Cell voltages need `BATTERY_CELL_DATA_FORMAT = 1` (the default).
  * *Limits & curves*: how the BMS is connected, the cell voltage limits as a
    picture (with the pack voltage for your cell count and where your cells
    are now), the main numbers, one graph per current limit curve (charge and
    discharge against cell voltage, temperature, MOSFET temperature and
    charge level, with a marker where you are now) and a table of everything
    you changed from the defaults.
  * *All settings*: every setting in groups, with search, switches, the default
    value, a *Reset to default* link and the original explanation under *About
    this setting*. Only the settings you change are written to its
    `config.ini`; the file with the defaults is never touched. Changes to
    Bluetooth BMS, the USB Bluetooth option or the CAN port need a reboot of
    the Pi (the page offers a *Reboot now* button).
* **Starlink** – modelled on the open-source Stardashy dashboard, with these views:
  * *Dashboard*: the last speed test (download, upload, latency and jitter, with
    the change since the previous test, bufferbloat grade and the Cloudflare
    server it used), your public IP, ping tiles with a history strip for
    Cloudflare, Google, Quad9 and the dish itself (add the Starlink gateway
    `100.64.0.1` to tell a dish problem from an internet problem), charts of the
    tests, and the data used in the billing cycle with an optional cap bar.
  * *Dish*: live downlink, uplink and PoP latency, alerts, obstruction, GPS,
    tilt, firmware, graphs and a statistics grid. A "throttled" badge shows when
    the dish reports that your speed is limited.
  * *Sky*: where the dish points (azimuth, elevation, tilt). Tracking which
    satellite serves you needs orbit data and a large calculation and is not
    included.
  * *Energy*: the dish's power draw now, today, this month and this year in kWh,
    a projection for the month, the cost if you set a price, and a bar chart per
    day. Counted from the dish's own per-second readings.
  * *Tests*: every speed test, failed ones included (never counted as 0 Mbps).
  * *Log*: availability, an outage timeline (red = the dish reported an outage
    and why, grey = the Pi could not reach the dish), the incident table and the
    history of your public IP address.
  * *Debug*: ping, traceroute, DNS lookup and an HTTP check run on the Pi.
  * *Settings*: **software update** (compares with GitHub and runs
    `update.sh`), the Starlink settings (speed test interval, ping targets,
    billing day, data cap, electricity price). The Telegram messages are on
    the **Notifications** tab.
  **In Venus OS:** the dish also appears as a device called *Starlink
  connection* (Device list, Levels page and VRM). There is no Starlink device
  type, so it is shown as a tank-type device whose *level* is the connection
  quality from 0 to 100 %: 85 and up excellent, 65 good, 40 fair, below that
  poor, 0 offline. The score starts at 100 and loses points for packet loss,
  blocked sky, high ping, a speed limit, a weak signal and dish alerts (an
  outage is 0). Speed, ping, loss and obstruction are available on D-Bus under
  `/Starlink/...`. Switch it off with *Show the dish in Venus OS* in the
  Starlink settings. It disappears when the dish has been unreachable for a minute.
  The Pi must reach the dish at `192.168.100.1` (check:
  `curl http://192.168.100.1/`). Speed tests are off until you press the button
  or set an interval: each moves about 33 MB, so 30 minutes is about 1.6 GB a
  day. Energy, data use, outages and test results are kept in
  `/data/shared_drivers/state/starlink.json`. Starlink does not publish the
  interface and this was not tested against a real dish by the author: if a
  view shows an error run
  `python3 /data/shared_drivers/diagnostics/starlink_probe.py` and send the
  output.
* **Notifications** – Telegram messages for the camper. Make a bot with
  @BotFather, paste its token, open the link the page shows to read your chat
  ID, save, and press *Send a test message*. You can switch each one on or off:
  lithium battery low (a warning and an alarm, 20 % and 10 % by default), any
  alarm or warning a Victron device shows (and when it clears), the gas sensor
  battery, the **parking spot** (park, press *Use the current position*, and you
  get a message with a map link when the camper is more than 200 m away, and when
  it is back), the **Orion XS** being switched on to charge, and the Starlink
  messages (dish offline, packet loss, hardware alerts, new public IP, failed
  speed test). The page shows what is live now and the last messages sent.
* **Debug** – two views for troubleshooting. **Raw data**: what the sensors really send. *Gas* shows
  every Mopeka advertisement with its raw bytes (`data=`), the decoded
  distance, temperature, battery and echo quality, and the level that was
  published. *Fridge* shows what the Pi sends (`tx`) and receives (`rx`), the
  decoded status, and the last status payload byte by byte with the meaning of
  each byte (set points, ranges, temperatures, voltage). *Water* shows the
  ESP32 advertisements. The log lives in `/tmp/venusosmods-raw/` on the Pi, so
  it starts empty after a reboot and never wears the SD card. Use *Copy* to
  paste it into a message.
  **Files & logs**: the raw config files as a fallback, and the log of every
  service.

Each save keeps the previous file as `config.ini.bak` next to it.

**Safety**
* The page listens on the local network only: the installer opens port 8088
  in the Pi's firewall for 192.168.x.x, 10.x.x.x, 172.16–31.x.x and Tailscale
  addresses, not for the internet. Don't forward the port on your router.
* From outside the van, use Tailscale or an SSH tunnel
  (`ssh -L 8088:localhost:8088 root@<pi>`, then open `http://localhost:8088`).
* Change the port, switch the firewall rule off (`open_firewall = no`) or
  bind to the Pi itself (`bind = 127.0.0.1`) in the `[web]` section.
* Page not reachable? `tail -F /var/log/web_ui/current | tai64nlocal` and
  `python3 /data/shared_drivers/diagnostics/test_venus_compatibility.py`.

---

## Part 9 – Updates, troubleshooting, removal

### Updating the drivers
Your settings are not touched by an update: `config.ini` is not in the
download, and `update.sh` also keeps a copy as `config.ini.pre-update`. Saves
from the settings page keep the previous version as `config.ini.bak`.
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
