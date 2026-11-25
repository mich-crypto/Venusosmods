# Truma Combi heater (CP plus) – in preparation

Goal: see and control a Truma Combi heater (gas or diesel, Combi 4/6, D/E variants)
with the **CP plus** panel from Venus OS, VRM and the Venus OS mods page, through the
**water level sensor's ESP32**. Status:

| Part | State |
|---|---|
| Wiring plan (below) | ready |
| Sensor firmware: heater state, `truma` / `truma set …` commands, Heater card on its page | ready (`USE_TRUMA 0` = off by default) |
| Sensor firmware: LIN bus (talking to the CP plus) | **to do**: needs the transceiver wired and tests at the heater |
| Pi driver: reads the heater at every check-in, Venus OS services, commands, frost guard | ready (setting *Truma heater* = yes) |
| Mods page: Heater tab (status, control, automations) | ready |
| Simulation (`truma sim on`, codes `truma sim err W42`) | ready: try Venus OS and both pages without the heater |
| Heating up before arrival (Telegram / geofence) | planned, after the LIN part |

## How it works

```
Truma Combi ──LIN── CP plus panel ──LIN── TJA1020 ── ESP32 (water level sensor) ──Bluetooth── Pi (Venus OS)
                    (bus master)                      answers like Truma's iNet box
```

The CP plus is the master of a LIN bus (12 V, 9600 baud, one wire). Truma's own
*iNet box* is a slave on that bus; the CP plus shows it as an "iNet" device and then
accepts settings from it. The ESP32 will do the same. Community projects have
documented the protocol by listening to a real iNet box (for example the
`inetbox` projects for Python/ESPHome); the firmware part will follow them.

The heater itself is never touched directly: the CP plus stays the master, keeps its
own safety logic, and the panel shows every change made from Venus OS.

## Hardware

* **LIN transceiver**: a TJA1020 or TJA1021 module (a few euros, "LIN transceiver
  breakout"). It turns the 12 V single-wire LIN bus into 3.3 V serial for the ESP32.
  * `RXD` → ESP32 **GPIO17** (`PIN_LIN_RX`)
  * `TXD` → ESP32 **GPIO18** (`PIN_LIN_TX`)
  * `SLP`/`EN` → 3V3 (or a GPIO, set `PIN_LIN_SLP`)
  * `LIN` → the bus wire, `VBAT` → 12 V, `GND` → GND (common with the ESP32)
  * As a slave the module needs **no** 1 kΩ master pull-up; leave it out.
* **Connection to the bus**: the CP plus has free RJ12 (6P6C) sockets marked for the
  iNet box / diagnosis. A short RJ12 cable or splitter gives LIN, 12 V and GND without
  cutting anything. Check the pin-out of your panel with a meter first (12 V on one
  pin, LIN idles near 12 V too).
* The ESP32 stays powered as now (5 V); only LIN and GND come from the heater side.
* **Cable length**: the water level sensor sits at the tank; LIN tolerates long wires
  (up to 40 m in cars), so a run from the CP plus to the wheel arch is fine.

## Commands (sensor)

| Command | Answer |
|---|---|
| `truma` | `truma {"en":0/1,"lin":"…","ago":s,"room":°C,"set":°C,"heat":0/1,"water":"off/eco/hot/boost","wt":°C,"err":code,"pend":0/1}` |
| `truma set heat=on room=21 water=eco` | any of the three; `ok truma …`. Kept until the LIN part sends it. |

`lin` says how far it is: `off` (module not built in), `no LIN data yet …`, `LIN silent`, `ok`.

## Venus OS

With *Water → Settings → Truma heater = yes* the water driver also asks the sensor
for `truma` at every check-in and publishes:

* **com.victronenergy.temperature** "Truma heater room": the room temperature measured
  by the heater (only when it really comes from the heater).
* **com.victronenergy.switch** "Truma heater" on the switch pane: *Heating* (on/off),
  *Room set point* (5–30 °C), *Hot water* (0 off, 1 eco, 2 hot, 3 boost).
* A change there, or on the mods page's **Heater** tab, goes to the sensor at once as
  one `truma set …` command over Bluetooth.

**Frost guard** (Heater tab → Automations): when the heater's room temperature is
below the frost limit (default 5 °C) and the heating is off, the driver switches it
on at 2 °C above the limit, once per cold spell.

## What's left (the LIN part)

In `trumaPoll()` in the sketch, with `USE_TRUMA 1`:

1. `Serial1` at 9600 baud on GPIO17/18; detect the LIN break and sync byte, read the
   protected ID.
2. Answer the frame IDs the CP plus polls for the iNet box; read the status frames
   (room and water temperature, set points, heating state, error code) into `truma.*`
   and set `truma.lastFrame`.
3. While `truma.wantPending`: send the wanted settings with the transport layer
   (multi-frame, like the iNet box), then clear it.
4. Test at the heater step by step: first only listen and log the frames (`dlog`),
   then answer, then send one setting.

## Codes

The CP plus shows codes like **W 42 H** (W = warning: the heater keeps running and the
warning goes away by itself) or **E 517 H** (E = error: remove the cause, then
acknowledge on the CP plus). The full list for the **gas Combi** (4, 6, 4 E, 6 E) from the
troubleshooting guide of the Truma CP plus manual is built in (`BUILTIN_CODES` /
`BUILTIN_HELP` in `truma_heater.py`, `TRUMA_CODES` on the settings page, `TCODES` on the
sensor page):

* Venus OS gets the short meaning as the warning/alarm name.
* The Venus OS mods page and the sensor's page show the meaning, *What to do*, and for
  E codes how to acknowledge: press the knob (from standby twice); don't switch the
  CP plus off and on (15-minute lockout); no more than 4 resets in 15 minutes (E 607 H);
  a flashing code means a lockout is running.
* Codes outside the list are shown with their class and "contact Truma Service";
  *Code meanings* on the Heater tab adds or changes meanings.
