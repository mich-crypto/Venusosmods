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
#define USE_DS18B20          1

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

// ---- DS18B20 sanity
#define TEMP_MAX_FAILS       3      // consecutive bad reads before the value is dropped

// ======================================================== END CONFIGURATION

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
};
static const size_t PAYLOAD_LEN = 16;

// Active calibration (sketch defaults or loaded from flash)
#define MAX_POINTS 40
static TankPoint table[MAX_POINTS];
static size_t tablePoints = 0;
static float zeroOffsetMm = ZERO_OFFSET_MM;
static Preferences prefs;
static bool serialLog = SERIAL_LOG;

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
static float temperatureC = NAN;
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

static void updateTemperature() {
#if USE_DS18B20
  static bool pending = false;
  static uint8_t fails = 0;
  if (pending) {
    // getTempC() checks the scratchpad CRC and returns DEVICE_DISCONNECTED_C
    // for a missing probe or a corrupted read.
    float t = ds18b20.getTempCByIndex(0);
    bool bad = t == DEVICE_DISCONNECTED_C || t < -55.0f || t > 125.0f;
    // 85.0 C is the power-on value of the scratchpad: a conversion that never
    // ran (brown-out, bad contact). Only believe it if we were already close.
    if (t == 85.0f && (isnan(temperatureC) || fabsf(temperatureC - 85.0f) > 5.0f)) bad = true;
    if (!bad) {
      temperatureC = t;
      fails = 0;
    } else if (++fails >= TEMP_MAX_FAILS) {
      temperatureC = NAN;   // stop sending a stale value as if it were current
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

  if (serialLog) {
    Serial.printf("signal=%.3f height=%.1fmm litres=%.1f level=%.1f%% temp=%.1f "
                  "activity=%.1f%s%s%s%s\n",
                  lastSignal, height, litres, level, temperatureC, activity,
                  sloshing ? " SLOSH" : "", filling ? " FILLING" : "",
                  draining ? " DRAINING" : "", (flags & FLAG_FAULT) ? " FAULT" : "");
  }
}

// AD structure types (Bluetooth Core Spec Supplement)
static const uint8_t AD_FLAGS = 0x01, AD_SHORT_NAME = 0x08, AD_MANUFACTURER = 0xFF;

static void updateAdvertisement() {
  // Raw advertising data, 31 bytes max:
  //   flags (3) + manufacturer data (4 + 16) + short name (2 + 3) = 28
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
static bool measureSteadyHeight(float &out, uint32_t ms = 5000) {
  if (sloshing) {
    Serial.println("Water is still moving (SLOSH) - wait until it settles and try again.");
    return false;
  }
  double sum = 0;
  uint32_t n = 0;
  float lo = 1e9f, hi = -1e9f;
  for (uint32_t t0 = millis(); millis() - t0 < ms; ) {
    sampleLevel();
    if (fault) { Serial.println("Sensor fault - check wiring."); return false; }
    sum += lastRawHeight;
    n++;
    lo = min(lo, lastRawHeight);
    hi = max(hi, lastRawHeight);
    delay(SAMPLE_PERIOD_MS);
  }
  out = (float)(sum / n);
  Serial.printf("  measured %.1f mm (spread %.1f mm over %u bursts)\n", out, hi - lo, (unsigned)n);
  if (hi - lo > 4 * SLOSH_EXIT_MM) {
    Serial.println("  spread is large - the level was still changing. Wait and repeat.");
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
  Serial.println("  #   height mm   litres");
  for (size_t i = 0; i < n; i++)
    Serial.printf("  %-3u %9.1f %8.1f\n", (unsigned)i, t[i].mm, t[i].litres);
}

static void printAsCode() {
  Serial.println("// paste into DEFAULT_TABLE in the sketch:");
  for (size_t i = 0; i < tablePoints; i++)
    Serial.printf("  { %6.1ff, %6.1ff },\n", table[i].mm, table[i].litres);
  Serial.printf("// zero offset: #define ZERO_OFFSET_MM %.1ff\n", zeroOffsetMm);
}

static void calAppend(float mm, float litres) {
  if (calCount >= MAX_POINTS) { Serial.println("Table full."); return; }
  if (calCount > 0 && mm <= calPts[calCount - 1].mm) {
    Serial.printf("Height %.1f mm is not above the previous point (%.1f mm) - not added. "
                  "Add more water per step, or check the sensor.\n", mm, calPts[calCount - 1].mm);
    return;
  }
  calPts[calCount++] = { mm, litres };
  calTotal = litres;
  Serial.printf("Point %u: %.1f mm = %.1f L\n", (unsigned)(calCount - 1), mm, litres);
}

static void printHelp() {
  Serial.println(
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
    Serial.printf("Periodic log %s\n", serialLog ? "on" : "off");
  } else if (cmd == "status") {
    Serial.printf("signal=%.3f raw height=%.1f mm filtered=%.1f mm zero offset=%.1f mm "
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
    Serial.printf("Zero offset %.1f mm stored.\n", zeroOffsetMm);
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
      Serial.println("Now pour in a known amount, wait ~30 s, then: cal add <litres>");
    } else if (sub == "add" || sub == "point") {
      if (!calActive) { calActive = true; calCount = 0; calTotal = 0; }
      if (sub == "add") {
        float l = arg.toFloat();
        if (l <= 0) { Serial.println("Usage: cal add <litres poured in this step>"); return; }
        float h;
        if (!measureSteadyHeight(h)) return;
        calAppend(h, calTotal + l);
      } else {
        int sp3 = arg.indexOf(' ');
        if (sp3 < 0) { Serial.println("Usage: cal point <mm> <total litres>"); return; }
        calAppend(arg.substring(0, sp3).toFloat(), arg.substring(sp3 + 1).toFloat());
      }
    } else if (sub == "undo") {
      if (calCount) { calCount--; calTotal = calCount ? calPts[calCount - 1].litres : 0; }
      printTable(calPts, calCount);
    } else if (sub == "show") {
      printTable(calPts, calCount);
      Serial.printf("  total so far: %.1f L\n", calTotal);
    } else if (sub == "save") {
      if (!tableValid(calPts, calCount)) {
        Serial.println("Need at least 2 points with rising height and litres.");
        return;
      }
      memcpy(table, calPts, calCount * sizeof(TankPoint));
      tablePoints = calCount;
      saveTable();
      calActive = false;
      Serial.printf("Saved %u points, capacity %.1f L.\n", (unsigned)tablePoints, capacityL());
      printAsCode();
    } else if (sub == "cancel") {
      calActive = false;
      calCount = 0;
      Serial.println("Calibration discarded.");
    } else {
      Serial.println("cal start | add <L> | point <mm> <L> | undo | show | save | cancel");
    }
  } else if (cmd == "table") {
    printAsCode();
  } else if (cmd == "reset") {
    prefs.begin("tank", false);
    prefs.clear();
    prefs.end();
    loadDefaults();
    emaHeight = NAN;
    Serial.println("Stored calibration erased, using sketch defaults.");
  } else {
    Serial.printf("Unknown command '%s' - type help\n", cmd.c_str());
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

// ------------------------------------------------------------ setup / loop

void setup() {
  Serial.begin(115200);
  delay(500);
  loadCalibration();
  analogReadResolution(12);
  pinMode(PIN_PRESSURE, INPUT);

#if USE_DS18B20
  ds18b20.begin();
  ds18b20.setResolution(11);            // 0.125 C, 375 ms conversion
  ds18b20.setWaitForConversion(false);
#endif

  // Prime the filter so the first advertisement is already meaningful
  for (int i = 0; i < 8; i++) { sampleLevel(); delay(20); }
  updateTemperature();

  bleStart();
  Serial.println("Type 'help' for calibration commands.");
}

void loop() {
  static uint32_t tSample = 0, tTrend = 0, tAdv = 0;
  uint32_t now = millis();

  if (now - tSample >= SAMPLE_PERIOD_MS) { tSample = now; sampleLevel(); }
  if (now - tTrend >= TREND_PERIOD_MS)   { tTrend = now; updateTrend(); updateTemperature(); }
  if (now - tAdv >= ADV_UPDATE_MS)       { tAdv = now; updateAdvertisement(); }
  pollSerial();

  delay(5);
}
