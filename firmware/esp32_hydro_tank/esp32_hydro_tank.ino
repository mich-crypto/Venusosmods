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
#include <esp_arduino_version.h>
#include <Preferences.h>

// ============================================================ CONFIGURATION

#define DEVICE_ID            1      // must match device_id in config.ini on the Pi

// ---- Pins (ESP32-S3: use GPIO1..GPIO10 = ADC1; ADC2 shares hardware with the radio)
#define PIN_PRESSURE         4
#define PIN_ONEWIRE          5
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

// ---- Status LED: the board's RGB LED (WS2812) breathes blue while the
//      Wi-Fi page is up. DevKitC-1 v1.0 and most clones: GPIO48, v1.1: GPIO38.
#define STATUS_LED_PIN       48     // -1 = no LED
#define STATUS_LED_MAX       40     // peak brightness 0..255 (these LEDs are bright)
#define BREATH_PERIOD_MS     3000   // one breath in and out

#define FW_VERSION           "2.2"

// ---- DS18B20 sanity
#define TEMP_MAX_FAILS       3      // consecutive bad reads before the value is dropped

// ======================================================== END CONFIGURATION

#if USE_WIFI_AP
#include <WiFi.h>
#include <WebServer.h>
#include <Update.h>
#endif

#if USE_DS18B20
#include <OneWire.h>
#include <DallasTemperature.h>
static OneWire oneWire(PIN_ONEWIRE);
static DallasTemperature ds18b20(&oneWire);
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
static uint8_t tempCount = 0;
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
  lastSignal = mvToSignal(readBurstMedianMv());
  fault = lastSignal < SIGNAL_FAULT_LOW || lastSignal > SIGNAL_FAULT_HIGH;
  if (fault) return;  // keep the last good EMA, flag the fault

  float h = signalToHeight(lastSignal);
  lastRawHeight = h;
  if (isnan(emaHeight)) {   // first sample after boot / fault
    emaHeight = h;
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
    tempName[i] = i < tempCount ? prefs.getString(nameKey(tempAddr[i]).c_str(), "") : String();
  prefs.end();
  for (uint8_t i = 0; i < MAX_TEMPS; i++)
    if (tempName[i].length() == 0) tempName[i] = "Temperature " + String(i + 1);
}

static bool setTempName(uint8_t i, String name) {
  name.trim();
  if (i >= tempCount || name.length() == 0 || name.length() > 32) return false;
  for (size_t k = 0; k < name.length(); k++)
    if ((uint8_t)name[k] < 32 || name[k] == '"' || name[k] == '\\') return false;
  prefs.begin("temps", false);
  prefs.putString(nameKey(tempAddr[i]).c_str(), name);
  prefs.end();
  tempName[i] = name;
  return true;
}

// Find the probes on the bus and number them by address (stable order)
static void findProbes() {
#if USE_DS18B20
  ds18b20.begin();
  uint8_t n = 0, a[8];
  for (uint8_t i = 0; i < ds18b20.getDeviceCount() && n < MAX_TEMPS; i++)
    if (ds18b20.getAddress(a, i)) memcpy(tempAddr[n++], a, 8);
  for (uint8_t i = 1; i < n; i++)              // insertion sort by address
    for (uint8_t j = i; j > 0 && memcmp(tempAddr[j - 1], tempAddr[j], 8) > 0; j--) {
      uint8_t t[8]; memcpy(t, tempAddr[j], 8); memcpy(tempAddr[j], tempAddr[j - 1], 8); memcpy(tempAddr[j - 1], t, 8);
    }
  if (n != tempCount) Serial.printf("Temperature probes found: %u\n", n);
  tempCount = n;
  for (uint8_t i = 0; i < MAX_TEMPS; i++) { tempC[i] = NAN; tempFails[i] = 0; }
  ds18b20.setResolution(11);            // 0.125 C, 375 ms conversion
  ds18b20.setWaitForConversion(false);
  loadTempNames();
#endif
}

static void updateTemperature() {
#if USE_DS18B20
  static bool pending = false;
  static uint32_t lastSearch = 0;
  // A probe plugged in later, or one that went missing: look again every 30 s
  bool missing = tempCount < MAX_TEMPS;
  for (uint8_t i = 0; i < tempCount; i++) missing |= isnan(tempC[i]) && tempFails[i] >= TEMP_MAX_FAILS;
  if (missing && millis() - lastSearch > 30000) {
    lastSearch = millis();
    float keep[MAX_TEMPS]; memcpy(keep, tempC, sizeof keep);
    uint8_t before[MAX_TEMPS][8]; memcpy(before, tempAddr, sizeof before);
    uint8_t had = tempCount;
    findProbes();
    if (tempCount == had && memcmp(before, tempAddr, sizeof before) == 0) memcpy(tempC, keep, sizeof keep);
    pending = false;
  }
  if (pending) {
    for (uint8_t i = 0; i < tempCount; i++) {
      // getTempC() checks the scratchpad CRC and returns DEVICE_DISCONNECTED_C
      // for a missing probe or a corrupted read.
      float t = ds18b20.getTempC(tempAddr[i]);
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
  ds18b20.requestTemperatures();   // non-blocking, read on next call
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
                  "activity=%.1f%s%s%s%s\n",
                  lastSignal, height, litres, level, temperatureC, temp2Valid ? tempC[1] : NAN, activity,
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

static void bleStart() {
  BLEDevice::init(ADV_NAME);
  advertising = BLEDevice::getAdvertising();

  uint16_t interval = ADV_INTERVAL_MS * 1000 / 625;  // 0.625 ms units
  advertising->setMinInterval(interval);
  advertising->setMaxInterval(interval + interval / 5);
  advertising->setScanResponse(false);
#if defined(CONFIG_NIMBLE_ENABLED)
  advertising->setAdvertisementType(BLE_GAP_CONN_MODE_NON);  // broadcast only
#else
  advertising->setAdvertisementType(ADV_TYPE_NONCONN_IND);
#endif

  updateAdvertisement();
  advertising->start();
  Serial.printf("Advertising as %s, MAC %s\n", ADV_NAME,
                BLEDevice::getAddress().toString().c_str());
}

// ------------------------------------------------------------- calibration
//
// Stored in flash (NVS namespace "tank"):
//   zero  float   offset added to the measured height
//   n     uint    number of table points
//   pts   blob    TankPoint[n]

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

static void loadCalibration() {
  loadDefaults();
  prefs.begin("tank", true);
  zeroOffsetMm = prefs.getFloat("zero", ZERO_OFFSET_MM);
  size_t n = prefs.getUInt("n", 0);
  TankPoint stored[MAX_POINTS];
  if (n >= 2 && n <= MAX_POINTS &&
      prefs.getBytes("pts", stored, n * sizeof(TankPoint)) == n * sizeof(TankPoint) &&
      tableValid(stored, n)) {
    memcpy(table, stored, n * sizeof(TankPoint));
    tablePoints = n;
    Serial.printf("Loaded calibration: %u points, %.1f L, zero offset %.1f mm\n",
                  (unsigned)n, capacityL(), zeroOffsetMm);
  } else {
    Serial.println("Using the default tank table from the sketch (no calibration stored)");
  }
  prefs.end();
}

static void saveZero() {
  prefs.begin("tank", false);
  prefs.putFloat("zero", zeroOffsetMm);
  prefs.end();
}

static void saveTable() {
  prefs.begin("tank", false);
  prefs.putUInt("n", tablePoints);
  prefs.putBytes("pts", table, tablePoints * sizeof(TankPoint));
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
    "  zero                tank EMPTY: make the current height read 0 mm\n"
    "  zero <mm>           set the zero offset by hand\n"
    "  cal start           tank EMPTY: start a new table with 0 L at the current height\n"
    "  cal add <litres>    after pouring <litres> in and waiting ~30 s: record a point\n"
    "  cal point <mm> <L>  add a point by hand (heights include the zero offset)\n"
    "  cal undo            remove the last point\n"
    "  cal show            show the table being measured\n"
    "  cal save            use and store the new table (last point = 100 %)\n"
    "  cal cancel          discard the table being measured\n"
    "  table               print the active table as code for the sketch\n"
    "  temps               list the temperature probes\n"
    "  name <n> <text>     name temperature probe n (1, 2)\n"
    "  reset               forget stored calibration, use the sketch defaults");
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
      if (!tableValid(calPts, calCount)) {
        out->println("Need at least 2 points with rising height and litres.");
        return;
      }
      memcpy(table, calPts, calCount * sizeof(TankPoint));
      tablePoints = calCount;
      saveTable();
      calActive = false;
      out->printf("Saved %u points, capacity %.1f L.\n", (unsigned)tablePoints, capacityL());
      printAsCode();
    } else if (sub == "cancel") {
      calActive = false;
      calCount = 0;
      out->println("Calibration discarded.");
    } else {
      out->println("cal start | add <L> | point <mm> <L> | undo | show | save | cancel");
    }
  } else if (cmd == "temps") {
    if (tempCount == 0) out->println("No temperature probes found (wiring, 4.7k pull-up, USE_DS18B20).");
    for (uint8_t i = 0; i < tempCount; i++)
      out->printf("  %u  %-24s %6.2f C  address %s\n", i + 1, tempName[i].c_str(), tempC[i], addrHex(tempAddr[i]).c_str());
  } else if (cmd == "name") {
    int sp = rest.indexOf(' ');
    int i = (sp < 0 ? rest : rest.substring(0, sp)).toInt();
    if (sp < 0 || i < 1 || !setTempName(i - 1, rest.substring(sp + 1)))
      out->printf("Usage: name <1..%u> <text up to 32 characters> (probe must be found)\n", (unsigned)tempCount);
    else
      out->printf("Probe %d is now '%s'.\n", i, tempName[i - 1].c_str());
  } else if (cmd == "table") {
    printAsCode();
  } else if (cmd == "reset") {
    prefs.begin("tank", false);
    prefs.clear();
    prefs.end();
    loadDefaults();
    emaHeight = NAN;
    out->println("Stored calibration erased, using sketch defaults.");
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

static void ledWrite(uint8_t r, uint8_t g, uint8_t b) {
#if STATUS_LED_PIN >= 0
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  rgbLedWrite(STATUS_LED_PIN, r, g, b);
#else
  neopixelWrite(STATUS_LED_PIN, r, g, b);
#endif
#endif
}

// Blue breathing while the page is reachable, off otherwise. Squaring the
// cosine wave makes the fade look even to the eye.
static void updateLed() {
  static uint32_t last = 0;
  static int lastLevel = -1;
  uint32_t now = millis();
  if (now - last < 20) return;              // 50 updates a second is smooth enough
  last = now;
  int level = 0;
  if (webUp) {
    float x = (1.0f - cosf(2.0f * PI * (now % BREATH_PERIOD_MS) / BREATH_PERIOD_MS)) / 2.0f;
    level = lroundf(x * x * STATUS_LED_MAX);
  }
  if (level != lastLevel) { ledWrite(0, 0, level); lastLevel = level; }
}

// ------------------------------------------------------- own Wi-Fi + web page
//
// The sender opens its own Wi-Fi network, so the page works with the Pi off.
// Calibration on this page changes the sender's own table, the same as the
// serial commands (the page simply runs those commands and shows the reply).

#if USE_WIFI_AP

static WebServer server(80);
static String apPassword;

static const char PAGE[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Water tank</title>
<style>
:root{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd;--card:#f6f6f6;--acc:#111;--accfg:#fff;--bad:#c62828;--ok:#2e7d32;--water:#3b82d6;--tank-empty:#fff;--tank-line:#222;--tank-text:#444}
@media(prefers-color-scheme:dark){:root{--bg:#000;--fg:#eee;--mut:#999;--line:#333;--card:#111;--acc:#eee;--accfg:#000;--bad:#ef5350;--ok:#66bb6a;--water:#3b82d6;--tank-empty:#000;--tank-line:#ddd;--tank-text:#bbb}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 -apple-system,system-ui,sans-serif}
main{max-width:560px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 12px}h2{font-size:16px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.big{font-size:44px;font-weight:700;line-height:1}.mut{color:var(--mut);font-size:14px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px}.grid b{display:block;font-size:18px}
.chip{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:1px 8px;font-size:13px;margin:6px 4px 0 0}
.chip.bad{color:var(--bad);border-color:var(--bad)}
button,input{font:inherit;border-radius:8px;border:1px solid var(--line);padding:9px 12px;background:var(--bg);color:var(--fg)}
button{cursor:pointer}button.p{background:var(--acc);color:var(--accfg);border-color:var(--acc)}button:disabled{opacity:.5}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.row input{flex:1;min-width:0}
pre{white-space:pre-wrap;font:13px/1.35 ui-monospace,Menlo,monospace;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;max-height:260px;overflow:auto;margin:8px 0 0}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:right;padding:2px 6px;border-bottom:1px solid var(--line)}
details summary{cursor:pointer;font-weight:600}
</style></head><body><main>
<h1>Water tank <span class="mut" id="ver"></span></h1>

<div class="card">
 <div class="big"><span id="lit">–</span> L</div>
 <div class="mut">of <span id="cap">–</span> L · <span id="lvl">–</span> %</div>
 <div id="tank" style="margin:12px 0"></div>
 <div class="grid">
  <div><span class="mut">Water height</span><b><span id="h">–</span> mm</b></div>
  <div><span class="mut">Sensor signal</span><b id="sig">–</b></div>
  <div><span class="mut">Zero offset</span><b><span id="z">–</span> mm</b></div>
 </div>
 <div id="chips"></div>
</div>

<div class="card" id="tcard" style="display:none">
 <h2>Temperatures</h2>
 <div id="temps"></div>
 <div class="mut" id="tnote"></div>
</div>

<div class="card">
 <h2>1. Zero (tank empty)</h2>
 <div class="mut">Park level, empty the tank, wait until the water is calm, then press the button. The height then reads 0 mm.</div>
 <div class="row"><button class="p" onclick="cmd('zero')">Tank is empty: set zero</button></div>
</div>

<div class="card">
 <h2>2. Measure the tank (optional)</h2>
 <div class="mut">Only needed if the litres don't match. Start with the tank empty, pour in a known amount, wait ~30 s, type how much you poured in this step and press Record. Repeat until full, then Save.</div>
 <div class="row"><button onclick="cmd('cal start')">Start (tank empty)</button></div>
 <div class="row"><input id="add" type="number" inputmode="decimal" placeholder="litres poured in this step"><button class="p" onclick="addPt()">Record</button></div>
 <div class="row"><button onclick="cmd('cal undo')">Undo last</button><button class="p" onclick="cmd('cal save')">Save table</button><button onclick="cmd('cal cancel')">Cancel</button></div>
 <div id="calbox"></div>
</div>

<div class="card">
 <h2>Reply</h2>
 <pre id="outp">Measurements take about 5 seconds.</pre>
</div>

<div class="card"><details><summary>Active tank table</summary><div id="tbl"></div>
 <div class="row"><button onclick="if(confirm('Forget the stored calibration and use the built-in table?'))cmd('reset')">Back to built-in table</button></div></details></div>

<div class="card"><details><summary>Console (serial commands)</summary>
 <div class="row"><input id="con" placeholder="e.g. status, help, cal point 300 15"><button onclick="cmd(document.getElementById('con').value)">Send</button></div></details></div>

<div class="card"><details><summary>Wi-Fi password</summary>
 <div class="mut">8 to 63 characters. You'll have to join the network again with the new password.</div>
 <div class="row"><input id="pw" type="text" autocomplete="off" placeholder="new password"><button onclick="setPw()">Save</button></div></details></div>

<div class="card"><details><summary>Firmware update</summary>
 <div class="mut">Choose the <b>WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin</b> file (not the FLASH-AT-0x0 one). Your calibration is kept.</div>
 <div class="row"><input id="fw" type="file" accept=".bin"><button onclick="upd()">Upload</button></div></details></div>

<div class="mut" id="foot"></div>
</main><script>
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
  s += '<path d="' + shape + '" style="fill:none;stroke:var(--tank-line);stroke-width:6;stroke-linejoin:round"/>' +
    '<circle cx="60" cy="' + (H - 20) + '" r="16" style="fill:var(--tank-line)"><title>sensor</title></circle>' +
    '<text x="' + (XC + 60) + '" y="' + (H - 40) + '" style="fill:var(--tank-text);font-size:40px">wheel arch</text>';
  if (h != null) {
    const y = H - h, ly = Math.max(40, Math.min(y - 16, H - 20));
    s += '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '" clip-path="url(#' + id + ')" style="stroke:var(--tank-line);stroke-width:3;stroke-dasharray:14 10"/>' +
      '<text x="' + (W - 20) + '" y="' + ly + '" text-anchor="end" style="fill:var(--tank-text);font-weight:700">' +
      Math.round(h) + ' mm' + (litres == null ? '' : ' · ' + litres.toFixed(1) + ' L' + (cap ? ' of ' + Math.round(cap) + ' L' : '')) + '</text>';
  }
  s += '<text x="-28" y="-30" text-anchor="end" style="fill:var(--tank-text);font-size:36px">mm</text></svg>';
  return s;
}
let busy=false;
function f1(x){return x==null?'–':x.toFixed(1)}
async function refresh(){
 if(busy)return;
 try{
  const s=await (await fetch('/api/status')).json();
  $('ver').textContent='v'+s.version;
  $('lit').textContent=f1(s.litres);$('cap').textContent=f1(s.capacity);$('lvl').textContent=f1(s.level);
  $('tank').innerHTML=tankSvg(s.height_mm,s.litres,s.capacity,'tc');
  $('h').textContent=f1(s.height_mm);$('z').textContent=f1(s.zero_mm);
  $('tcard').style.display=s.temp_probe?'':'none';
  renderTemps(s.temps||[]);
  $('sig').textContent=s.signal==null?'–':s.signal.toFixed(3)+' '+s.signal_unit;
  let c='';
  if(s.fault)c+='<span class="chip bad">Sensor fault – check wiring</span>';
  if(s.sloshing)c+='<span class="chip">Water moving</span>';
  if(s.filling)c+='<span class="chip">Filling</span>';
  if(s.draining)c+='<span class="chip">Draining</span>';
  c+='<span class="chip">'+(s.custom_table?'Your calibration':'Built-in table')+'</span>';
  $('chips').innerHTML=c;
  $('tbl').innerHTML=tab(s.table);
  $('calbox').innerHTML=s.cal_active?'<p class="mut">Measured so far ('+s.cal_points.length+' points):</p>'+tab(s.cal_points):'';
  $('foot').textContent='Bluetooth '+s.mac+' · sender id '+s.device_id+' · up '+Math.round(s.uptime_s/60)+' min';
 }catch(e){$('foot').textContent='No connection to the sender…'}
}
function tab(p){if(!p||!p.length)return'';let h='<table><tr><th>#</th><th>height mm</th><th>litres</th></tr>';p.forEach((x,i)=>h+='<tr><td>'+i+'</td><td>'+x[0].toFixed(1)+'</td><td>'+x[1].toFixed(1)+'</td></tr>');return h+'</table>'}
async function cmd(c){
 if(!c)return;busy=true;$('outp').textContent='> '+c+'\n…';
 document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const r=await fetch('/api/cmd',{method:'POST',body:new URLSearchParams({c})});$('outp').textContent='> '+c+'\n'+await r.text()}
 catch(e){$('outp').textContent='Failed: '+e}
 document.querySelectorAll('button').forEach(b=>b.disabled=false);busy=false;refresh();
}
let tempsKey='';
function renderTemps(t){
 $('tnote').textContent=t.length?'Warm one probe in your hand to see which is which.':'No probe found. Check the wiring (data on GPIO5, 4.7 kΩ to 3V3). The sender looks again every 30 s.';
 const key=t.map(x=>x.addr+x.name).join();
 if(key!==tempsKey){tempsKey=key;
  $('temps').innerHTML=t.map(x=>'<div class="row" style="align-items:center"><div style="flex:1;min-width:0"><span class="mut">'+x.n+' · '+esc(x.name)+'</span><b style="display:block;font-size:22px" id="tv'+x.n+'">–</b></div><button onclick="rename('+x.n+')">Rename</button></div>').join('');}
 t.forEach(x=>{const e=$('tv'+x.n);if(e)e.textContent=x.c==null?'no reading':x.c.toFixed(1)+' °C'});
}
function esc(v){return String(v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c])}
function rename(n){const v=prompt('Name for temperature '+n+' (up to 32 characters)');if(v&&v.trim())cmd('name '+n+' '+v.trim().replace(/["\\]/g,''))}
function addPt(){const v=parseFloat($('add').value);if(!(v>0)){alert('Type the litres you poured in this step');return}cmd('cal add '+v);$('add').value=''}
async function setPw(){const p=$('pw').value;if(p.length<8||p.length>63){alert('8 to 63 characters');return}
 const r=await fetch('/api/password',{method:'POST',body:new URLSearchParams({p})});alert(await r.text())}
async function upd(){const f=$('fw').files[0];if(!f)return;const d=new FormData();d.append('fw',f);
 $('outp').textContent='Uploading '+f.name+' …';busy=true;
 try{const r=await fetch('/update',{method:'POST',body:d});$('outp').textContent=await r.text()}catch(e){$('outp').textContent='Failed: '+e}
 busy=false;setTimeout(refresh,8000)}
refresh();setInterval(refresh,2000);
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

static bool customTable() {
  prefs.begin("tank", true);
  bool c = prefs.getUInt("n", 0) >= 2;
  prefs.end();
  return c;
}

static void handleStatus() {
  float height = isnan(emaHeight) ? NAN : max(0.0f, emaHeight);
  float litres = isnan(height) ? NAN : heightToLitres(height);
  String j = "{";
  j += "\"version\":\"" FW_VERSION "\",";
  j += "\"device_id\":" + String(DEVICE_ID) + ",";
  j += "\"mac\":\"" + String(BLEDevice::getAddress().toString().c_str()) + "\",";
  j += "\"uptime_s\":" + String(millis() / 1000) + ",";
  jsonNum(j, "signal", lastSignal, 3);
  j += INPUT_CURRENT_LOOP ? "\"signal_unit\":\"mA\"," : "\"signal_unit\":\"V\",";
  jsonNum(j, "height_mm", height);
  jsonNum(j, "raw_height_mm", lastRawHeight);
  jsonNum(j, "zero_mm", zeroOffsetMm);
  jsonNum(j, "litres", litres);
  jsonNum(j, "capacity", capacityL());
  jsonNum(j, "level", isnan(litres) ? NAN : litres / capacityL() * 100.0f);
  j += "\"temps\":[";
  for (uint8_t i = 0; i < tempCount; i++) {
    String nm = tempName[i];
    nm.replace("\\", "\\\\"); nm.replace("\"", "\\\"");
    if (i) j += ',';
    j += "{\"n\":" + String(i + 1) + ",\"name\":\"" + nm + "\",\"addr\":\"" + addrHex(tempAddr[i]) + "\",\"c\":";
    j += isnan(tempC[i]) ? String("null") : String(tempC[i], 2);
    j += '}';
  }
  j += "],";
  j += String("\"temp_probe\":") + (USE_DS18B20 ? "true" : "false") + ",";
  j += String("\"fault\":") + (fault || isnan(emaHeight) ? "true" : "false") + ",";
  j += String("\"sloshing\":") + (sloshing ? "true" : "false") + ",";
  j += String("\"filling\":") + (filling ? "true" : "false") + ",";
  j += String("\"draining\":") + (draining ? "true" : "false") + ",";
  j += String("\"custom_table\":") + (customTable() ? "true" : "false") + ",";
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
  StringOut reply;
  out = &reply;
  handleCommand(server.arg("c"));
  out = &Serial;
  if (reply.text.length() == 0) reply.text = "OK";
  Serial.print(reply.text);   // keep the serial monitor in the picture
  server.send(200, "text/plain; charset=utf-8", reply.text);
}

static void startAp() {
  String ssid = String(AP_SSID_PREFIX) + DEVICE_ID;
  WiFi.mode(WIFI_AP);
  // Modem sleep must stay on while Bluetooth runs too (the default).
  bool ok = WiFi.softAP(ssid.c_str(), apPassword.c_str(), AP_CHANNEL);
  webUp = ok;
  Serial.printf("Wi-Fi %s '%s', page http://%s\n", ok ? "network" : "FAILED to start",
                ssid.c_str(), WiFi.softAPIP().toString().c_str());
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
  server.send(200, "text/plain", "Saved. The Wi-Fi restarts now: join it again with the new password.");
  delay(500);
  apPassword = p;
  webUp = false;
  WiFi.softAPdisconnect(true);
  startAp();
}

// The full image for the browser flasher starts with the bootloader, which
// must never land in an app slot: an app's first segment loads at 0x3C......
static bool wrongImage = false;

static void handleUpdateDone() {
  bool ok = !Update.hasError() && !wrongImage;
  server.send(200, "text/plain", ok ? "Update OK. The sender restarts; reload the page in ~10 s."
              : wrongImage ? "Wrong file: this is the FLASH-AT-0x0 file for the USB flasher. Upload WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin here. Nothing was changed."
                           : String("Update failed: ") + Update.errorString());
  if (ok) { delay(500); ESP.restart(); }
}

static void handleUpdateUpload() {
  HTTPUpload &u = server.upload();
  if (u.status == UPLOAD_FILE_START) {
    Serial.printf("Firmware update: %s\n", u.filename.c_str());
    wrongImage = false;
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
  } else if (u.status == UPLOAD_FILE_WRITE) {
    if (u.totalSize == 0 && u.currentSize >= 28 && u.buf[27] != 0x3C) {   // first chunk: check the image
      wrongImage = true;
      Update.abort();
      Serial.println("Firmware update refused: not an app image (bootloader / full flash file?)");
    }
    if (wrongImage) return;
    if (Update.write(u.buf, u.currentSize) != u.currentSize) Update.printError(Serial);
  } else if (u.status == UPLOAD_FILE_END) {
    if (!wrongImage && !Update.end(true)) Update.printError(Serial);
  } else if (u.status == UPLOAD_FILE_ABORTED) {
    Update.abort();
  }
}

static void webStart() {
  prefs.begin("wifi", true);
  apPassword = prefs.getString("appass", AP_PASSWORD_DEFAULT);
  prefs.end();
  if (apPassword.length() < 8) apPassword = AP_PASSWORD_DEFAULT;
  startAp();

  server.on("/", HTTP_GET, []() { server.send_P(200, "text/html", PAGE); });
  server.on("/api/status", HTTP_GET, handleStatus);
  server.on("/api/cmd", HTTP_POST, handleCmd);
  server.on("/api/password", HTTP_POST, handlePassword);
  server.on("/update", HTTP_POST, handleUpdateDone, handleUpdateUpload);
  server.onNotFound([]() { server.sendHeader("Location", "/"); server.send(302); });
  server.begin();
  if (!webUp) Serial.println("Web page not reachable: the Wi-Fi network did not start (LED stays off).");
}

#endif  // USE_WIFI_AP

// ------------------------------------------------------------ setup / loop

void setup() {
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

  bleStart();
#if USE_WIFI_AP
  webStart();
#endif
  Serial.println("Type 'help' for calibration commands.");
}

void loop() {
  static uint32_t tSample = 0, tTrend = 0, tAdv = 0;
  uint32_t now = millis();

  if (now - tSample >= SAMPLE_PERIOD_MS) { tSample = now; sampleLevel(); }
  if (now - tTrend >= TREND_PERIOD_MS)   { tTrend = now; updateTrend(); updateTemperature(); }
  if (now - tAdv >= ADV_UPDATE_MS)       { tAdv = now; updateAdvertisement(); }
  pollSerial();
#if USE_WIFI_AP
  server.handleClient();
#endif
  updateLed();

  delay(5);
}
