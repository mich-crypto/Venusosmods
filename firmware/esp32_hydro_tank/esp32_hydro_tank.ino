/*
 * ESP32-S3 hydrostatic water tank sender for Victron Venus OS
 *
 * Reads a hydrostatic pressure transmitter, filters out sloshing, converts
 * height to litres with a lookup table and broadcasts the result as a
 * non-connectable BLE advertisement. The Venus OS side is
 * data/shared_drivers/esp32_hydro_tank.py.
 *
 * Board:  ESP32S3 Dev Module (Arduino-ESP32 core 2.0.x or 3.x; works with both
 *         the Bluedroid and the NimBLE stack of the built-in BLE library)
 * Libs:   OneWire, DallasTemperature  (only if USE_DS18B20 = 1)
 *
 * Temperatures: up to MAX_TEMPS DS18B20 probes share the one-wire pin. They
 * are numbered by their address (so the order is stable) and each gets a
 * name, set on the web page or with "name <1|2> <text>".
 *
 * Filter chain, every SAMPLE_PERIOD_MS:
 *   burst of BURST_SAMPLES ADC reads -> median    (kills ADC noise and spikes)
 *   median -> exponential moving average          (kills waves)
 *   EMA alpha switches to a much slower value while the burst medians jump
 *   around (driving), so the level stays stable on the road and still
 *   reacts within seconds when parked.
 *
 * Calibration (non-square tanks): open the Serial Monitor at 115200 baud and
 * type "help". The height -> litres table is measured by filling the tank in
 * steps ("cal start", "cal add 10", ..., "cal save") and stored in flash, so
 * no re-flashing is needed. See docs/calibration.md.
 *
 * Own Wi-Fi (works without Venus OS): the sender opens a Wi-Fi network
 * "Watertank-<DEVICE_ID>" (password below, changeable on the page). Join it
 * with a phone and open http://192.168.4.1 for live values, calibration,
 * the command console and firmware updates. Needs the partition scheme
 * "Minimal SPIFFS (1.9MB APP with OTA)" (firmware too big for the default).
 */

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEAdvertising.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <Update.h>
#include <esp_arduino_version.h>
#include <Preferences.h>
#include <esp_coexist.h>

// ============================================================ CONFIGURATION

#define DEVICE_ID            1      // must match device_id in config.ini on the Pi

// ---- Pins (ESP32-S3: use GPIO1..GPIO10 = ADC1; ADC2 shares hardware with the radio)
#define PIN_PRESSURE         4
#define PIN_ONEWIRE          5      // temperature probe 1 (DS18B20 data)
#define PIN_ONEWIRE2         6      // temperature probe 2 on its own pin; -1 = both on PIN_ONEWIRE
#define USE_DS18B20          1      // 1 = DS18B20 temperature probes fitted, 0 = none
#define MAX_TEMPS            2      // probes on the one-wire bus (sent to the Pi: the first two)

// ---- Input stage. Pick ONE:
//  INPUT_CURRENT_LOOP 1: 4-20 mA transmitter, shunt resistor from pin to GND.
//                        120 R gives 0.48 V .. 2.4 V, headroom for fault
//                        currents. Signal unit = mA (then use 4.0 / 20.0
//                        below, fault limits ~3.0 / 22.0).
//  INPUT_CURRENT_LOOP 0: voltage output transmitter, optionally through a
//                        divider R1 (top) / R2 (bottom).
//                        DIVIDER_RATIO = (R1 + R2) / R2, 1.0 = no divider.
//                        Signal unit = V. Keep the pin below ~3.1 V.
//
// Presets (docs/wiring.md):
//  A  0.5-4.5 V:  LOOP 0, DIVIDER 1.5,  ZERO 0.5, FULL 4.5,  FAULT 0.2 / 4.9
//  B  4-20 mA:    LOOP 1, SHUNT 120,    ZERO 4.0, FULL 20.0, FAULT 3.0 / 22.0
//  C  0-3.3 V:    LOOP 0, DIVIDER 1.0,  ZERO 0.0, FULL 3.3,  FAULT -1 (off) / 3.05
// Defaults below: preset C for the QDY30A 0-3.3 V, 2 m range.
#define INPUT_CURRENT_LOOP   0
#define SHUNT_OHMS           120.0f
#define DIVIDER_RATIO        1.0f

// ---- Transmitter datasheet values (V or mA, see above)
#define SIGNAL_AT_ZERO       0.0f   // output at 0 pressure
#define SIGNAL_AT_FULL       3.3f   // output at full scale
#define SENSOR_RANGE_MM      2000.0f// full scale in mm of water (2 m H2O; 1 m H2O = 9.81 kPa)
#define SIGNAL_FAULT_LOW     -1.0f  // below this: open wire / dead sensor (-1 = can't tell, see C)
#define SIGNAL_FAULT_HIGH    3.05f  // above this: short / overpressure (ADC tops out ~3.1 V)

// ---- Default zero offset (mm added to the measured height). Normally you
//      set it with the "zero" serial command on an empty tank instead.
#define ZERO_OFFSET_MM       0.0f

// ---- Default tank shape: water height (mm) -> volume (litres), ascending.
//      Used until you store a measured table with "cal save" (or paste the
//      output of the "table" command here). The last entry is 100 %.
struct TankPoint { float mm; float litres; };
#define BUILTIN_TANK_NAME    "Wheel-arch tank 100 L"   // name of DEFAULT_TABLE on the page
#define MAX_TANKS            4      // measured tanks kept in flash besides the built-in one
// Wheel-arch tank from docs/tank/tank-drawing.jpg, computed with
// tools/tank_wheelarch.py and scaled to its nominal 100 L. Heights from the
// inner floor of the leg, where the sensor lies.
static const TankPoint DEFAULT_TABLE[] = {
  {    0.0f,    0.0f },
  {   20.0f,    0.8f },
  {   40.0f,    1.6f },
  {   60.0f,    2.4f },
  {   80.0f,    3.3f },
  {  100.0f,    4.1f },
  {  120.0f,    5.0f },
  {  140.0f,    5.9f },
  {  160.0f,    6.9f },
  {  180.0f,    7.8f },
  {  200.0f,    8.9f },
  {  220.0f,    9.9f },
  {  240.0f,   11.1f },
  {  260.0f,   12.3f },
  {  280.0f,   13.6f },
  {  300.0f,   14.9f },
  {  320.0f,   16.4f },
  {  340.0f,   18.0f },
  {  350.0f,   18.9f },
  {  360.0f,   19.9f },
  {  380.0f,   22.5f },
  {  400.0f,   26.1f },
  {  420.0f,   30.5f },
  {  440.0f,   35.2f },
  {  460.0f,   39.9f },
  {  465.0f,   41.1f },
  {  480.0f,   45.1f },
  {  500.0f,   50.4f },
  {  520.0f,   55.8f },
  {  540.0f,   61.2f },
  {  560.0f,   66.5f },
  {  580.0f,   71.9f },
  {  600.0f,   77.2f },
  {  620.0f,   82.6f },
  {  640.0f,   87.9f },
  {  660.0f,   93.3f },
  {  680.0f,   98.7f },
  {  685.0f,  100.0f },
};

// ---- Filter tuning
#define BURST_SAMPLES        31     // odd number -> true median
#define SAMPLE_PERIOD_MS     250    // one burst every 250 ms (4 Hz)
#define EMA_ALPHA_CALM       0.15f  // ~1.6 s time constant when parked
#define EMA_ALPHA_SLOSH      0.01f  // ~25 s time constant while driving
#define SLOSH_ENTER_MM       6.0f   // mean |median - EMA| above this -> sloshing
#define SLOSH_EXIT_MM        3.0f   // ... and back below this -> calm (hysteresis)
#define ACTIVITY_ALPHA       0.05f

// ---- Filling / draining detection
#define TREND_PERIOD_MS      5000   // trend sample every 5 s
#define TREND_SAMPLES        12     // compare against 60 s ago
#define FILL_RATE_MM_MIN     5.0f   // > +5 mm/min -> filling
#define DRAIN_RATE_MM_MIN    5.0f   // < -5 mm/min -> draining

// ---- BLE
#define ADV_UPDATE_MS        2000   // new payload every 2 s
#define ADV_INTERVAL_MS      500    // radio interval (power vs. latency)
#define ADV_NAME             "VHT"

#define SERIAL_LOG           1      // status line every ADV_UPDATE_MS ("log" toggles it)

// ---- Own Wi-Fi network with a web page (http://192.168.4.1)
#define USE_WIFI_AP          1
#define AP_SSID_PREFIX       "Watertank-"   // + DEVICE_ID -> "Watertank-1"
#define AP_PASSWORD_DEFAULT  "watertank"    // 8..63 chars; change it on the page
#define AP_CHANNEL           6
#define MDNS_NAME            "watertank"    // also reachable as http://watertank.local

// ---- Wi-Fi modes (set from the Venus OS page over Bluetooth, or "wifi mode ..."):
//   auto  Bluetooth only (saves energy). The own network starts by itself when
//         the Pi has not checked in over Bluetooth for PI_SILENT_MS.
//   van   also joins the van Wi-Fi (wifi ssid / wifi pass); the own network only
//         when neither the van Wi-Fi nor the Pi is there.
//   ap    own network always on (uses the most energy).
//   off   own network only after pressing BOOT (no start-up window, no fallback).
// The own network is also on for BOOT_AP_MS after power-on and BUTTON_AP_MS
// after pressing the BOOT button, and never switches off while a phone is on it.
#define WIFI_MODE_DEFAULT    "auto"
#define PI_SILENT_MS         (15UL * 60UL * 1000UL)
#define BOOT_AP_MS           (10UL * 60UL * 1000UL)
#define BUTTON_AP_MS         (30UL * 60UL * 1000UL)
#define AP_MIN_ON_MS         (2UL * 60UL * 1000UL)  // no flapping
#define PIN_BOOT_BUTTON      0

// ---- Energy: 80 MHz is plenty for this job and saves ~10-15 mA against 240 MHz
#define CPU_MHZ              80

// ---- Status LED: the board's RGB LED (WS2812) gives one short dim flash every
//      2 s: blue = Bluetooth sends, green = Wi-Fi on (page reachable), both =
//      blue and green take turns. Bright green/red = firmware update. DevKitC-1 v1.0 and most clones: GPIO48, v1.1: GPIO38.
#define STATUS_LED_PIN       48     // -1 = no LED
#define STATUS_LED_MAX       40     // brightness during a firmware update (0..255, these LEDs are bright)
#define STATUS_LED_DIM       6      // brightness of the normal status flash (dim, saves energy)
#define BLINK_PERIOD_MS      2000   // one short flash this often
#define BLINK_ON_MS          120    // ... this long: the LED is dark 94 % of the time
#define OTA_BLINK_MS         250    // firmware arriving from the Pi: green and red alternate this fast

#define FW_VERSION           "4.9"

// ---- DS18B20 sanity
#define TEMP_MAX_FAILS       3      // consecutive bad reads before the value is dropped

// ======================================================== END CONFIGURATION

#if USE_WIFI_AP
#include <WiFi.h>
#include <WebServer.h>
#include <Update.h>
#include <DNSServer.h>
#include <HTTPUpdate.h>
#include <ESPmDNS.h>
#endif

#if USE_DS18B20
#include <OneWire.h>
#include <DallasTemperature.h>
static OneWire oneWire(PIN_ONEWIRE);
static DallasTemperature ds18b20(&oneWire);
#if PIN_ONEWIRE2 >= 0
static OneWire oneWire2(PIN_ONEWIRE2);
static DallasTemperature ds18b20b(&oneWire2);
static DallasTemperature *buses[] = { &ds18b20, &ds18b20b };
static const int busPins[] = { PIN_ONEWIRE, PIN_ONEWIRE2 };
#else
static DallasTemperature *buses[] = { &ds18b20 };
static const int busPins[] = { PIN_ONEWIRE };
#endif
static const uint8_t BUSES = sizeof(busPins) / sizeof(busPins[0]);
#endif

// Protocol (keep in sync with esp32_hydro_protocol.py)
static const uint16_t COMPANY_ID = 0xFFFF;
static const uint8_t PROTO_VERSION = 1;
enum : uint8_t {
  FLAG_TEMP_VALID = 0x01,
  FLAG_FILLING    = 0x02,
  FLAG_DRAINING   = 0x04,
  FLAG_FAULT      = 0x08,
  FLAG_SLOSHING   = 0x10,
  FLAG_TEMP2_VALID = 0x20,
};
static const size_t PAYLOAD_LEN = 18;   // 16 + second temperature (older Pi versions ignore it)

// Active calibration (sketch defaults or loaded from flash)
#define MAX_POINTS 40
static TankPoint table[MAX_POINTS];
static size_t tablePoints = 0;
static float zeroOffsetMm = ZERO_OFFSET_MM;
static Preferences prefs;
static bool serialLog = SERIAL_LOG;

// Test mode: no smoothing, so the level follows the signal at once (bench tests
// with a potentiometer). Switches itself off after FAST_MODE_MS and at restart.
#define FAST_MODE_MS (15UL * 60UL * 1000UL)
static uint32_t fastUntil = 0;
static bool fastMode() { return fastUntil && (int32_t)(fastUntil - millis()) > 0; }

// Simulation: broadcast a chosen water height instead of the sensor, to see how
// long Venus OS takes to show a change ("sim 50%", "sim 300", "sim off").
// Switches itself off after SIM_MS and at a restart.
#define SIM_MS (15UL * 60UL * 1000UL)
static float simHeight = NAN;          // mm; NAN = off
static uint32_t simSince = 0, simUntil = 0;
static bool advNow = false;            // send the next broadcast right away
static bool simActive() {
  if (!isnan(simHeight) && (int32_t)(simUntil - millis()) <= 0) simHeight = NAN;
  return !isnan(simHeight);
}

// Debug log of the link (Bluetooth, Wi-Fi, updates), newest DLOG_N lines in RAM: "dlog" prints it,
// the page shows it, the Venus OS page fetches it over Bluetooth. Times are the sensor's uptime.
#define DLOG_N 120
#define DLOG_W 110
static char dlogLines[DLOG_N][DLOG_W];
static uint16_t dlogHead = 0, dlogCount = 0;
static uint32_t dlogTotal = 0, dlogMark = 0;   // lines ever written / handed to the Pi ("dlog new")
static portMUX_TYPE dlogMux = portMUX_INITIALIZER_UNLOCKED;

static void dlog(const char *fmt, ...) {
  char buf[DLOG_W];
  uint32_t t = millis() / 1000;
  int n = snprintf(buf, sizeof(buf), "%02lu:%02lu:%02lu ", (unsigned long)(t / 3600), (unsigned long)(t / 60 % 60), (unsigned long)(t % 60));
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf + n, sizeof(buf) - n, fmt, ap);
  va_end(ap);
  portENTER_CRITICAL(&dlogMux);
  memcpy(dlogLines[(dlogHead + dlogCount) % DLOG_N], buf, sizeof(buf));
  if (dlogCount < DLOG_N) dlogCount++; else dlogHead = (dlogHead + 1) % DLOG_N;
  dlogTotal++;
  portEXIT_CRITICAL(&dlogMux);
}

static String dlogText() {
  uint32_t t = millis() / 1000;
  String o = "sensor log, firmware " FW_VERSION ", uptime now " + String(t / 3600) + "h" + String(t / 60 % 60) + "m" + String(t % 60) + "s\n";
  char line[DLOG_W];
  for (uint16_t i = 0; ; i++) {
    portENTER_CRITICAL(&dlogMux);
    bool more = i < dlogCount;
    if (more) memcpy(line, dlogLines[(dlogHead + i) % DLOG_N], sizeof(line));
    portEXIT_CRITICAL(&dlogMux);
    if (!more) break;
    line[DLOG_W - 1] = 0;
    o += line; o += '\n';
  }
  return o;
}

// The lines the Pi has not fetched yet (it stores them with its own log, for long-term tests)
static String dlogNew() {
  String o = "dlog new";
  char line[DLOG_W];
  portENTER_CRITICAL(&dlogMux);
  uint32_t total = dlogTotal, first = dlogTotal - dlogCount;
  uint32_t from = dlogMark > first ? dlogMark : first;
  if (total - from > 60) from = total - 60;      // keep one answer small
  uint32_t skipped = from > dlogMark ? from - dlogMark : 0;
  dlogMark = total;
  portEXIT_CRITICAL(&dlogMux);
  if (skipped) o += "\n(" + String(skipped) + " older lines not fetched)";
  for (uint32_t i = from; i < total; i++) {
    portENTER_CRITICAL(&dlogMux);
    bool ok = i >= dlogTotal - dlogCount;
    if (ok) memcpy(line, dlogLines[(dlogHead + (i - (dlogTotal - dlogCount))) % DLOG_N], sizeof(line));
    portEXIT_CRITICAL(&dlogMux);
    if (!ok) continue;
    line[DLOG_W - 1] = 0;
    o += '\n'; o += line;
  }
  return o;
}

// a command for the log, without passwords
static String dlogCmd(const String &c) {
  if (c.startsWith("wifi pass")) return "wifi pass ***";
  int tab = c.indexOf('\t');
  String x = tab < 0 ? c : c.substring(0, tab) + " (+password)";
  return x.length() > 70 ? x.substring(0, 70) + "..." : x;
}

// Where command replies go: the serial port, or a web request's reply text
static Print *out = &Serial;
class StringOut : public Print {
 public:
  String text;
  size_t write(uint8_t c) override { text += (char)c; return 1; }
};

static float capacityL() { return table[tablePoints - 1].litres; }

// State
static float emaHeight = NAN;
static float activity = 0.0f;
static bool sloshing = false;
static bool fault = false;
static float lastSignal = 0.0f;
static float lastRawHeight = NAN;   // unfiltered height of the last burst
static float trend[TREND_SAMPLES];
static uint8_t trendCount = 0, trendIdx = 0;
static bool filling = false, draining = false;
// Temperature probes, sorted by address. temperatureC = probe 1 (old name kept).
static float tempC[MAX_TEMPS];
static uint8_t tempAddr[MAX_TEMPS][8];
static uint8_t tempFails[MAX_TEMPS];
static String tempName[MAX_TEMPS];
static uint8_t tempCount = 0;          // probes found
static bool tempPresent[MAX_TEMPS];    // slot has a probe
static uint8_t tempBus[MAX_TEMPS];     // which pin it is on
#define temperatureC (tempC[0])
static uint8_t sequence = 0;
static BLEAdvertising *advertising = nullptr;

// ------------------------------------------------------------------ helpers

static float medianOf(float *v, size_t n) {
  // insertion sort - n is small
  for (size_t i = 1; i < n; i++) {
    float x = v[i];
    size_t j = i;
    while (j > 0 && v[j - 1] > x) { v[j] = v[j - 1]; j--; }
    v[j] = x;
  }
  return v[n / 2];
}

// litres -> height, the table read the other way round (for "sim <percent>%")
static float litresToHeight(float l) {
  if (l <= table[0].litres) return table[0].mm;
  for (size_t i = 1; i < tablePoints; i++) {
    if (l <= table[i].litres) {
      const TankPoint &a = table[i - 1], &b = table[i];
      if (b.litres <= a.litres) return b.mm;
      return a.mm + (b.mm - a.mm) * (l - a.litres) / (b.litres - a.litres);
    }
  }
  return table[tablePoints - 1].mm;
}

static float heightToLitres(float mm) {
  if (mm <= table[0].mm) return table[0].litres;
  for (size_t i = 1; i < tablePoints; i++) {
    if (mm <= table[i].mm) {
      const TankPoint &a = table[i - 1], &b = table[i];
      return a.litres + (b.litres - a.litres) * (mm - a.mm) / (b.mm - a.mm);
    }
  }
  return capacityL();
}

// Pin millivolts -> transmitter signal (V or mA)
static float mvToSignal(float mv) {
#if INPUT_CURRENT_LOOP
  return mv / SHUNT_OHMS;              // mV / ohm = mA
#else
  return mv / 1000.0f * DIVIDER_RATIO; // V at the transmitter output
#endif
}

static float signalToHeight(float s) {
  float h = (s - SIGNAL_AT_ZERO) / (SIGNAL_AT_FULL - SIGNAL_AT_ZERO) * SENSOR_RANGE_MM;
  return h + zeroOffsetMm;
}

// ---------------------------------------------------------------- sampling

static float readBurstMedianMv() {
  static float buf[BURST_SAMPLES];
  for (size_t i = 0; i < BURST_SAMPLES; i++) {
    buf[i] = (float)analogReadMilliVolts(PIN_PRESSURE);  // factory-calibrated
    delayMicroseconds(300);
  }
  return medianOf(buf, BURST_SAMPLES);
}

static void sampleLevel() {
  if (simActive()) {                    // the simulated height goes out as it is, no filter
    emaHeight = lastRawHeight = simHeight;
    fault = sloshing = false;
    activity = 0.0f;
    return;
  }
  lastSignal = mvToSignal(readBurstMedianMv());
  fault = lastSignal < SIGNAL_FAULT_LOW || lastSignal > SIGNAL_FAULT_HIGH;
  if (fault) return;  // keep the last good EMA, flag the fault

  float h = signalToHeight(lastSignal);
  lastRawHeight = h;
  if (isnan(emaHeight)) {   // first sample after boot / fault
    emaHeight = h;
    return;
  }

  if (fastMode()) {               // test mode: show every burst as it is
    emaHeight = h;
    activity = 0.0f;
    sloshing = false;
    return;
  }

  // Slosh detector: how far do the burst medians wander around the average?
  activity += ACTIVITY_ALPHA * (fabsf(h - emaHeight) - activity);
  if (!sloshing && activity > SLOSH_ENTER_MM) sloshing = true;
  else if (sloshing && activity < SLOSH_EXIT_MM) sloshing = false;

  float alpha = sloshing ? EMA_ALPHA_SLOSH : EMA_ALPHA_CALM;
  emaHeight += alpha * (h - emaHeight);
}

static void updateTrend() {
  if (fault || isnan(emaHeight)) return;
  trend[trendIdx] = emaHeight;
  trendIdx = (trendIdx + 1) % TREND_SAMPLES;
  if (trendCount < TREND_SAMPLES) trendCount++;
  if (trendCount < TREND_SAMPLES) { filling = draining = false; return; }

  // trendIdx now points at the oldest sample
  float minutes = (TREND_SAMPLES - 1) * TREND_PERIOD_MS / 60000.0f;
  float rate = (emaHeight - trend[trendIdx]) / minutes;
  filling = !sloshing && rate > FILL_RATE_MM_MIN;
  draining = !sloshing && rate < -DRAIN_RATE_MM_MIN;
}

static String addrHex(const uint8_t *a) {
  char b[24];
  snprintf(b, sizeof b, "%02X%02X%02X%02X%02X%02X%02X%02X", a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]);
  return String(b);
}

// NVS keys are at most 15 characters: "t" + the 7 bytes after the family code
static String nameKey(const uint8_t *a) { return "t" + addrHex(a).substring(2); }

static void loadTempNames() {
  prefs.begin("temps", true);
  for (uint8_t i = 0; i < MAX_TEMPS; i++)
    tempName[i] = tempPresent[i] ? prefs.getString(nameKey(tempAddr[i]).c_str(), "") : String();
  prefs.end();
  for (uint8_t i = 0; i < MAX_TEMPS; i++)
    if (tempName[i].length() == 0) tempName[i] = "Temperature " + String(i + 1);
}

static bool setTempName(uint8_t i, String name) {
  name.trim();
  if (i >= MAX_TEMPS || !tempPresent[i] || name.length() == 0 || name.length() > 32) return false;
  for (size_t k = 0; k < name.length(); k++)
    if ((uint8_t)name[k] < 32 || name[k] == '"' || name[k] == '\\') return false;
  prefs.begin("temps", false);
  prefs.putString(nameKey(tempAddr[i]).c_str(), name);
  prefs.end();
  tempName[i] = name;
  return true;
}

#if USE_DS18B20
// Addresses of the probes on one pin, sorted (stable order); returns how many
static uint8_t scanBus(uint8_t bus, uint8_t found[][8], uint8_t maxN) {
  // The chip's own pull-up (~45 kOhm) helps when the 4.7 kOhm resistor is missing
  // (short cables). It stays on: OneWire only switches the output driver on and
  // off. With the external resistor fitted it changes nothing.
  pinMode(busPins[bus], INPUT_PULLUP);
  DallasTemperature *d = buses[bus];
  d->begin();
  uint8_t n = 0, a[8];
  for (uint8_t i = 0; i < d->getDeviceCount() && n < maxN; i++)
    if (d->getAddress(a, i)) memcpy(found[n++], a, 8);
  for (uint8_t i = 1; i < n; i++)              // insertion sort by address
    for (uint8_t j = i; j > 0 && memcmp(found[j - 1], found[j], 8) > 0; j--) {
      uint8_t t[8]; memcpy(t, found[j], 8); memcpy(found[j], found[j - 1], 8); memcpy(found[j - 1], t, 8);
    }
  d->setResolution(11);                 // 0.125 C, 375 ms conversion
  d->setWaitForConversion(false);
  return n;
}

static void putProbe(uint8_t slot, const uint8_t *addr, uint8_t bus) {
  memcpy(tempAddr[slot], addr, 8);
  tempBus[slot] = bus;
  tempPresent[slot] = true;
}
#endif

// Find the probes. With a second pin: probe 1 = GPIO5, probe 2 = GPIO6 (or a
// second probe on GPIO5 if GPIO6 has none). With one pin: numbered by address.
static void findProbes() {
#if USE_DS18B20
  uint8_t a0[MAX_TEMPS][8], a1[MAX_TEMPS][8];
  uint8_t n0 = scanBus(0, a0, MAX_TEMPS), n1 = BUSES > 1 ? scanBus(1, a1, MAX_TEMPS) : 0;
  for (uint8_t i = 0; i < MAX_TEMPS; i++) { tempPresent[i] = false; tempC[i] = NAN; tempFails[i] = 0; }
  if (n0 > 0) putProbe(0, a0[0], 0);
  if (MAX_TEMPS > 1) {
    if (n1 > 0) putProbe(1, a1[0], 1);
    else if (n0 > 1) putProbe(1, a0[1], 0);
  }
  uint8_t n = 0;
  for (uint8_t i = 0; i < MAX_TEMPS; i++) n += tempPresent[i];
  if (n != tempCount) Serial.printf("Temperature probes found: %u (GPIO%d: %u, GPIO%d: %u)\n",
                                    n, busPins[0], n0, busPins[BUSES - 1], n1);
  tempCount = n;
  loadTempNames();
#endif
}

static void updateTemperature() {
#if USE_DS18B20
  static bool pending = false;
  static uint32_t lastSearch = 0;
  // A probe plugged in later, or one that went missing: look again every 30 s
  bool missing = tempCount < MAX_TEMPS;
  for (uint8_t i = 0; i < MAX_TEMPS; i++) missing |= tempPresent[i] && isnan(tempC[i]) && tempFails[i] >= TEMP_MAX_FAILS;
  if (missing && millis() - lastSearch > 30000) {
    lastSearch = millis();
    float keep[MAX_TEMPS]; memcpy(keep, tempC, sizeof keep);
    uint8_t before[MAX_TEMPS][8]; memcpy(before, tempAddr, sizeof before);
    bool had[MAX_TEMPS]; memcpy(had, tempPresent, sizeof had);
    findProbes();
    if (memcmp(had, tempPresent, sizeof had) == 0 && memcmp(before, tempAddr, sizeof before) == 0)
      memcpy(tempC, keep, sizeof keep);
    pending = false;
  }
  if (pending) {
    for (uint8_t i = 0; i < MAX_TEMPS; i++) {
      if (!tempPresent[i]) continue;
      // getTempC() checks the scratchpad CRC and returns DEVICE_DISCONNECTED_C
      // for a missing probe or a corrupted read.
      float t = buses[tempBus[i]]->getTempC(tempAddr[i]);
      bool bad = t == DEVICE_DISCONNECTED_C || t < -55.0f || t > 125.0f;
      // 85.0 C is the power-on value of the scratchpad: a conversion that never
      // ran (brown-out, bad contact). Only believe it if we were already close.
      if (t == 85.0f && (isnan(tempC[i]) || fabsf(tempC[i] - 85.0f) > 5.0f)) bad = true;
      if (!bad) {
        tempC[i] = t;
        tempFails[i] = 0;
      } else if (++tempFails[i] >= TEMP_MAX_FAILS) {
        tempC[i] = NAN;   // stop sending a stale value as if it were current
      }
    }
  }
  for (uint8_t b = 0; b < BUSES; b++) buses[b]->requestTemperatures();   // non-blocking, read on next call
  pending = true;
#endif
}

// --------------------------------------------------------------------- BLE

static void put16(uint8_t *p, uint16_t v) { p[0] = v & 0xFF; p[1] = v >> 8; }

static void buildPayload(uint8_t *p) {
  float height = isnan(emaHeight) ? 0.0f : max(0.0f, emaHeight);
  float litres = heightToLitres(height);
  float level = capacityL() > 0 ? litres / capacityL() * 100.0f : 0.0f;
  bool tempValid = !isnan(temperatureC);

  uint8_t flags = 0;
  if (tempValid) flags |= FLAG_TEMP_VALID;
  if (filling)   flags |= FLAG_FILLING;
  if (draining)  flags |= FLAG_DRAINING;
  if (fault || isnan(emaHeight)) flags |= FLAG_FAULT;
  if (sloshing)  flags |= FLAG_SLOSHING;
  bool temp2Valid = MAX_TEMPS > 1 && !isnan(tempC[MAX_TEMPS > 1 ? 1 : 0]);
  if (temp2Valid) flags |= FLAG_TEMP2_VALID;

  p[0] = 'V'; p[1] = 'H';
  p[2] = PROTO_VERSION;
  p[3] = DEVICE_ID;
  p[4] = flags;
  p[5] = sequence++;
  put16(p + 6,  (uint16_t)lroundf(constrain(level, 0.0f, 100.0f) * 100.0f));
  put16(p + 8,  (uint16_t)lroundf(litres * 10.0f));
  put16(p + 10, (uint16_t)lroundf(capacityL() * 10.0f));
  put16(p + 12, (uint16_t)lroundf(height));
  put16(p + 14, (uint16_t)(int16_t)(tempValid ? lroundf(temperatureC * 100.0f) : 0));
  put16(p + 16, (uint16_t)(int16_t)(temp2Valid ? lroundf(tempC[MAX_TEMPS > 1 ? 1 : 0] * 100.0f) : 0));

  if (serialLog) {
    Serial.printf("signal=%.3f height=%.1fmm litres=%.1f level=%.1f%% temp=%.1f temp2=%.1f "
                  "activity=%.1f ble=%s%s%s%s%s\n",
                  lastSignal, height, litres, level, temperatureC, temp2Valid ? tempC[1] : NAN, activity,
                  advertising && advertising->isAdvertising() ? "on" : "OFF",
                  sloshing ? " SLOSH" : "", filling ? " FILLING" : "",
                  draining ? " DRAINING" : "", (flags & FLAG_FAULT) ? " FAULT" : "");
  }
}

// AD structure types (Bluetooth Core Spec Supplement)
static const uint8_t AD_FLAGS = 0x01, AD_SHORT_NAME = 0x08, AD_MANUFACTURER = 0xFF;

static void updateAdvertisement() {
  // Raw advertising data, 31 bytes max:
  //   flags (3) + manufacturer data (4 + 18) + short name (2 + 3) = 30
  uint8_t adv[31];
  size_t n = 0;
  adv[n++] = 2; adv[n++] = AD_FLAGS;
  adv[n++] = 0x06;                       // LE general discoverable, no BR/EDR
  adv[n++] = 3 + PAYLOAD_LEN; adv[n++] = AD_MANUFACTURER;
  put16(adv + n, COMPANY_ID); n += 2;
  buildPayload(adv + n); n += PAYLOAD_LEN;
  adv[n++] = 1 + strlen(ADV_NAME); adv[n++] = AD_SHORT_NAME;
  memcpy(adv + n, ADV_NAME, strlen(ADV_NAME)); n += strlen(ADV_NAME);

  BLEAdvertisementData data;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  data.addData(String((const char *)adv, n));
#else
  data.addData(std::string((const char *)adv, n));
#endif
  // Updating the data while advertising is allowed; no restart needed.
  advertising->setAdvertisementData(data);
}

// ------------------------------------------------------------ Bluetooth link
//
// Besides the broadcast, the sender takes short connections from the Pi:
//   service 5f6d0001-...  write  5f6d0002-...  commands, first byte 'C' + text
//                                               or 'D' + firmware data
//                         read/notify 5f6d0003-...  replies, notified in chunks
//                                               and ended with 0x04 (EOT)
// The Pi checks in every few minutes ("hb"), which keeps the own Wi-Fi off in
// auto mode, sets the Wi-Fi mode and reads "info". Everything except "auth"
// and "info" needs "auth <password>" first (the own Wi-Fi password).
// Firmware updates (4.1+): Bluetooth only carries one short command,
//   fwget <url>\t<van Wi-Fi name>\t<van Wi-Fi password>
// The sensor joins the van Wi-Fi, downloads the file from the Pi over HTTP and
// installs it (checked by the update library; the old firmware stays if
// anything fails), then restarts. Sending 1.2 MB through Bluetooth was slow
// and broke easily; Wi-Fi does it in seconds.
#define LINK_SERVICE_UUID    "5f6d0001-8d4b-4bd1-9a1c-7ad0a8e3c101"
#define LINK_CMD_UUID        "5f6d0002-8d4b-4bd1-9a1c-7ad0a8e3c101"
#define LINK_REPLY_UUID      "5f6d0003-8d4b-4bd1-9a1c-7ad0a8e3c101"

static BLEServer *linkServer = nullptr;
static BLECharacteristic *linkReply = nullptr;
static volatile bool linkAuthed = false;
static volatile bool advRestart = false;   // a device connected: broadcast on again (from loop(), a bit later)
static volatile uint32_t linkConnectedAt = 0;  // millis() of the last connection
#define ADV_AFTER_CONNECT_MS 3000            // let the connection settle (service discovery, MTU) first
static uint32_t advRestarts = 0;           // how often the watchdog had to restart it
static volatile bool linkConnected = false;
static String apPassword;                  // own Wi-Fi password, also the Bluetooth key
static uint32_t lastPiHeard = 0;           // millis() of the last "hb" (0 = never)
static String fwError;                     // why the last firmware download failed (shown to the Pi)

// Wi-Fi state, run by updateWifi() (stored in NVS namespace "wifi")
static String wifiMode = WIFI_MODE_DEFAULT; // auto | van | ap
static String staSsid, staPass;             // van Wi-Fi
#define MAX_KNOWN 6                         // Wi-Fi networks remembered (name + password), newest first
static String knownSsid[MAX_KNOWN], knownPass[MAX_KNOWN];
static bool staDirty = false;               // van Wi-Fi settings changed: reconnect
static bool apOn = false, staOn = false;
static uint32_t apOnSince = 0, buttonUntil = 0;
static uint32_t lastWebUse = 0;             // last request to the page (a phone that is only joined doesn't count)
static const char *wifiReason = "";         // why the own Wi-Fi is on, for the page
#define WEB_IDLE_MS (10UL * 60UL * 1000UL)   // a joined phone keeps the Wi-Fi on only while the page was used this recently

static bool piRecent() { return lastPiHeard && millis() - lastPiHeard < PI_SILENT_MS; }

static void loadWifiSettings() {
  prefs.begin("wifi", true);
  apPassword = prefs.getString("appass", AP_PASSWORD_DEFAULT);
  wifiMode = prefs.getString("mode", WIFI_MODE_DEFAULT);
  staSsid = prefs.getString("ssid", "");
  staPass = prefs.getString("pass", "");
  for (uint8_t i = 0; i < MAX_KNOWN; i++) {
    knownSsid[i] = prefs.getString(("k" + String(i)).c_str(), "");
    knownPass[i] = prefs.getString(("q" + String(i)).c_str(), "");
  }
  prefs.end();
  if (apPassword.length() < 8) apPassword = AP_PASSWORD_DEFAULT;
  if (wifiMode != "auto" && wifiMode != "van" && wifiMode != "ap" && wifiMode != "off") wifiMode = WIFI_MODE_DEFAULT;
  if (staSsid.length() && knownIndex(staSsid) < 0) rememberNetwork(staSsid, staPass);   // set before 4.4
}

static void saveWifiSetting(const char *key, const String &value) {
  prefs.begin("wifi", false);
  prefs.putString(key, value);
  prefs.end();
}

static int knownIndex(const String &ssid) {
  if (!ssid.length()) return -1;
  for (uint8_t i = 0; i < MAX_KNOWN; i++) if (knownSsid[i] == ssid) return i;
  return -1;
}

static void saveKnown() {
  prefs.begin("wifi", false);
  for (uint8_t i = 0; i < MAX_KNOWN; i++) {
    prefs.putString(("k" + String(i)).c_str(), knownSsid[i]);
    prefs.putString(("q" + String(i)).c_str(), knownPass[i]);
  }
  prefs.end();
}

// Put a network at the top of the known list (the oldest drops out when full)
static void rememberNetwork(const String &ssid, const String &pass) {
  if (!ssid.length()) return;
  int at = knownIndex(ssid);
  if (at == 0 && knownPass[0] == pass) return;
  int from = at < 0 ? MAX_KNOWN - 1 : at;
  for (int i = from; i > 0; i--) { knownSsid[i] = knownSsid[i - 1]; knownPass[i] = knownPass[i - 1]; }
  knownSsid[0] = ssid; knownPass[0] = pass;
  saveKnown();
}

static bool forgetNetwork(const String &ssid) {
  int at = knownIndex(ssid);
  if (at < 0) return false;
  for (int i = at; i < MAX_KNOWN - 1; i++) { knownSsid[i] = knownSsid[i + 1]; knownPass[i] = knownPass[i + 1]; }
  knownSsid[MAX_KNOWN - 1] = ""; knownPass[MAX_KNOWN - 1] = "";
  saveKnown();
  return true;
}

static String jsonEsc(String v) { v.replace("\\", "\\\\"); v.replace("\"", "\\\""); return v; }

static String staIp();                       // in the Wi-Fi part
static bool staConnected();
static String wifiScanJson(int maxN);

// What the Pi reads: firmware, Wi-Fi state, probe names
static String infoJson() {
  String j = "{\"fw\":\"" FW_VERSION "\",\"id\":" + String(DEVICE_ID);
  j += ",\"mode\":\"" + wifiMode + "\",\"ap\":" + String(apOn ? 1 : 0);
  j += ",\"sta\":" + String(staConnected() ? 1 : 0) + ",\"ip\":\"" + staIp() + "\"";
  j += ",\"ssid\":\"" + jsonEsc(staSsid) + "\",\"up\":" + String(millis() / 1000);
  j += ",\"pi_ago\":" + String(lastPiHeard ? (long)((millis() - lastPiHeard) / 1000) : -1L);
  j += ",\"fw_err\":\"" + jsonEsc(fwError) + "\"";
  j += ",\"kn\":[";
  for (uint8_t i = 0, k = 0; i < MAX_KNOWN; i++)
    if (knownSsid[i].length()) { if (k++) j += ','; j += "\"" + jsonEsc(knownSsid[i]) + "\""; }
  j += "]";
  j += ",\"tn\":[";
  for (uint8_t i = 0; i < MAX_TEMPS; i++) {
    if (i) j += ',';
    j += "\"" + jsonEsc(tempPresent[i] ? tempName[i] : String()) + "\"";
  }
  j += "]}";
  return j;
}
static portMUX_TYPE linkMux = portMUX_INITIALIZER_UNLOCKED;
static String linkQueue[8];
static uint8_t linkQHead = 0, linkQCount = 0;

// Firmware download from the Pi (fwget), run from loop()
static String fwUrl, fwSsid, fwPass;       // set by the command, used by fwGet()
static bool fwPending = false, fwBusy = false;
static volatile bool webOtaActive = false; // an upload on the sensor's own page

// Firmware over Bluetooth (4.2+), in batches over the same connection as the live reading:
//   "ota begin <size> <md5>"   -> "ok ota begin mtu=<n>"   (n: the packet size that fits one frame)
//   'D' + offset (uint32 LE) + data, any number      (no answer; repeats and gaps are harmless)
//   "ota status"               -> "ota active <got>/<size>"   the Pi carries on from <got>
//   "ota end"                  -> "ok ota done, restarting" | "error ..."
// A dropped connection doesn't end it: the Pi reconnects and asks "ota status". With no
// data for OTA_IDLE_MS it is given up and the old firmware stays.
#define OTA_IDLE_MS (3UL * 60UL * 1000UL)
static volatile bool otaActive = false;
static volatile bool otaFailed = false;
static volatile uint32_t otaSize = 0, otaGot = 0, otaLastData = 0;
static uint32_t otaRestartAt = 0;
static uint32_t rebootAt = 0;              // "reboot": restart once the answer has gone out

static bool otaBusy() { return fwBusy || webOtaActive || otaActive || otaRestartAt; }

static void linkSetReply(const String &text) {
  if (linkReply) linkReply->setValue(text);
}

// Reply to a queued command: notify in MTU-sized chunks, then EOT
static void linkNotify(const String &text) {
  if (!linkReply || !linkConnected) { dlog("reply lost: the Pi was gone (%u bytes)", text.length()); return; }
  size_t chunk = 20;
  if (linkServer) {
    uint16_t mtu = linkServer->getPeerMTU(linkServer->getConnId());
    if (mtu > 23) chunk = min((size_t)(mtu - 3), (size_t)180);
  }
  for (size_t i = 0; i < text.length(); i += chunk) {
    String part = text.substring(i, min(text.length(), i + chunk));
    linkReply->setValue(part);
    linkReply->notify();
    delay(5);
  }
  linkReply->setValue(String("\x04"));
  linkReply->notify();
  linkSetReply(text.length() > 500 ? text.substring(0, 500) : text);
}

// Handled right away in the Bluetooth task (the password check); the rest runs in loop()
static String linkImmediate(const String &cmd) {
  if (cmd.startsWith("auth ")) {
    linkAuthed = cmd.substring(5) == apPassword;
    dlog("auth %s", linkAuthed ? "ok" : "DENIED (wrong sensor password on the Pi)");
    return linkAuthed ? "ok auth" : "denied: wrong password";
  }
  return String();
}

class LinkServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *s) override {
    linkConnected = true; linkAuthed = false;
    linkConnectedAt = millis();
    advRestart = true;                       // keep broadcasting the level while connected (after a moment)
    // No connection-parameter request: right at the start of a connection it made the
    // Raspberry Pi's built-in Bluetooth drop the link.
  }
  void onDisconnect(BLEServer *s) override {
    linkConnected = false; linkAuthed = false;
    dlog("BT disconnected after %.1f s", (millis() - linkConnectedAt) / 1000.0f);
  }
#if defined(CONFIG_NIMBLE_ENABLED)
  void onConnect(BLEServer *s, ble_gap_conn_desc *d) override {
    dlog("BT connected (interval %u ms, latency %u, timeout %u ms)", d->conn_itvl * 5 / 4, d->conn_latency, d->supervision_timeout * 10);
  }
  void onMtuChanged(BLEServer *s, ble_gap_conn_desc *d, uint16_t mtu) override { dlog("BT packet size (MTU) %u", mtu); }
#else
  void onConnect(BLEServer *s, esp_ble_gatts_cb_param_t *p) override { dlog("BT connected"); }
#endif
};

class LinkCmdCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    String v = c->getValue();
    if (v.length() < 1) return;
    if (v[0] == 'D') {                       // firmware data (see "Firmware over Bluetooth")
      if (!otaActive || !linkAuthed || otaFailed || v.length() < 5) return;
      const uint8_t *d = (const uint8_t *)v.c_str() + 1;
      uint32_t off = d[0] | (d[1] << 8) | (d[2] << 16) | ((uint32_t)d[3] << 24);
      const uint8_t *data = d + 4;
      size_t n = v.length() - 5;
      if (off != otaGot) return;             // a repeat or a gap: "ota status" tells the Pi where to go on
      if (off == 0 && n >= 28 && data[27] != 0x3C) { otaFailed = true; return; }   // not an app image
      if (Update.write((uint8_t *)data, n) != n) otaFailed = true;
      else { otaGot += n; otaLastData = millis(); }
      return;
    }
    if (v[0] != 'C') return;
    String cmd = v.substring(1);
    cmd.trim();
    String now = linkImmediate(cmd);
    if (now.length()) { linkSetReply(now); return; }
    portENTER_CRITICAL(&linkMux);
    bool queued = linkQCount < 8;
    if (queued) { linkQueue[(linkQHead + linkQCount) % 8] = cmd; linkQCount++; }
    portEXIT_CRITICAL(&linkMux);
    if (!queued) dlog("command dropped (queue full): %s", dlogCmd(cmd).c_str());
  }
};

static void handleCommand(String line);

// Queued commands run here, in loop(), like serial commands
static void linkPoll() {
  if (otaActive && millis() - otaLastData > OTA_IDLE_MS) {
    Update.abort();                          // the Pi did not come back: keep the old firmware
    otaActive = false;
    dlog("BT update given up: no data for 3 min (%lu of %lu bytes)", (unsigned long)otaGot, (unsigned long)otaSize);
    Serial.println("Bluetooth firmware upload given up (no data for 3 minutes); keeping the old firmware.");
  }
  if (rebootAt && (int32_t)(millis() - rebootAt) >= 0) {
    Serial.println("Restarting (reboot command).");
    delay(100);
    ESP.restart();
  }
  if (otaRestartAt && (int32_t)(millis() - otaRestartAt) >= 0) {
    Serial.println("New firmware installed over Bluetooth: restarting.");
    delay(100);
    ESP.restart();
  }
  while (true) {
    String cmd;
    portENTER_CRITICAL(&linkMux);
    bool have = linkQCount > 0;
    if (have) { cmd = linkQueue[linkQHead]; linkQHead = (linkQHead + 1) % 8; linkQCount--; }
    portEXIT_CRITICAL(&linkMux);
    if (!have) return;
    {
      // the live view sends hb/fast/live twice a second: fold repeats into one line
      static String lastLogged; static uint32_t lastAt = 0, reps = 0;
      bool repeat = (cmd == "hb" || cmd == "fast" || cmd == "live") && cmd == lastLogged && millis() - lastAt < 10000;
      if (repeat) reps++;
      else {
        if (reps) dlog("  (%lu more like this)", (unsigned long)reps);
        reps = 0;
        dlog("cmd %s", dlogCmd(cmd).c_str());
      }
      lastLogged = cmd; lastAt = millis();
    }
    StringOut reply;
    if (!linkAuthed && cmd != "info") {
      reply.print("denied: auth first");
    } else {
      out = &reply;
      handleCommand(cmd);
      out = &Serial;
    }
    if (reply.text.length() == 0) reply.text = "ok";
    linkNotify(reply.text);
  }
}

static void bleStart() {
  BLEDevice::init(ADV_NAME);
  BLEDevice::setMTU(517);
  linkServer = BLEDevice::createServer();
  linkServer->setCallbacks(new LinkServerCallbacks());
  linkServer->advertiseOnDisconnect(true);
  BLEService *svc = linkServer->createService(LINK_SERVICE_UUID);
  BLECharacteristic *cmd = svc->createCharacteristic(LINK_CMD_UUID, BLECharacteristic::PROPERTY_WRITE);
  cmd->setCallbacks(new LinkCmdCallbacks());
  linkReply = svc->createCharacteristic(LINK_REPLY_UUID,
                                        BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
#if defined(CONFIG_BLUEDROID_ENABLED)
  linkReply->addDescriptor(new BLE2902());
#endif
  linkReply->setValue(String("ready"));
  svc->start();
  advertising = BLEDevice::getAdvertising();

  uint16_t interval = ADV_INTERVAL_MS * 1000 / 625;  // 0.625 ms units
  advertising->setMinInterval(interval);
  advertising->setMaxInterval(interval + interval / 5);
  advertising->setScanResponse(false);
  // Connectable, so the Pi can check in; the broadcast data stays the same
#if defined(CONFIG_NIMBLE_ENABLED)
  advertising->setAdvertisementType(BLE_GAP_CONN_MODE_UND);
#else
  advertising->setAdvertisementType(ADV_TYPE_IND);
#endif

  updateAdvertisement();
  advertising->start();
  Serial.printf("Advertising as %s, MAC %s\n", ADV_NAME,
                BLEDevice::getAddress().toString().c_str());
}

// ------------------------------------------------------------- calibration
//
// Stored in flash:
//   NVS "tank":  zero  float   offset added to the measured height
//                sel   char    active tank: -1 = built-in table, 0.. = saved slot
//   NVS "tanks": nm<i> string  name of saved tank i (empty = free slot)
//                pt<i> blob    its TankPoint[], from "cal save"
// (4.3 and older kept one table as "n"/"pts" in "tank"; it becomes the first saved tank.)
static int8_t activeTank = -1;
static String tankNames[MAX_TANKS];
static float tankCaps[MAX_TANKS];

static void loadDefaults() {
  tablePoints = sizeof(DEFAULT_TABLE) / sizeof(DEFAULT_TABLE[0]);
  memcpy(table, DEFAULT_TABLE, sizeof(DEFAULT_TABLE));
  zeroOffsetMm = ZERO_OFFSET_MM;
}

static bool tableValid(const TankPoint *t, size_t n) {
  if (n < 2 || n > MAX_POINTS) return false;
  for (size_t i = 1; i < n; i++)
    if (!(t[i].mm > t[i - 1].mm) || t[i].litres < t[i - 1].litres) return false;
  return t[n - 1].litres > 0;
}

// Read saved tank i into t; returns the number of points (0 = none / broken)
static size_t readTank(int i, TankPoint *t) {
  prefs.begin("tanks", true);
  size_t bytes = prefs.isKey(("pt" + String(i)).c_str()) ? prefs.getBytesLength(("pt" + String(i)).c_str()) : 0;
  size_t n = bytes / sizeof(TankPoint);
  if (n < 2 || n > MAX_POINTS || prefs.getBytes(("pt" + String(i)).c_str(), t, n * sizeof(TankPoint)) != n * sizeof(TankPoint)) n = 0;
  prefs.end();
  return n && tableValid(t, n) ? n : 0;
}

static void writeTank(int i, const String &name, const TankPoint *t, size_t n) {
  prefs.begin("tanks", false);
  if (n) { prefs.putString(("nm" + String(i)).c_str(), name); prefs.putBytes(("pt" + String(i)).c_str(), t, n * sizeof(TankPoint)); }
  else { prefs.remove(("nm" + String(i)).c_str()); prefs.remove(("pt" + String(i)).c_str()); }
  prefs.end();
}

// Names and capacities of the saved tanks, for the page
static void loadTankList() {
  TankPoint t[MAX_POINTS];
  for (int i = 0; i < MAX_TANKS; i++) {
    size_t n = readTank(i, t);
    prefs.begin("tanks", true);
    tankNames[i] = n ? prefs.getString(("nm" + String(i)).c_str(), "") : String();
    prefs.end();
    if (n && !tankNames[i].length()) tankNames[i] = "Tank " + String(i + 1);
    tankCaps[i] = n ? t[n - 1].litres : 0;
  }
}

static void saveActiveTank() {
  prefs.begin("tank", false);
  prefs.putChar("sel", activeTank);
  prefs.end();
}

// Make tank i (-1 = built-in) the one that converts height to litres
static bool useTank(int i) {
  if (i < 0) {
    tablePoints = sizeof(DEFAULT_TABLE) / sizeof(DEFAULT_TABLE[0]);
    memcpy(table, DEFAULT_TABLE, sizeof(DEFAULT_TABLE));
  } else {
    TankPoint t[MAX_POINTS];
    size_t n = i < MAX_TANKS ? readTank(i, t) : 0;
    if (!n) return false;
    memcpy(table, t, n * sizeof(TankPoint));
    tablePoints = n;
  }
  activeTank = i;
  return true;
}

static String activeTankName() { return activeTank < 0 ? String(BUILTIN_TANK_NAME) : tankNames[activeTank]; }

static void loadCalibration() {
  loadDefaults();
  prefs.begin("tank", false);
  zeroOffsetMm = prefs.getFloat("zero", ZERO_OFFSET_MM);
  int8_t sel = prefs.getChar("sel", -1);
  // the one table of 4.3 and older -> first free slot
  size_t n = prefs.getUInt("n", 0);
  TankPoint old[MAX_POINTS];
  bool migrate = n >= 2 && n <= MAX_POINTS &&
                 prefs.getBytes("pts", old, n * sizeof(TankPoint)) == n * sizeof(TankPoint) && tableValid(old, n);
  if (prefs.isKey("n")) { prefs.remove("n"); prefs.remove("pts"); }
  prefs.end();
  loadTankList();
  if (migrate) {
    for (int i = 0; i < MAX_TANKS; i++)
      if (!tankNames[i].length()) { writeTank(i, "My calibration", old, n); sel = i; break; }
    loadTankList();
    prefs.begin("tank", false); prefs.putChar("sel", sel); prefs.end();
  }
  if (!useTank(sel)) useTank(-1);
  Serial.printf("Tank: %s, %u points, %.1f L, zero offset %.1f mm\n",
                activeTankName().c_str(), (unsigned)tablePoints, capacityL(), zeroOffsetMm);
}

static void saveZero() {
  prefs.begin("tank", false);
  prefs.putFloat("zero", zeroOffsetMm);
  prefs.end();
}


// Average the unfiltered height over a few seconds while the water is calm.
// Blocks the loop; BLE keeps advertising the last payload meanwhile.
static bool measureSteadyHeight(float &result, uint32_t ms = 5000) {
  if (sloshing) {
    out->println("Water is still moving (SLOSH) - wait until it settles and try again.");
    return false;
  }
  double sum = 0;
  uint32_t n = 0;
  float lo = 1e9f, hi = -1e9f;
  for (uint32_t t0 = millis(); millis() - t0 < ms; ) {
    sampleLevel();
    if (fault) { out->println("Sensor fault - check wiring."); return false; }
    sum += lastRawHeight;
    n++;
    lo = min(lo, lastRawHeight);
    hi = max(hi, lastRawHeight);
    for (uint32_t t1 = millis(); millis() - t1 < SAMPLE_PERIOD_MS; ) { updateLed(); delay(5); }  // keep breathing
  }
  result = (float)(sum / n);
  out->printf("  measured %.1f mm (spread %.1f mm over %u bursts)\n", result, hi - lo, (unsigned)n);
  if (hi - lo > 4 * SLOSH_EXIT_MM) {
    out->println("  spread is large - the level was still changing. Wait and repeat.");
    return false;
  }
  return true;
}

// In-progress calibration, copied to `table` by "cal save"
static TankPoint calPts[MAX_POINTS];
static size_t calCount = 0;
static float calTotal = 0;
static bool calActive = false;

static void printTable(const TankPoint *t, size_t n) {
  out->println("  #   height mm   litres");
  for (size_t i = 0; i < n; i++)
    out->printf("  %-3u %9.1f %8.1f\n", (unsigned)i, t[i].mm, t[i].litres);
}

static void printAsCode() {
  out->println("// paste into DEFAULT_TABLE in the sketch:");
  for (size_t i = 0; i < tablePoints; i++)
    out->printf("  { %6.1ff, %6.1ff },\n", table[i].mm, table[i].litres);
  out->printf("// zero offset: #define ZERO_OFFSET_MM %.1ff\n", zeroOffsetMm);
}

static void calAppend(float mm, float litres) {
  if (calCount >= MAX_POINTS) { out->println("Table full."); return; }
  if (calCount > 0 && mm <= calPts[calCount - 1].mm) {
    out->printf("Height %.1f mm is not above the previous point (%.1f mm) - not added. "
                  "Add more water per step, or check the sensor.\n", mm, calPts[calCount - 1].mm);
    return;
  }
  calPts[calCount++] = { mm, litres };
  calTotal = litres;
  out->printf("Point %u: %.1f mm = %.1f L\n", (unsigned)(calCount - 1), mm, litres);
}

static void printHelp() {
  out->println(
    "Commands (end with Enter):\n"
    "  status              current reading and active table\n"
    "  log                 toggle the periodic status line\n"
    "  fast / fast off     test mode: no smoothing for 15 min (bench tests)\n"
    "  wifi                Wi-Fi state\n"
    "  wifi mode <m>       auto (Bluetooth, own Wi-Fi as fallback) | van | ap | off\n"
    "  wifi ssid <name>    van Wi-Fi name    (wifi ssid - clears it)\n"
    "  wifi pass <text>    van Wi-Fi password\n"
    "  wifi join <name>[TAB<password>]  join a network (a known one needs no password)\n"
    "  wifi known          remembered networks\n"
    "  wifi forget <name>  remove a remembered network\n"
    "  info                state as JSON (used by the Pi)\n"
    "  fwget <url>[\\t<wifi>\\t<password>]  download and install firmware over Wi-Fi\n"
    "  ota begin|status|end|abort   firmware over Bluetooth (used by the Pi)\n"
    "  live                raw reading as JSON (the Pi's live view)\n"
    "  sim <mm> | <n>%     broadcast this level instead of the sensor (15 min)\n"
    "  sim off             back to the sensor\n"
    "  zero                tank EMPTY: make the current height read 0 mm\n"
    "  zero <mm>           set the zero offset by hand\n"
    "  cal start           tank EMPTY: start a new table with 0 L at the current height\n"
    "  cal add <litres>    after pouring <litres> in and waiting ~30 s: record a point\n"
    "  cal point <mm> <L>  add a point by hand (heights include the zero offset)\n"
    "  cal undo            remove the last point\n"
    "  cal show            show the table being measured\n"
    "  cal save [name]     store the new table as a tank and use it (last point = 100 %)\n"
    "  cal cancel          discard the table being measured\n"
    "  table               print the active table as code for the sketch\n"
    "  temps               list the temperature probes\n"
    "  name <n> <text>     name temperature probe n (1, 2)\n"
    "  tank                list the tanks (* = in use)\n"
    "  tank use <n>        use tank n (0 = built-in)\n"
    "  tank rename <n> <name> | tank delete <n>\n"
    "  reset               built-in tank and zero offset 0 (saved tanks are kept)\n"
    "  reboot              restart the sensor (settings are kept)\n"
    "  dlog [clear]        debug log: Bluetooth, Wi-Fi and updates");
}

static void handleCommand(String line) {
  line.trim();
  if (line.length() == 0) return;
  int sp1 = line.indexOf(' ');
  String cmd = sp1 < 0 ? line : line.substring(0, sp1);
  String rest = sp1 < 0 ? String() : line.substring(sp1 + 1);
  rest.trim();
  cmd.toLowerCase();

  if (cmd == "help" || cmd == "?") {
    printHelp();
  } else if (cmd == "hb") {
    lastPiHeard = millis();
    if (!lastPiHeard) lastPiHeard = 1;
    out->print("ok");
  } else if (cmd == "info") {
    out->print(infoJson());
  } else if (cmd == "ota") {
    // runs in loop() (queued from Bluetooth); only the data packets are handled in the Bluetooth task
    if (rest.startsWith("begin ")) {
      if (fwBusy || webOtaActive) { out->print("error ota: another update is running"); return; }
      String a = rest.substring(6);
      int sp = a.indexOf(' ');
      uint32_t size = (uint32_t)(sp < 0 ? a : a.substring(0, sp)).toInt();
      String md5 = sp < 0 ? String() : a.substring(sp + 1);
      md5.trim();
      if (otaActive) Update.abort();
      otaActive = false; otaFailed = false; otaGot = 0; otaSize = size; otaLastData = millis();
      if (size == 0 || !Update.begin(size)) { out->print(String("error ota begin: ") + Update.errorString()); return; }
      if (md5.length() == 32) Update.setMD5(md5.c_str());
      otaActive = true;
      dlog("BT update started: %lu bytes", (unsigned long)size);
      uint16_t mtu = linkServer ? linkServer->getPeerMTU(linkServer->getConnId()) : 23;
      out->printf("ok ota begin mtu=%u", (unsigned)mtu);
    } else if (rest == "status") {
      if (!otaActive) out->print("ota idle");
      else out->printf("ota %s %lu/%lu", otaFailed ? "failed" : "active", (unsigned long)otaGot, (unsigned long)otaSize);
    } else if (rest == "end") {
      if (!otaActive) { out->print("error ota: not started"); return; }
      otaActive = false;
      if (otaFailed || otaGot != otaSize) {
        Update.abort();
        out->printf("error ota: %s", otaFailed ? "not an app image, or flash write failed" : "file incomplete");
      } else if (!Update.end(true)) {
        out->print(String("error ota end: ") + Update.errorString());
      } else {
        dlog("BT update complete: restarting");
        otaRestartAt = millis() + 1500;      // after this answer has gone out
        if (!otaRestartAt) otaRestartAt = 1;
        out->print("ok ota done, restarting");
      }
    } else if (rest == "abort") {
      if (otaActive) Update.abort();
      otaActive = false;
      out->print("ok ota aborted");
    } else {
      out->print("ota begin <size> <md5> | status | end | abort");
    }
  } else if (cmd == "fwget") {
    // url, then (separated by tabs) the van Wi-Fi to join for the download
    int t1 = rest.indexOf('\t');
    String url = t1 < 0 ? rest : rest.substring(0, t1);
    String creds = t1 < 0 ? String() : rest.substring(t1 + 1);
    int t2 = creds.indexOf('\t');
    url.trim();
    if (!url.startsWith("http://")) { out->print("error fwget: needs an http:// address"); return; }
    if (otaActive) { out->print("error fwget: a Bluetooth update is running"); return; }
    fwUrl = url;
    fwSsid = t2 < 0 ? creds : creds.substring(0, t2);
    fwPass = t2 < 0 ? String() : creds.substring(t2 + 1);
    if (fwSsid.length() == 0) { fwSsid = staSsid; fwPass = staPass; }   // the sensor's own Wi-Fi network
    if (fwSsid.length() == 0 && !staConnected()) { out->print("error fwget: no Wi-Fi to download over (connect the sensor to a Wi-Fi network first)"); return; }
    fwError = "";
    fwPending = true;                     // runs in loop() once this reply has gone out
    out->print("ok fwget: joining the Wi-Fi and downloading");
  } else if (cmd == "sim") {
    if (rest == "off" || rest.length() == 0) {
      bool was = simActive();
      simHeight = NAN;
      emaHeight = NAN;                    // the filter starts again from the real sensor
      advNow = true;
      out->println(was ? "Simulation off: the sensor again." : "No simulation running.");
      return;
    }
    float mm = rest.endsWith("%") ? litresToHeight(constrain(rest.toFloat(), 0.0f, 100.0f) * capacityL() / 100.0f)
                                  : rest.toFloat();
    if (mm < 0 || mm > 5000) { out->println("Usage: sim <mm> | sim <percent>% | sim off"); return; }
    simHeight = mm;
    simSince = millis();
    simUntil = simSince + SIM_MS;
    advNow = true;                        // out at once, so the timer starts with the broadcast
    out->printf("Simulating %.0f mm = %.1f L (%.0f %%), sent now. Watch Venus OS.\n",
                mm, heightToLitres(mm), heightToLitres(mm) / capacityL() * 100.0f);
  } else if (cmd == "live") {
    // Unfiltered signal and height, plus what the filter makes of it
    float h = isnan(emaHeight) ? NAN : max(0.0f, emaHeight);
    String j = "{\"s\":" + (isnan(lastSignal) ? String("null") : String(lastSignal, 3));
    j += String(",\"u\":\"") + (INPUT_CURRENT_LOOP ? "mA" : "V") + "\"";
    j += ",\"raw\":" + (isnan(lastRawHeight) ? String("null") : String(lastRawHeight, 1));
    j += ",\"h\":" + (isnan(h) ? String("null") : String(h, 1));
    j += ",\"l\":" + (isnan(h) ? String("null") : String(heightToLitres(h), 1));
    j += ",\"fault\":" + String(fault ? 1 : 0);
    j += ",\"fast\":" + String(fastMode() ? (unsigned long)((fastUntil - millis()) / 1000) : 0UL) + "}";
    out->print(j);
  } else if (cmd == "wifi") {
    int sp = rest.indexOf(' ');
    String sub = sp < 0 ? rest : rest.substring(0, sp);
    String arg = sp < 0 ? String() : rest.substring(sp + 1);
    sub.toLowerCase();
    if (sub == "mode") {
      arg.toLowerCase(); arg.trim();
      if (arg != "auto" && arg != "van" && arg != "ap" && arg != "off") { out->println("wifi mode auto | van | ap | off"); return; }
      if (arg != wifiMode) { wifiMode = arg; saveWifiSetting("mode", arg); }
      out->printf("ok wifi mode %s\n", wifiMode.c_str());
    } else if (sub == "ssid") {
      if (arg == "-") arg = "";
      if (arg.length() > 32) { out->println("ssid too long"); return; }
      if (arg != staSsid) {
        staSsid = arg; saveWifiSetting("ssid", arg); staDirty = true;
        int k = knownIndex(arg);                 // a known network brings its password along
        staPass = k < 0 ? String() : knownPass[k];
        saveWifiSetting("pass", staPass);
      }
      out->println("ok wifi ssid");
    } else if (sub == "pass") {
      if (arg.length() > 63) { out->println("password too long"); return; }
      if (arg.length() == 0 && knownIndex(staSsid) >= 0) arg = knownPass[knownIndex(staSsid)];  // empty: keep the known one
      if (arg != staPass) { staPass = arg; saveWifiSetting("pass", arg); staDirty = true; }
      rememberNetwork(staSsid, staPass);
      out->println("ok wifi pass");
    } else if (sub == "join") {
      // join <name>[TAB<password>]: without a password the remembered one is used
      int t = arg.indexOf('\t');
      String ssid = t < 0 ? arg : arg.substring(0, t);
      String pass = t < 0 ? String() : arg.substring(t + 1);
      ssid.trim();
      if (!ssid.length() || ssid.length() > 32 || pass.length() > 63) { out->println("Usage: wifi join <name>[TAB<password>]"); return; }
      if (t < 0 && knownIndex(ssid) >= 0) pass = knownPass[knownIndex(ssid)];
      staSsid = ssid; staPass = pass;
      saveWifiSetting("ssid", ssid); saveWifiSetting("pass", pass);
      rememberNetwork(ssid, pass);
      if (wifiMode != "van") { wifiMode = "van"; saveWifiSetting("mode", "van"); }
      staDirty = true;
      out->printf("ok joining '%s'\n", ssid.c_str());
    } else if (sub == "forget") {
      if (!forgetNetwork(arg)) { out->printf("'%s' is not a known network\n", arg.c_str()); return; }
      if (arg == staSsid) { staSsid = ""; staPass = ""; saveWifiSetting("ssid", ""); saveWifiSetting("pass", ""); staDirty = true; }
      out->printf("ok forgot '%s'\n", arg.c_str());
    } else if (sub == "scan") {
      out->print(wifiScanJson(15));
    } else if (sub == "known") {
      for (uint8_t i = 0; i < MAX_KNOWN; i++)
        if (knownSsid[i].length()) out->printf("  %s%s\n", knownSsid[i].c_str(), knownSsid[i] == staSsid ? "   (selected)" : "");
    } else {
      out->println(infoJson());
    }
  } else if (cmd == "fast") {
    if (rest == "off") {
      fastUntil = 0;
      out->println("Test mode off: normal smoothing again.");
    } else {
      fastUntil = millis() + FAST_MODE_MS;
      if (!fastUntil) fastUntil = 1;
      out->println("Test mode on for 15 minutes: the level follows the signal at once (no smoothing).");
    }
  } else if (cmd == "log") {
    serialLog = !serialLog;
    out->printf("Periodic log %s\n", serialLog ? "on" : "off");
  } else if (cmd == "status") {
    out->printf("signal=%.3f raw height=%.1f mm filtered=%.1f mm zero offset=%.1f mm "
                  "litres=%.1f/%.1f%s%s\n", lastSignal, lastRawHeight, emaHeight, zeroOffsetMm,
                  heightToLitres(max(0.0f, emaHeight)), capacityL(),
                  sloshing ? " SLOSH" : "", fault ? " FAULT" : "");
    printTable(table, tablePoints);
  } else if (cmd == "zero") {
    float h;
    if (rest.length()) {
      zeroOffsetMm = rest.toFloat();
    } else {
      if (!measureSteadyHeight(h)) return;
      zeroOffsetMm -= h;              // h already includes the old offset
    }
    saveZero();
    emaHeight = NAN;                  // restart the filter at the new zero
    out->printf("Zero offset %.1f mm stored.\n", zeroOffsetMm);
  } else if (cmd == "cal") {
    int sp2 = rest.indexOf(' ');
    String sub = sp2 < 0 ? rest : rest.substring(0, sp2);
    String arg = sp2 < 0 ? String() : rest.substring(sp2 + 1);
    arg.trim();
    sub.toLowerCase();
    if (sub == "start") {
      float h;
      if (!measureSteadyHeight(h)) return;
      calCount = 0;
      calActive = true;
      calAppend(h, 0.0f);
      out->println("Now pour in a known amount, wait ~30 s, then: cal add <litres>");
    } else if (sub == "add" || sub == "point") {
      if (!calActive) { calActive = true; calCount = 0; calTotal = 0; }
      if (sub == "add") {
        float l = arg.toFloat();
        if (l <= 0) { out->println("Usage: cal add <litres poured in this step>"); return; }
        float h;
        if (!measureSteadyHeight(h)) return;
        calAppend(h, calTotal + l);
      } else {
        int sp3 = arg.indexOf(' ');
        if (sp3 < 0) { out->println("Usage: cal point <mm> <total litres>"); return; }
        calAppend(arg.substring(0, sp3).toFloat(), arg.substring(sp3 + 1).toFloat());
      }
    } else if (sub == "undo") {
      if (calCount) { calCount--; calTotal = calCount ? calPts[calCount - 1].litres : 0; }
      printTable(calPts, calCount);
    } else if (sub == "show") {
      printTable(calPts, calCount);
      out->printf("  total so far: %.1f L\n", calTotal);
    } else if (sub == "save") {
      // save [name]: stored as a new tank (or over the saved tank with that name) and used from now on
      if (!tableValid(calPts, calCount)) {
        out->println("Need at least 2 points with rising height and litres.");
        return;
      }
      String name = arg.length() ? arg.substring(0, 32) : String();
      int slot = -1;
      for (int i = 0; i < MAX_TANKS && name.length(); i++) if (tankNames[i] == name) slot = i;
      for (int i = 0; i < MAX_TANKS && slot < 0; i++) if (!tankNames[i].length()) slot = i;
      if (slot < 0) { out->printf("All %d tank places are used: delete one first (tank delete <n>).\n", MAX_TANKS); return; }
      if (!name.length()) name = "Tank " + String(slot + 1);
      writeTank(slot, name, calPts, calCount);
      loadTankList();
      useTank(slot);
      saveActiveTank();
      emaHeight = NAN;
      calActive = false;
      out->printf("Saved as tank %d '%s': %u points, %.1f L. It is in use now.\n", slot + 1, name.c_str(), (unsigned)tablePoints, capacityL());
    } else if (sub == "cancel") {
      calActive = false;
      calCount = 0;
      out->println("Calibration discarded.");
    } else {
      out->println("cal start | add <L> | point <mm> <L> | undo | show | save | cancel");
    }
  } else if (cmd == "temps") {
    if (tempCount == 0) out->println("No temperature probes found (wiring, 4.7k pull-up, USE_DS18B20).");
    for (uint8_t i = 0; i < MAX_TEMPS; i++)
      if (tempPresent[i])
        out->printf("  %u  %-24s %6.2f C  GPIO%d  address %s\n", i + 1, tempName[i].c_str(), tempC[i],
                    busPins[tempBus[i]], addrHex(tempAddr[i]).c_str());
      else
        out->printf("  %u  (no probe found)\n", i + 1);
  } else if (cmd == "name") {
    int sp = rest.indexOf(' ');
    int i = (sp < 0 ? rest : rest.substring(0, sp)).toInt();
    if (sp < 0 || i < 1 || !setTempName(i - 1, rest.substring(sp + 1)))
      out->printf("Usage: name <1..%u> <text up to 32 characters> (probe must be found)\n", (unsigned)MAX_TEMPS);
    else
      out->printf("Probe %d is now '%s'.\n", i, tempName[i - 1].c_str());
  } else if (cmd == "table") {
    printAsCode();
  } else if (cmd == "tank") {
    // tank [list] | use <n> | rename <n> <name> | delete <n>    (0 = built-in, 1.. = saved)
    int sp = rest.indexOf(' ');
    String sub = sp < 0 ? rest : rest.substring(0, sp);
    String arg = sp < 0 ? String() : rest.substring(sp + 1);
    arg.trim(); sub.toLowerCase();
    int sp2 = arg.indexOf(' ');
    int n = (sp2 < 0 ? arg : arg.substring(0, sp2)).toInt();
    String txt = sp2 < 0 ? String() : arg.substring(sp2 + 1);
    txt.trim();
    bool saved = n >= 1 && n <= MAX_TANKS && tankNames[n - 1].length();
    if (sub == "use") {
      if (arg != "0" && !saved) { out->println("Usage: tank use <n>   (0 = built-in, see: tank)"); return; }
      useTank(n - 1);
      saveActiveTank();
      emaHeight = NAN;
      out->printf("Using tank %d '%s' (%.1f L).\n", n, activeTankName().c_str(), capacityL());
    } else if (sub == "rename") {
      if (!saved || !txt.length()) { out->println("Usage: tank rename <n> <name>   (saved tanks only)"); return; }
      TankPoint t[MAX_POINTS];
      size_t k = readTank(n - 1, t);
      writeTank(n - 1, txt.substring(0, 32), t, k);
      loadTankList();
      out->printf("Tank %d is now '%s'.\n", n, tankNames[n - 1].c_str());
    } else if (sub == "delete") {
      if (!saved) { out->println("Usage: tank delete <n>   (saved tanks only)"); return; }
      writeTank(n - 1, "", nullptr, 0);
      String was = tankNames[n - 1];
      loadTankList();
      if (activeTank == n - 1) { useTank(-1); saveActiveTank(); emaHeight = NAN; }
      out->printf("Deleted tank %d '%s'. In use: %s.\n", n, was.c_str(), activeTankName().c_str());
    } else {
      out->printf("  %s0  %-24s %6.1f L  built-in\n", activeTank < 0 ? "*" : " ", BUILTIN_TANK_NAME,
                  DEFAULT_TABLE[sizeof(DEFAULT_TABLE) / sizeof(DEFAULT_TABLE[0]) - 1].litres);
      for (int i = 0; i < MAX_TANKS; i++)
        if (tankNames[i].length())
          out->printf("  %s%d  %-24s %6.1f L\n", activeTank == i ? "*" : " ", i + 1, tankNames[i].c_str(), tankCaps[i]);
      out->println("tank use <n> | rename <n> <name> | delete <n>");
    }
  } else if (cmd == "dlog") {
    if (rest == "clear") { portENTER_CRITICAL(&dlogMux); dlogCount = 0; portEXIT_CRITICAL(&dlogMux); out->print("ok log cleared"); }
    else if (rest == "new") out->print(dlogNew());
    else out->print(dlogText());
  } else if (cmd == "reboot" || cmd == "restart") {
    if (fwBusy || webOtaActive || otaRestartAt) { out->print("error reboot: a firmware update is being written"); return; }
    if (otaActive) { Update.abort(); otaActive = false; }   // an unfinished Bluetooth upload: dropped, old firmware stays
    dlog("restart asked for");
    rebootAt = millis() + 1500;              // after this answer has gone out (web page, Bluetooth)
    if (!rebootAt) rebootAt = 1;
    out->print("ok rebooting: back in about 10 s");
  } else if (cmd == "reset") {
    useTank(-1);
    saveActiveTank();
    zeroOffsetMm = ZERO_OFFSET_MM;
    saveZero();
    emaHeight = NAN;
    out->println("Built-in tank and zero offset back to the defaults. Saved tanks are kept (tank delete <n>).");
  } else {
    out->printf("Unknown command '%s' - type help\n", cmd.c_str());
  }
}

static void pollSerial() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length()) handleCommand(buf);
      buf = "";
    } else if (buf.length() < 80) {
      buf += c;
    }
  }
}

// -------------------------------------------------------------- status LED

static bool webUp = false;   // set once the Wi-Fi network and the web server run
static bool otaBusy();       // a firmware upload from the Pi is running (Bluetooth link part)
static uint32_t lastAdvSent = 0;   // millis() of the last broadcast update (blue flash)

static void ledWrite(uint8_t r, uint8_t g, uint8_t b) {
#if STATUS_LED_PIN >= 0
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  rgbLedWrite(STATUS_LED_PIN, r, g, b);
#else
  neopixelWrite(STATUS_LED_PIN, r, g, b);
#endif
#endif
}

// A short dim flash every 2 s: blue while Bluetooth sends, green while Wi-Fi is on,
// taking turns when both are on; bright green/red during a firmware update.
static void updateLed() {
  static uint32_t last = 0;
  static int lastR = -1, lastG = -1, lastB = -1;
  uint32_t now = millis();
  if (now - last < 20) return;              // 50 updates a second is smooth enough
  last = now;
  int r = 0, g = 0, b = 0;
  // One short, dim flash every 2 s. Blue = Bluetooth sends, green = Wi-Fi on; with both,
  // the flashes take turns (blue, green, blue, ...).
  bool bt = advertising && advertising->isAdvertising();
  uint32_t n = now / BLINK_PERIOD_MS;
  bool flash = now % BLINK_PERIOD_MS < BLINK_ON_MS;
  if (otaBusy()) {                          // firmware arriving: green and red alternate, bright
    if ((now / OTA_BLINK_MS) % 2) g = STATUS_LED_MAX; else r = STATUS_LED_MAX;
  } else if (flash) {
    bool blue = bt && (!webUp || n % 2 == 0);
    if (blue) b = STATUS_LED_DIM;
    else if (webUp) g = STATUS_LED_DIM;
  }
  if (r != lastR || g != lastG || b != lastB) { ledWrite(r, g, b); lastR = r; lastG = g; lastB = b; }
}

// ------------------------------------------------------- own Wi-Fi + web page
//
// The sender opens its own Wi-Fi network, so the page works with the Pi off.
// Calibration on this page changes the sender's own table, the same as the
// serial commands (the page simply runs those commands and shows the reply).

#if USE_WIFI_AP

static WebServer server(80);
static DNSServer dns;        // answers every name with our address: phones then open the page by themselves

static const char PAGE[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Water tank</title>
<style>
:root{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd;--card:#f6f6f6;--acc:#111;--accfg:#fff;--bad:#c62828;--ok:#2e7d32;--bt:#1e6fe8;--wf:#2e9d4a;--warn:#d89a00;--water:#3b82d6;--tank-empty:#fff;--tank-line:#222;--tank-text:#444}
@media(prefers-color-scheme:dark){:root{--bg:#000;--fg:#eee;--mut:#999;--line:#333;--card:#111;--acc:#eee;--accfg:#000;--bad:#ef5350;--ok:#66bb6a;--bt:#4d94ff;--wf:#4cc26c;--warn:#f0b400;--water:#3b82d6;--tank-empty:#000;--tank-line:#ddd;--tank-text:#bbb}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 -apple-system,system-ui,sans-serif}
main{max-width:560px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0}h2{font-size:16px;margin:0 0 8px}
header{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0 12px}
.stat{display:flex;gap:6px;flex-wrap:wrap;min-width:0}
.pill{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:999px;padding:3px 9px;font-size:13px;white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);flex:none}
.dot.br{animation:br 2.4s ease-in-out infinite}@keyframes br{0%,100%{opacity:.25}50%{opacity:1}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.big{font-size:44px;font-weight:700;line-height:1}.mut{color:var(--mut);font-size:14px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px}.grid b{display:block;font-size:18px}
.chip{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:1px 8px;font-size:13px;margin:6px 4px 0 0}
.chip.bad{color:var(--bad);border-color:var(--bad)}
button,input,select{font:inherit;border-radius:8px;border:1px solid var(--line);padding:9px 12px;background:var(--bg);color:var(--fg)}
button{cursor:pointer}button.p{background:var(--acc);color:var(--accfg);border-color:var(--acc)}button:disabled{opacity:.5}
.icon{width:40px;height:40px;border-radius:50%;padding:0;font-weight:700;flex:none;line-height:1}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.row input{flex:1;min-width:0}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:6px 0}
.seg button{flex:1;border:0;border-radius:0;border-right:1px solid var(--line);padding:8px 6px}.seg button:last-child{border-right:0}
.seg button.on{background:var(--acc);color:var(--accfg);font-weight:600}
.list{border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:6px 0}
.item{display:flex;align-items:center;gap:8px;padding:7px 10px;border-bottom:1px solid var(--line);background:var(--bg);flex-wrap:wrap}
.item:last-child{border-bottom:0}.item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item button{padding:5px 10px}.item.pick{cursor:pointer}
.lbl{font-size:13px;font-weight:600;color:var(--mut);margin-top:10px}
pre{white-space:pre-wrap;font:13px/1.35 ui-monospace,Menlo,monospace;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;max-height:260px;overflow:auto;margin:8px 0 0}
code{font:13px ui-monospace,Menlo,monospace}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:right;padding:2px 6px;border-bottom:1px solid var(--line)}
details summary{cursor:pointer;font-weight:600}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--fg);width:calc(100% - 32px);max-width:520px;padding:16px}
dialog::backdrop{background:rgba(0,0,0,.55)}ol{padding-left:20px;margin:6px 0}ol li{margin:3px 0}
</style></head><body><main>
<header><h1>Water tank <span class="mut" id="ver"></span></h1>
 <div class="stat">
  <span class="pill" id="btp"><i class="dot" id="btd"></i><span id="btt">–</span></span>
  <span class="pill" id="wfp"><svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M2.5 9a14 14 0 0 1 19 0M6 12.5a9 9 0 0 1 12 0M9.4 16a4 4 0 0 1 5.2 0" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/><circle cx="12" cy="19.3" r="1.7" fill="currentColor"/></svg><span id="wft">–</span></span>
 </div></header>

<div class="card">
 <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
  <div><div class="big"><span id="lit">–</span> L</div>
   <div class="mut">of <span id="cap">–</span> L · <span id="lvl">–</span> %</div></div>
  <button class="icon" onclick="openDlg('zdlg')" title="Set zero (tank empty)" aria-label="Set zero (tank empty)">→0</button>
 </div>
 <div id="tank" style="margin:12px 0"></div>
 <div class="grid">
  <div><span class="mut">Water height</span><b><span id="h">–</span> mm</b></div>
  <div><span class="mut">Sensor signal</span><b id="sig">–</b></div>
  <div><span class="mut">Zero offset</span><b><span id="z">–</span> mm</b></div>
 </div>
 <div id="chips"></div>
 <div class="row"><button id="fastbtn" onclick="cmd(fastOn?'fast off':'fast')">Test mode: fast reading</button></div>
</div>

<div class="card">
 <h2>Tank</h2>
 <div class="row"><select id="tsel" style="flex:1;min-width:0" onchange="cmd('tank use '+this.value)" aria-label="Tank in use"></select></div>
 <div class="row"><button class="p" id="calbtn" onclick="openDlg('caldlg')">Calibrate the tank</button><button id="trn" onclick="tankRename()">Rename</button><button id="tdel" onclick="tankDel()">Delete</button></div>
 <details><summary class="mut">Table in use</summary><div id="tbl"></div></details>
</div>

<div class="card" id="tcard" style="display:none">
 <h2>Temperatures</h2>
 <div id="temps"></div>
 <div class="mut" id="tnote"></div>
</div>

<div class="card">
 <h2>Wi-Fi settings</h2>
 <div class="seg" id="wmode"></div>
 <div class="mut" id="whelp"></div>
 <div id="wnet">
  <div class="lbl">Known networks</div>
  <div id="wknown"></div>
  <div class="row"><button onclick="wscan()" id="wscanb">Scan for other networks</button></div>
  <div id="wscan"></div>
  <details style="margin-top:6px"><summary class="mut">Hidden network</summary>
   <div class="row"><input id="hssid" placeholder="network name" autocomplete="off" autocapitalize="off"></div>
   <div class="row"><input id="hpass" type="password" placeholder="password" autocomplete="off"><button onclick="wjoin($('hssid').value.trim(),$('hpass').value)">Connect</button></div></details>
 </div>
 <div class="mut" id="wstate" style="margin-top:8px"></div>
 <details style="margin-top:8px"><summary class="mut">Password of the own network (AP)</summary>
  <div class="mut">8 to 63 characters. Also the sensor password on the Venus OS page. Join the network again afterwards.</div>
  <div class="row"><input id="pw" type="text" autocomplete="off" placeholder="new password"><button onclick="setPw()">Save</button></div></details>
</div>

<div class="card">
 <h2>Terminal</h2>
 <pre id="outp">Type a command, or pick one from the list.</pre>
 <form class="row" onsubmit="event.preventDefault();cmd($('con').value.trim())"><input id="con" placeholder="command, e.g. status" autocomplete="off" autocapitalize="off" spellcheck="false"><button class="p">Send</button></form>
 <button onclick="cmdsToggle()" id="cmdsb">Show commands</button>
 <div id="cmds" class="list" hidden></div>
</div>

<div class="card"><details id="dlogd" ontoggle="if(this.open)dlogLoad()"><summary>Debug log</summary>
 <div class="mut">What happened on Bluetooth, Wi-Fi and updates (times = time since the sensor started). Copy it to share it.</div>
 <div class="row"><button onclick="dlogLoad()">Refresh</button><button onclick="dlogCopy()">Copy</button></div>
 <pre id="dlog">…</pre></details></div>

<div class="card"><details><summary>Firmware update</summary>
 <div class="mut">Choose the <b>WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin</b> file (not the FLASH-AT-0x0 one). Calibration and settings are kept.</div>
 <div class="row"><input id="fw" type="file" accept=".bin"><button onclick="upd()">Upload</button></div></details></div>

<div class="row"><button onclick="reboot()">Restart the sensor</button></div>
<div class="mut" id="foot"></div>
</main>

<dialog id="zdlg">
 <h2>Set zero</h2>
 <div class="mut">Park level, empty the tank and wait until the water is calm. The height measured now becomes 0 mm (takes about 5 s).</div>
 <div class="row"><button class="p" onclick="cmd('zero')">Tank is empty: set zero</button><button onclick="$('zdlg').close()">Close</button></div>
 <pre class="dout" hidden></pre>
</dialog>

<dialog id="caldlg">
 <h2>Calibrate the tank</h2>
 <div id="calintro">
  <div class="mut">Measures how many litres fit at each height, so the reading matches your tank. Saved as a new tank in the list; the old one stays.</div>
  <ol class="mut">
   <li>Park level, empty the tank, let the water calm down.</li>
   <li>Press <b>Start</b>: the empty level becomes 0 L.</li>
   <li>Pour in a known amount (e.g. a 10 L bucket), wait ~30 s, type the litres of this step and press <b>Record</b>.</li>
   <li>Repeat until full. Smaller steps where the tank shape changes make it more accurate.</li>
   <li>Give it a name and press <b>Save tank</b>. It is used right away.</li>
  </ol>
  <div class="row"><button class="p" onclick="cmd('cal start')">Start: tank is empty</button><button onclick="$('caldlg').close()">Close</button></div>
 </div>
 <div id="calrun" hidden>
  <div class="mut">So far <b id="caltot">0</b> L in <span id="calnp">0</span> points. Pour in the next amount, wait ~30 s, then:</div>
  <div class="row"><input id="add" type="number" inputmode="decimal" placeholder="litres poured in this step"><button class="p" onclick="addPt()">Record</button></div>
  <div id="calbox"></div>
  <div class="row"><button onclick="cmd('cal undo')">Undo last</button><button onclick="if(confirm('Throw the measured points away?'))cmd('cal cancel')">Cancel calibration</button></div>
  <div class="lbl">Tank full?</div>
  <div class="row"><input id="calname" placeholder="name, e.g. Wheel-arch measured" maxlength="32"><button class="p" onclick="calSave()">Save tank</button></div>
  <div class="row"><button onclick="$('caldlg').close()">Close (continue later)</button></div>
 </div>
 <pre class="dout" hidden></pre>
</dialog>
<script>
const $=id=>document.getElementById(id);
// Front view of the wheel-arch tank (docs/tank/tank-drawing.jpg), mm, floor of the leg = 0.
// Water is drawn to the height the sender measures; the shape clips it.
function tankSvg(hmm, litres, cap, id) {
  const W = 1150, H = 695, XC = 590, R = 385, LEG = XC - R, BOX = H - R;
  const h = hmm == null ? null : Math.max(0, Math.min(H, hmm));
  const shape = 'M0,' + H + ' L' + LEG + ',' + H + ' A' + R + ',' + R + ' 0 0 1 ' + XC + ',' + BOX +
    ' L' + W + ',' + BOX + ' L' + W + ',0 L0,0 Z';
  let s = '<svg viewBox="-110 -70 1290 800" role="img" aria-label="Tank, water ' + (h == null ? 'unknown' : Math.round(h) + ' mm') +
    '" style="width:100%;height:auto;display:block;font:44px system-ui,sans-serif">' +
    '<defs><clipPath id="' + id + '"><path d="' + shape + '"/></clipPath></defs>' +
    '<path d="' + shape + '" style="fill:var(--tank-empty)"/>';
  if (h != null) {
    s += '<rect x="0" y="' + (H - h) + '" width="' + W + '" height="' + h + '" clip-path="url(#' + id + ')" style="fill:var(--water)"/>';
  }
  for (let t = 0; t <= 600; t += 100) {
    s += '<line x1="-18" x2="0" y1="' + (H - t) + '" y2="' + (H - t) + '" style="stroke:var(--tank-line);stroke-width:3"/>' +
      '<text x="-28" y="' + (H - t + 14) + '" text-anchor="end" style="fill:var(--tank-text)">' + t + '</text>';
  }
  s += '<path d="' + shape + '" style="fill:none;stroke:var(--tank-line);stroke-width:6;stroke-linejoin:round"/>';
  if (h != null) {
    const y = H - h, ly = Math.max(40, Math.min(y - 16, H - 20));
    s += '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '" clip-path="url(#' + id + ')" style="stroke:var(--tank-line);stroke-width:3;stroke-dasharray:14 10"/>' +
      '<text x="' + (W - 20) + '" y="' + ly + '" text-anchor="end" style="fill:var(--tank-text);font-weight:700">' +
      Math.round(h) + ' mm' + (litres == null ? '' : ' · ' + litres.toFixed(1) + ' L' + (cap ? ' of ' + Math.round(cap) + ' L' : '')) + '</text>';
  }
  s += '<text x="-28" y="-30" text-anchor="end" style="fill:var(--tank-text);font-size:36px">mm</text></svg>';
  return s;
}
let busy=false,fastOn=false,S=null,wview=null,scanRes=null,wpick=null;
function f1(x){return x==null?'–':x.toFixed(1)}
function esc(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c])}
function ago(t){return t<60?t+' s':t<3600?Math.round(t/60)+' min':(t/3600).toFixed(1)+' h'}
function tab(p){if(!p||!p.length)return'';let h='<table><tr><th>#</th><th>height mm</th><th>litres</th></tr>';p.forEach((x,i)=>h+='<tr><td>'+i+'</td><td>'+x[0].toFixed(1)+'</td><td>'+x[1].toFixed(1)+'</td></tr>');return h+'</table>'}
function openDlg(id){const d=$(id);d.querySelector('.dout').hidden=true;d.showModal()}
async function refresh(){
 if(busy)return;
 try{
  const s=await (await fetch('/api/status')).json();S=s;
  $('ver').textContent='v'+s.version;
  $('lit').textContent=f1(s.litres);$('cap').textContent=f1(s.capacity);$('lvl').textContent=f1(s.level);
  $('tank').innerHTML=tankSvg(s.height_mm,s.litres,s.capacity,'tc');
  $('h').textContent=f1(s.height_mm);$('z').textContent=f1(s.zero_mm);
  $('sig').textContent=s.signal==null?'–':s.signal.toFixed(3)+' '+s.signal_unit;
  let c='';
  if(s.fault)c+='<span class="chip bad">Sensor fault – check wiring</span>';
  if(s.sloshing)c+='<span class="chip">Water moving</span>';
  if(s.filling)c+='<span class="chip">Filling</span>';
  if(s.draining)c+='<span class="chip">Draining</span>';
  if(s.sim_mm!=null)c+='<span class="chip bad">SIMULATION: '+s.sim_mm.toFixed(0)+' mm, not the sensor (sim off)</span>';
  fastOn=s.fast_s>0;
  if(fastOn)c+='<span class="chip bad">Test mode: no smoothing, '+Math.ceil(s.fast_s/60)+' min left</span>';
  $('fastbtn').textContent=fastOn?'Back to normal smoothing':'Test mode: fast reading (15 min)';
  $('chips').innerHTML=c;
  $('tcard').style.display=s.temp_probe?'':'none';
  renderTemps(s.temps||[]);
  header(s);tanks(s);wifi(s);
  $('foot').textContent=(s.ap_on&&s.wifi_reason?'Own Wi-Fi on because: '+s.wifi_reason+' · ':'')+'Bluetooth '+s.mac+' · sensor id '+s.device_id+' · up '+ago(s.uptime_s);
 }catch(e){$('foot').textContent='No connection to the sensor…'}
}
// top right: Bluetooth light + when the Pi last checked in, Wi-Fi symbol + network
function header(s){
 const p=s.pi_ago_s,ok=s.ble_adv&&p>=0&&p<900;
 $('btd').style.background=ok?'var(--bt)':'var(--bad)';$('btd').className='dot'+(ok?' br':'');
 $('btt').textContent='Venus OS '+(p<0?'–':ago(p));
 $('btp').title='Bluetooth '+(s.ble_adv?'broadcasting':'NOT broadcasting')+' · Venus OS '+(p<0?'has not checked in yet':'checked in '+ago(p)+' ago')+(s.ble_conn?' · connected now':'');
 const w=$('wfp');
 w.style.color=s.sta_ip||s.ap_on?'var(--wf)':'var(--mut)';
 $('wft').textContent=s.sta_ip?s.sta_ssid+(s.ap_on?' + AP':''):s.ap_on?'AP on':s.wifi_mode==='van'&&s.sta_ssid?'joining…':'off';
 w.title=(s.sta_ip?'Connected to “'+s.sta_ssid+'” ('+s.sta_ip+')':'Not connected to a Wi-Fi network')+' · own network (AP) '+(s.ap_on?'on':'off');
}
// tank list, calibration dialog
let tkey='';
function tanks(s){
 const k=JSON.stringify(s.tanks)+s.tank;
 if(k!==tkey&&document.activeElement!==$('tsel')){tkey=k;
  $('tsel').innerHTML=(s.tanks||[]).map(t=>'<option value="'+t.n+'"'+(t.n===s.tank?' selected':'')+'>'+esc(t.name)+' · '+t.cap.toFixed(0)+' L'+(t.n?'':' (built-in)')+'</option>').join('')}
 $('trn').disabled=$('tdel').disabled=!s.tank;
 $('tbl').innerHTML=tab(s.table);
 const n=(s.cal_points||[]).length;
 $('calbtn').textContent=s.cal_active?'Continue calibration ('+n+' points)':'Calibrate the tank';
 $('calintro').hidden=!!s.cal_active;$('calrun').hidden=!s.cal_active;
 $('caltot').textContent=n?s.cal_points[n-1][1].toFixed(1):'0';$('calnp').textContent=n;
 $('calbox').innerHTML=tab(s.cal_points);
}
function tankRename(){const t=(S.tanks||[]).find(x=>x.n===S.tank);const v=prompt('New name for “'+(t?t.name:'')+'” (up to 32 characters)',t?t.name:'');if(v&&v.trim())cmd('tank rename '+S.tank+' '+v.trim())}
function tankDel(){const t=(S.tanks||[]).find(x=>x.n===S.tank);if(t&&confirm('Delete the tank “'+t.name+'”? The built-in tank is used then.'))cmd('tank delete '+S.tank)}
function addPt(){const v=parseFloat($('add').value);if(!(v>0)){alert('Type the litres you poured in this step');return}cmd('cal add '+v);$('add').value=''}
function calSave(){const v=$('calname').value.trim();cmd('cal save'+(v?' '+v:''));$('calname').value=''}
async function cmd(c,shown){
 if(!c)return'';busy=true;const lbl=shown||c,o=$('outp');let r='';o.textContent='> '+lbl+'\n…';
 const d=document.querySelector('dialog[open] .dout');if(d){d.hidden=false;d.textContent=o.textContent}
 document.querySelectorAll('button,select').forEach(b=>b.disabled=true);
 try{const x=await fetch('/api/cmd',{method:'POST',body:new URLSearchParams({c})});r=await x.text();o.textContent='> '+lbl+'\n'+r}
 catch(e){o.textContent='Failed: '+e}
 if(d)d.textContent=o.textContent;
 document.querySelectorAll('button,select').forEach(b=>b.disabled=false);busy=false;await refresh();return r;
}
let tempsKey='';
function renderTemps(t){
 $('tnote').textContent=t.length?'':'No probe found. Check the wiring (probe 1 data on GPIO5, probe 2 on GPIO6, 4.7 kΩ to 3V3 on each). The sensor looks again every 30 s.';
 const key=t.map(x=>x.addr+x.name).join();
 if(key!==tempsKey){tempsKey=key;
  $('temps').innerHTML=t.map(x=>'<div class="row" style="align-items:center"><div style="flex:1;min-width:0"><span class="mut">'+x.n+' · '+esc(x.name)+(x.pin!=null?' · GPIO'+x.pin:'')+'</span><b style="display:block;font-size:22px" id="tv'+x.n+'">–</b></div><button onclick="rename('+x.n+')">Rename</button></div>').join('');}
 t.forEach(x=>{const e=$('tv'+x.n);if(e)e.textContent=x.c==null?'no reading':x.c.toFixed(1)+' °C'});
}
function rename(n){const v=prompt('Name for temperature '+n+' (up to 32 characters)');if(v&&v.trim())cmd('name '+n+' '+v.trim().replace(/["\\]/g,''))}
async function setPw(){const p=$('pw').value;if(p.length<8||p.length>63){alert('8 to 63 characters');return}
 const r=await fetch('/api/password',{method:'POST',body:new URLSearchParams({p})});alert(await r.text())}
async function upd(){const f=$('fw').files[0];if(!f)return;const d=new FormData();d.append('fw',f);
 $('outp').textContent='Uploading '+f.name+' …';busy=true;
 try{const r=await fetch('/update',{method:'POST',body:d});$('outp').textContent=await r.text()}catch(e){$('outp').textContent='Failed: '+e}
 busy=false;setTimeout(refresh,8000)}
// Wi-Fi: mode switch, known networks (tap to connect), scan to add one
const WMODES=[['ap','AP only'],['van','Wi-Fi network'],['auto','Bluetooth only']];
const WHELP={ap:'Own network “Watertank” always on. Uses the most energy.',van:'Joins a network below; the page is then reachable in that network too. If it is not there and the Pi is silent, the own network comes on.',auto:'Saves energy: Wi-Fi off. The own network comes on by itself when the Pi has not been heard for 15 min, or for 30 min after pressing BOOT.'};
function viaSta(){return S&&S.sta_ip&&location.hostname===S.sta_ip}
function setMode(v){
 if(v==='van'){if(S.sta_ssid)cmd('wifi mode van');else{wview='van';wifi(S)}return}
 wview=null;
 if(viaSta()&&!confirm('You are on this page through “'+S.sta_ssid+'”. It will be gone from that network; use the own network Watertank instead. Continue?'))return;
 cmd('wifi mode '+v);
}
let wkey='';
function wifi(s){
 const cur=wview||(s.wifi_mode==='off'?'auto':s.wifi_mode);
 const k=JSON.stringify([cur,s.known,s.sta_ssid,s.sta_ip,s.wifi_mode,scanRes,wpick]);
 $('wstate').textContent=(s.sta_ip?'Connected to “'+s.sta_ssid+'”: http://'+s.sta_ip+'. ':s.wifi_mode==='van'&&s.sta_ssid?'Joining “'+s.sta_ssid+'”… ':'')+'Own network (AP) '+(s.ap_on?'on.':'off.');
 if(k===wkey||(document.activeElement&&document.activeElement.closest('#wnet input')))return;wkey=k;
 $('wmode').innerHTML=WMODES.map(([v,l])=>'<button class="'+(cur===v?'on':'')+'" onclick="setMode(\''+v+'\')">'+l+'</button>').join('');
 $('whelp').textContent=WHELP[cur]||'';
 $('wnet').hidden=cur!=='van';
 const kn=s.known||[];
 $('wknown').innerHTML=kn.length?'<div class="list">'+kn.map((n,i)=>{
   const sel=n===s.sta_ssid&&s.wifi_mode==='van',st=sel?(s.sta_ip?'<b style="color:var(--wf)">Connected</b>':'<span class="mut">joining…</span>'):'<button class="p" onclick="wjoin(S.known['+i+'])">Connect</button>';
   return '<div class="item"><span class="nm">'+esc(n)+'</span>'+st+'<button onclick="wforget('+i+')" title="Forget" aria-label="Forget '+esc(n)+'">✕</button></div>'}).join('')+'</div>'
  :'<div class="mut">None saved yet: scan and pick your network.</div>';
 if(scanRes==null){$('wscan').innerHTML='';return}
 const other=scanRes.filter(x=>kn.indexOf(x.s)<0);
 $('wscan').innerHTML=!scanRes.length?'<div class="mut">No networks found.</div>':!other.length?'<div class="mut">Only known networks around.</div>':'<div class="list">'+other.map(x=>{
   const i=scanRes.indexOf(x),bars=x.r>-60?'strong':x.r>-75?'good':'weak';
   return '<div class="item pick" onclick="if(wpick!=='+i+'){wpick='+i+';wkey=\'\';wifi(S);const e=$(\'wp'+i+'\');if(e)e.focus()}"><span class="nm">'+esc(x.s)+'</span><span class="mut">'+bars+' · '+x.r+' dBm'+(wpick===i?'':' ›')+'</span>'+
    (wpick===i?'<div class="row" style="width:100%;margin:4px 0 0"><input id="wp'+i+'" type="password" placeholder="password (empty if open)" autocomplete="off" onkeydown="if(event.key===\'Enter\')wjoin(scanRes['+i+'].s,this.value)"><button class="p" onclick="event.stopPropagation();wjoin(scanRes['+i+'].s,$(\'wp'+i+'\').value)">Connect</button></div>':'')+'</div>'}).join('')+'</div>';
}
async function wscan(){$('wscanb').textContent='Scanning… (a few seconds)';$('wscanb').disabled=true;
 try{scanRes=await (await fetch('/api/wifiscan')).json()}catch(e){scanRes=[]}
 $('wscanb').textContent='Scan again';$('wscanb').disabled=false;wpick=null;wkey='';wifi(S)}
async function wjoin(name,pw){
 if(!name){alert('Enter the network name');return}
 if(viaSta()&&name!==S.sta_ssid&&!confirm('You are on this page through “'+S.sta_ssid+'”. It moves to “'+name+'”. Continue?'))return;
 wview=null;wpick=null;
 await cmd('wifi join '+name+(pw==null?'':'\t'+pw),'wifi join '+name);
 $('hpass').value='';
}
function wforget(i){const n=S.known[i];if(confirm('Forget “'+n+'”?'))cmd('wifi forget '+n)}
async function dlogLoad(){try{$('dlog').textContent=await (await fetch('/api/dlog')).text();const p=$('dlog');p.scrollTop=p.scrollHeight}catch(e){$('dlog').textContent='Failed: '+e}}
async function dlogCopy(){const t=$('dlog').textContent;try{await navigator.clipboard.writeText(t);alert('Copied')}catch(e){const r=document.createRange();r.selectNodeContents($('dlog'));const s=getSelection();s.removeAllRanges();s.addRange(r);alert('Selected: copy it now (the browser blocked automatic copying)')}}
async function reboot(){if(!confirm('Restart the sensor? Settings and calibration are kept; it is back in about 10 s.'))return;
 await cmd('reboot');busy=true;$('foot').textContent='Restarting… the page reconnects by itself.';
 setTimeout(()=>{busy=false},9000)}
// terminal: list of commands, a tap puts one in the input line
const CMDS=[['status','reading and table in use'],['help','all commands with details'],['temps','temperature probes'],['name 1 Fresh water','name probe 1'],
 ['zero','tank empty: height becomes 0 mm'],['tank','list the tanks'],['tank use 0','use the built-in tank'],['cal start','calibration: start with the tank empty'],
 ['cal add 10','calibration: 10 L poured in'],['cal undo','calibration: remove the last point'],['cal show','calibration: points so far'],['cal save My tank','calibration: save as a tank and use it'],
 ['cal cancel','calibration: throw away'],['table','table in use, as code for the sketch'],['wifi','Wi-Fi state'],['wifi known','remembered networks'],
 ['wifi mode auto','Wi-Fi mode: auto | van | ap | off'],['fast','test mode: no smoothing for 15 min'],['fast off','normal smoothing'],['sim 50%','broadcast a fake level for 15 min'],
 ['sim off','back to the real sensor'],['reboot','restart the sensor'],['dlog','debug log'],['live','raw reading as JSON'],['info','state as JSON (what the Pi reads)'],['log','serial log on / off']];
function cmdsToggle(){const l=$('cmds');l.hidden=!l.hidden;$('cmdsb').textContent=l.hidden?'Show commands':'Hide commands';
 if(!l.innerHTML)l.innerHTML=CMDS.map((c,i)=>'<div class="item pick" onclick="$(\'con\').value=CMDS['+i+'][0];$(\'con\').focus()"><code class="nm" style="flex:none">'+esc(c[0])+'</code><span class="mut nm">'+esc(c[1])+'</span></div>').join('')}
// 4 updates a second in test mode, every 2 s otherwise
function loop(){refresh().finally(()=>setTimeout(loop,fastOn?250:2000))}
loop();
</script></body></html>)HTML";

static void jsonNum(String &j, const char *key, float v, int decimals = 1) {
  j += '"'; j += key; j += "\":";
  if (isnan(v) || isinf(v)) j += "null"; else j += String(v, decimals);
  j += ',';
}

static void jsonPoints(String &j, const char *key, const TankPoint *t, size_t n) {
  j += '"'; j += key; j += "\":[";
  for (size_t i = 0; i < n; i++) {
    if (i) j += ',';
    j += '['; j += String(t[i].mm, 1); j += ','; j += String(t[i].litres, 1); j += ']';
  }
  j += "],";
}


static void handleStatus() {
  lastWebUse = millis();
  float height = isnan(emaHeight) ? NAN : max(0.0f, emaHeight);
  float litres = isnan(height) ? NAN : heightToLitres(height);
  String j = "{";
  j += "\"version\":\"" FW_VERSION "\",";
  j += "\"device_id\":" + String(DEVICE_ID) + ",";
  j += "\"mac\":\"" + String(BLEDevice::getAddress().toString().c_str()) + "\",";
  j += "\"uptime_s\":" + String(millis() / 1000) + ",";
  j += "\"wifi_mode\":\"" + wifiMode + "\",\"pi_ago_s\":" + String(lastPiHeard ? (long)((millis() - lastPiHeard) / 1000) : -1L) + ",";
  j += "\"sta_ip\":\"" + staIp() + "\",\"ap_on\":" + String(apOn ? "true" : "false") + ",";
  j += "\"sta_ssid\":\"" + jsonEsc(staSsid) + "\",";
  j += "\"ble_adv\":" + String(advertising && advertising->isAdvertising() ? "true" : "false") + ",";
  j += "\"ble_conn\":" + String(linkServer ? linkServer->getConnectedCount() : 0) + ",";
  j += "\"ble_restarts\":" + String(advRestarts) + ",";
  j += "\"wifi_reason\":\"" + String(wifiReason) + "\",";
  if (simActive()) {
    jsonNum(j, "sim_mm", simHeight);
    j += "\"sim_ms\":" + String(millis() - simSince) + ",\"sim_left_s\":" + String((simUntil - millis()) / 1000) + ",";
  }
  jsonNum(j, "signal", lastSignal, 3);
  j += INPUT_CURRENT_LOOP ? "\"signal_unit\":\"mA\"," : "\"signal_unit\":\"V\",";
  jsonNum(j, "height_mm", height);
  jsonNum(j, "raw_height_mm", lastRawHeight);
  jsonNum(j, "zero_mm", zeroOffsetMm);
  j += "\"fast_s\":" + String(fastMode() ? (unsigned long)((fastUntil - millis()) / 1000) : 0UL) + ",";
  jsonNum(j, "litres", litres);
  jsonNum(j, "capacity", capacityL());
  jsonNum(j, "level", isnan(litres) ? NAN : litres / capacityL() * 100.0f);
  j += "\"temps\":[";
  bool first = true;
  for (uint8_t i = 0; i < MAX_TEMPS; i++) {
    if (!tempPresent[i]) continue;
    String nm = tempName[i];
    nm.replace("\\", "\\\\"); nm.replace("\"", "\\\"");
    if (!first) j += ',';
    first = false;
    j += "{\"n\":" + String(i + 1) + ",\"pin\":" + String(busPins[tempBus[i]]) + ",\"name\":\"" + nm + "\",\"addr\":\"" + addrHex(tempAddr[i]) + "\",\"c\":";
    j += isnan(tempC[i]) ? String("null") : String(tempC[i], 2);
    j += '}';
  }
  j += "],";
  j += String("\"temp_probe\":") + (USE_DS18B20 ? "true" : "false") + ",";
  j += String("\"fault\":") + (fault || isnan(emaHeight) ? "true" : "false") + ",";
  j += String("\"sloshing\":") + (sloshing ? "true" : "false") + ",";
  j += String("\"filling\":") + (filling ? "true" : "false") + ",";
  j += String("\"draining\":") + (draining ? "true" : "false") + ",";
  j += "\"tank\":" + String(activeTank + 1) + ",\"tanks\":[{\"n\":0,\"name\":\"" BUILTIN_TANK_NAME "\",\"cap\":" +
       String(DEFAULT_TABLE[sizeof(DEFAULT_TABLE) / sizeof(DEFAULT_TABLE[0]) - 1].litres, 1) + "}";
  for (int i = 0; i < MAX_TANKS; i++)
    if (tankNames[i].length()) j += ",{\"n\":" + String(i + 1) + ",\"name\":\"" + jsonEsc(tankNames[i]) + "\",\"cap\":" + String(tankCaps[i], 1) + "}";
  j += "],\"known\":[";
  bool firstK = true;
  for (uint8_t i = 0; i < MAX_KNOWN; i++)
    if (knownSsid[i].length()) { if (!firstK) j += ','; firstK = false; j += "\"" + jsonEsc(knownSsid[i]) + "\""; }
  j += "],";
  j += String("\"cal_active\":") + (calActive ? "true" : "false") + ",";
  jsonPoints(j, "cal_points", calPts, calActive ? calCount : 0);
  jsonPoints(j, "table", table, tablePoints);
  j.remove(j.length() - 1);  // trailing comma
  j += '}';
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "application/json", j);
}

// Run one serial command and send back what it printed
static void handleCmd() {
  lastWebUse = millis();
  StringOut reply;
  out = &reply;
  handleCommand(server.arg("c"));
  out = &Serial;
  if (reply.text.length() == 0) reply.text = "OK";
  Serial.print(reply.text);   // keep the serial monitor in the picture
  server.send(200, "text/plain; charset=utf-8", reply.text);
}

static bool staConnected() { return staOn && WiFi.status() == WL_CONNECTED; }
static String staIp() { return staConnected() ? WiFi.localIP().toString() : String(); }

static void startAp() {
  String ssid = String(AP_SSID_PREFIX) + DEVICE_ID;
  bool ok = WiFi.softAP(ssid.c_str(), apPassword.c_str(), AP_CHANNEL);
  if (ok) {
    dns.setErrorReplyCode(DNSReplyCode::NoError);
    dns.start(53, "*", WiFi.softAPIP());
  }
  Serial.printf("Own Wi-Fi %s '%s', page http://%s\n", ok ? "on:" : "FAILED to start",
                ssid.c_str(), WiFi.softAPIP().toString().c_str());
}

// Decide every second which Wi-Fi is on (see "Wi-Fi modes" at the top)
static void updateWifi() {
  static uint32_t last = 0;
  static bool mdnsUp = false;
  uint32_t now = millis();
  if (now - last < 1000) return;
  last = now;

  if (digitalRead(PIN_BOOT_BUTTON) == LOW) {
    if (!buttonUntil || (int32_t)(buttonUntil - now) < (int32_t)(BUTTON_AP_MS - 5000))
      Serial.println("BOOT pressed: own Wi-Fi on for 30 minutes.");
    buttonUntil = now + BUTTON_AP_MS;
    if (!buttonUntil) buttonUntil = 1;
  }
  bool button = buttonUntil && (int32_t)(buttonUntil - now) > 0;
  bool wantSta = wifiMode == "van" && staSsid.length() > 0;
  bool staUp = staConnected();
  // off: only the BOOT button switches the own Wi-Fi on (no start-up window, no fallback)
  bool wantAp = wifiMode == "off" ? button
              : wifiMode == "ap" || now < BOOT_AP_MS || button || (!piRecent() && !staUp);
  wifiReason = button ? "BOOT button pressed" : wifiMode == "ap" ? "set to always on"
             : wifiMode == "off" ? "" : now < BOOT_AP_MS ? "first 10 minutes after power-on"
             : !piRecent() && !staUp ? "the Pi has not checked in over Bluetooth for 15 minutes (fallback)" : "";
  // Phones rejoin a known network by themselves: a joined phone keeps the Wi-Fi on only
  // while the page is in use, else the Wi-Fi would never go off.
  bool phoneUsing = WiFi.softAPgetStationNum() > 0 && lastWebUse && now - lastWebUse < WEB_IDLE_MS;
  if (apOn && !wantAp && (phoneUsing || now - apOnSince < AP_MIN_ON_MS)) {
    wantAp = true;
    wifiReason = phoneUsing ? "a phone is using the page" : "only just switched on";
  }

  if (wantAp != apOn || wantSta != staOn || staDirty) {
    wifi_mode_t m = wantAp && wantSta ? WIFI_AP_STA : wantAp ? WIFI_AP : wantSta ? WIFI_STA : WIFI_OFF;
    if (!wantAp && apOn) { dns.stop(); WiFi.softAPdisconnect(true); Serial.println("Own Wi-Fi off (the Pi or the van Wi-Fi is there)."); dlog("own Wi-Fi (AP) off"); }
    if ((!wantSta || staDirty) && staOn) WiFi.disconnect(false);
    WiFi.mode(m);
    if (wantAp && !apOn) { startAp(); apOnSince = now; dlog("own Wi-Fi (AP) on: %s", wifiReason); }
    if (wantSta && (!staOn || staDirty)) {
      WiFi.setAutoReconnect(true);
      WiFi.begin(staSsid.c_str(), staPass.c_str());
      Serial.printf("Joining the van Wi-Fi '%s'\n", staSsid.c_str());
      dlog("joining Wi-Fi '%s'", staSsid.c_str());
    }
    apOn = wantAp; staOn = wantSta; staDirty = false;
    if (m == WIFI_OFF && mdnsUp) { MDNS.end(); mdnsUp = false; }
  }
  if (!mdnsUp && (apOn || staConnected()) && MDNS.begin(MDNS_NAME)) {
    MDNS.addService("http", "tcp", 80);
    mdnsUp = true;
  }
  static bool wasStaUp = false;
  if (staConnected() != wasStaUp) {
    wasStaUp = staConnected();
    if (wasStaUp) Serial.printf("Van Wi-Fi connected: http://%s\n", WiFi.localIP().toString().c_str());
    if (wasStaUp) dlog("Wi-Fi '%s' connected: %s, signal %d dBm", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());
    else dlog("Wi-Fi network lost");
  }
  webUp = apOn || staConnected();
}

static void handlePassword() {
  String p = server.arg("p");
  if (p.length() < 8 || p.length() > 63) {
    server.send(400, "text/plain", "The password needs 8 to 63 characters.");
    return;
  }
  prefs.begin("wifi", false);
  prefs.putString("appass", p);
  prefs.end();
  server.send(200, "text/plain", "Saved. The Wi-Fi restarts now: join it again with the new password. "
                                 "Enter the same password as 'Sender password' on the Venus OS page.");
  delay(500);
  apPassword = p;
  if (apOn) { dns.stop(); WiFi.softAPdisconnect(false); startAp(); }
}

// The full image for the browser flasher starts with the bootloader, which
// must never land in an app slot: an app's first segment loads at 0x3C......
static bool wrongImage = false;

static void handleUpdateDone() {
  bool ok = !Update.hasError() && !wrongImage;
  server.send(200, "text/plain", ok ? "Update OK. The sender restarts; reload the page in ~10 s."
              : wrongImage ? (fwBusy ? "Refused: the sensor is downloading an update over Wi-Fi right now. Try again in a minute."
                                        : "Wrong file: this is the FLASH-AT-0x0 file for the USB flasher. Upload WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin here. Nothing was changed.")
                           : String("Update failed: ") + Update.errorString());
  if (ok) { delay(500); ESP.restart(); }
}

static void handleUpdateUpload() {
  HTTPUpload &u = server.upload();
  if (u.status == UPLOAD_FILE_START) {
    Serial.printf("Firmware update: %s\n", u.filename.c_str());
    wrongImage = false;
    // The person at the sensor wins: an unfinished Bluetooth upload from the Pi is dropped
    // (the Pi then reports it as failed). A download over Wi-Fi (fwget) runs only for seconds.
    if (otaActive) { Update.abort(); otaActive = false; Serial.println("Bluetooth upload from the Pi dropped: uploading on the page instead."); }
    if (fwBusy) { wrongImage = true; Serial.println("Refused: the sensor is downloading an update over Wi-Fi."); return; }
    webOtaActive = true;
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
  } else if (u.status == UPLOAD_FILE_WRITE) {
    if (wrongImage) return;                // refused: never touch Update (a Pi update may be using it)
    if (u.totalSize == 0 && u.currentSize >= 28 && u.buf[27] != 0x3C) {   // first chunk: check the image
      wrongImage = true;
      Update.abort();
      Serial.println("Firmware update refused: not an app image (bootloader / full flash file?)");
      return;
    }
    if (Update.write(u.buf, u.currentSize) != u.currentSize) Update.printError(Serial);
  } else if (u.status == UPLOAD_FILE_END) {
    if (!wrongImage && !Update.end(true)) Update.printError(Serial);
    webOtaActive = false;
  } else if (u.status == UPLOAD_FILE_ABORTED) {
    if (!wrongImage) Update.abort();
    webOtaActive = false;
  }
}

// Download and install the firmware the Pi offers (fwget). Blocks for the few
// seconds it takes; Bluetooth keeps broadcasting the last reading meanwhile.
// The real reason of a failed download: HTTPUpdate aborts the update after an error and
// then reports only "Aborted", so translate its error code ourselves.
static String fwErrorText(int code) {
  switch (code) {
    case UPDATE_ERROR_STREAM: return "the download stalled (Wi-Fi too weak or busy)";
    case UPDATE_ERROR_WRITE: case UPDATE_ERROR_ERASE: case UPDATE_ERROR_READ: return "flash write failed";
    case UPDATE_ERROR_SPACE: case UPDATE_ERROR_SIZE: return "file too big for the update slot";
    case UPDATE_ERROR_MD5: return "file damaged (checksum)";
    case UPDATE_ERROR_MAGIC_BYTE: return "not a firmware file";
    default: return "download failed: " + httpUpdate.getLastErrorString();
  }
}

static void fwGet() {
  fwPending = false;
  fwBusy = true;
  Serial.printf("Firmware download from %s\n", fwUrl.c_str());
  // One radio for both: give Wi-Fi all of it while the file comes in. The Pi's Bluetooth
  // connection is dropped and broadcasting pauses (back after a failure; a success restarts).
  if (linkServer && linkServer->getConnectedCount()) linkServer->disconnect(linkServer->getConnId());
  if (advertising) advertising->stop();
  esp_coex_preference_set(ESP_COEX_PREFER_WIFI);
  dlog("Wi-Fi update: joining '%s'", fwSsid.length() ? fwSsid.c_str() : WiFi.SSID().c_str());
  if (!staConnected()) {
    WiFi.mode(apOn ? WIFI_AP_STA : WIFI_STA);
    WiFi.begin(fwSsid.c_str(), fwPass.c_str());
    for (uint32_t t0 = millis(); WiFi.status() != WL_CONNECTED && millis() - t0 < 25000; ) {
      updateLed();
      delay(50);
    }
  }
  if (WiFi.status() != WL_CONNECTED) {
    fwError = "could not join the Wi-Fi '" + fwSsid + "'";
  } else {
    WiFiClient client;
    httpUpdate.rebootOnUpdate(true);    // restarts by itself when the new firmware is in
    httpUpdate.onProgress([](int done, int total) {   // green/red while it loads
      ledWrite(((done >> 14) & 1) ? 0 : STATUS_LED_MAX, ((done >> 14) & 1) ? STATUS_LED_MAX : 0, 0);
    });
    client.setTimeout(20);
    dlog("Wi-Fi update: joined (signal %d dBm), downloading", WiFi.RSSI());
    t_httpUpdate_return r = httpUpdate.update(client, fwUrl);
    if (r == HTTP_UPDATE_NO_UPDATES) fwError = "the Pi had no file";
    else fwError = httpUpdate.getLastError() > 0 ? fwErrorText(httpUpdate.getLastError())
                                                 : "download failed: " + httpUpdate.getLastErrorString();
  }
  esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
  if (advertising) advertising->start();
  dlog("Wi-Fi update failed: %s", fwError.c_str());
  Serial.printf("Firmware download failed: %s\n", fwError.c_str());
  staDirty = true;                      // back to the Wi-Fi the mode asks for
  fwBusy = false;
}

// Networks around, for the Wi-Fi card (needs the station side for a moment)
static String wifiScanJson(int maxN) {
  if (!(WiFi.getMode() & WIFI_STA)) WiFi.enableSTA(true);
  int n = WiFi.scanNetworks();
  String j = "[";
  int shown = 0;
  for (int i = 0; i < n && shown < maxN; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0) continue;
    bool dup = false;
    for (int k = 0; k < i; k++) if (WiFi.SSID(k) == ssid) { dup = true; break; }
    if (dup) continue;
    if (shown++) j += ',';
    j += "{\"s\":\"" + jsonEsc(ssid) + "\",\"r\":" + String(WiFi.RSSI(i)) + "}";
  }
  j += "]";
  WiFi.scanDelete();
  staDirty = !staOn;                     // back to the Wi-Fi the mode asks for
  return j;
}

static void handleWifiScan() {
  lastWebUse = millis();
  server.send(200, "application/json", wifiScanJson(25));
}

static void webStart() {
  pinMode(PIN_BOOT_BUTTON, INPUT_PULLUP);
  updateWifi();                            // own Wi-Fi on: the first minutes after power-on

  server.on("/", HTTP_GET, []() { server.send_P(200, "text/html", PAGE); });
  server.on("/api/status", HTTP_GET, handleStatus);
  server.on("/api/cmd", HTTP_POST, handleCmd);
  server.on("/api/wifiscan", HTTP_GET, handleWifiScan);
  server.on("/api/dlog", HTTP_GET, []() { server.sendHeader("Cache-Control", "no-store"); server.send(200, "text/plain; charset=utf-8", dlogText()); });
  server.on("/api/password", HTTP_POST, handlePassword);
  server.on("/update", HTTP_POST, handleUpdateDone, handleUpdateUpload);
  // Phones test for internet with fixed addresses (captive.apple.com/hotspot-detect.html,
  // connectivitycheck.gstatic.com/generate_204, ...). The DNS server sends them here and
  // this redirect makes the phone show its "sign in to network" window with our page.
  server.onNotFound([]() {
    String host = server.hostHeader();
    String self = staConnected() && host == WiFi.localIP().toString() ? host : WiFi.softAPIP().toString();
    server.sendHeader("Location", "http://" + self + "/");
    server.send(302, "text/plain", "");
  });
  server.begin();
  Serial.printf("Web page: http://%s or http://%s.local (also opens by itself on most phones)\n",
                WiFi.softAPIP().toString().c_str(), MDNS_NAME);
  if (!webUp) Serial.println("Web page not reachable: the Wi-Fi network did not start (LED stays off).");
}

#else
static bool staConnected() { return false; }
static String staIp() { return String(); }
static String wifiScanJson(int) { return "[]"; }
#endif  // USE_WIFI_AP

// ------------------------------------------------------------ setup / loop

static const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power on";
    case ESP_RST_SW: return "restart (command or update)";
    case ESP_RST_PANIC: return "CRASH (panic)";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "CRASH (watchdog)";
    case ESP_RST_BROWNOUT: return "BROWNOUT (supply voltage dropped)";
    case ESP_RST_EXT: return "reset button";
    case ESP_RST_DEEPSLEEP: return "deep sleep";
    default: return "other";
  }
}

void setup() {
  setCpuFrequencyMhz(CPU_MHZ);
  dlog("start: firmware " FW_VERSION ", last reset: %s", resetReason());
  Serial.begin(115200);
  ledWrite(0, 0, 0);                    // off until the web page runs
  delay(500);
  loadCalibration();
  analogReadResolution(12);
  pinMode(PIN_PRESSURE, INPUT);

  for (uint8_t i = 0; i < MAX_TEMPS; i++) tempC[i] = NAN;
  findProbes();

  // Prime the filter so the first advertisement is already meaningful
  for (int i = 0; i < 8; i++) { sampleLevel(); delay(20); }
  updateTemperature();

  loadWifiSettings();                   // before Bluetooth: the password is its key too
  bleStart();
#if USE_WIFI_AP
  webStart();
#endif
  Serial.println("Type 'help' for calibration commands.");
}

void loop() {
  static uint32_t tSample = 0, tTrend = 0, tAdv = 0;
  if (advNow) { advNow = false; sampleLevel(); tAdv = millis() - ADV_UPDATE_MS; }   // a new simulated level: send now
  uint32_t now = millis();

  if (now - tSample >= SAMPLE_PERIOD_MS) { tSample = now; sampleLevel(); }
  if (now - tTrend >= TREND_PERIOD_MS)   { tTrend = now; updateTrend(); updateTemperature(); }
  if (now - tAdv >= ADV_UPDATE_MS) {
    tAdv = now;
    updateAdvertisement();
    if (advertising->isAdvertising()) lastAdvSent = now ? now : 1;   // flash only when it really goes out
  }
  // A connection stops the broadcast in the Bluetooth stack; the Pi needs it for the
  // level, so start it again at once, and check every 5 s that it is still running.
  static uint32_t tAdvCheck = 0;
  bool settling = linkConnected && now - linkConnectedAt < ADV_AFTER_CONNECT_MS;
  if (!settling && (advRestart || now - tAdvCheck >= 5000)) {
    tAdvCheck = now;
    if (advRestart || !advertising->isAdvertising()) {
      if (!advRestart) { advRestarts++; Serial.println("Bluetooth broadcast had stopped: started again."); dlog("BT broadcast had stopped: started again"); }
      advRestart = false;
      advertising->start();
    }
  }
  pollSerial();
#if USE_WIFI_AP
  if (fwPending) fwGet();
  updateWifi();
  if (apOn) dns.processNextRequest();
  server.handleClient();
#endif
  linkPoll();
  updateLed();

  delay(5);
}
