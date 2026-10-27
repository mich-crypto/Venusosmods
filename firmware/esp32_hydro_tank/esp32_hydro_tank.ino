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
 */

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEAdvertising.h>
#include <esp_arduino_version.h>

// ============================================================ CONFIGURATION

#define DEVICE_ID            1      // must match device_id in config.ini on the Pi

// ---- Pins (ESP32-S3: only GPIO1..GPIO10 are ADC1, ADC2 is unusable with BLE)
#define PIN_PRESSURE         4
#define PIN_ONEWIRE          5
#define USE_DS18B20          1

// ---- Input stage. Pick ONE:
//  INPUT_CURRENT_LOOP 1: 4-20 mA transmitter, shunt resistor from pin to GND.
//                        120 R gives 0.48 V .. 2.4 V, headroom for fault
//                        currents. Signal unit = mA (then use 4.0 / 20.0
//                        below, fault limits ~3.0 / 22.0).
//  INPUT_CURRENT_LOOP 0: voltage output transmitter (0.5-4.5 V, 0-5 V ...)
//                        through a divider R1 (top) / R2 (bottom).
//                        DIVIDER_RATIO = (R1 + R2) / R2. Signal unit = V.
//                        Keep the pin below ~3.1 V: 4.5 V with 10k/20k = 3.0 V.
#define INPUT_CURRENT_LOOP   0
#define SHUNT_OHMS           120.0f
#define DIVIDER_RATIO        1.5f

// ---- Transmitter datasheet values (V or mA, see above)
#define SIGNAL_AT_ZERO       0.5f   // output at 0 pressure
#define SIGNAL_AT_FULL       4.5f   // output at full scale
#define SENSOR_RANGE_MM      1000.0f// full scale in mm of water (1 m H2O = 9.81 kPa)
#define SIGNAL_FAULT_LOW     0.2f   // below this: open wire / dead sensor
#define SIGNAL_FAULT_HIGH    4.9f   // above this: short / overpressure

// ---- Calibration: height of the sensor's membrane above the tank bottom.
//      With the tank empty, read "height" from the serial log and adjust.
#define ZERO_OFFSET_MM       0.0f

// ---- Tank shape: water height (mm) -> volume (litres), ascending.
//      Measure it once: fill the tank in 5-10 L steps and note the height.
//      The last entry defines the capacity (100 %).
struct TankPoint { float mm; float litres; };
static const TankPoint TANK_TABLE[] = {
  {   0.0f,   0.0f },
  {  40.0f,   5.0f },
  { 100.0f,  15.0f },
  { 200.0f,  35.0f },
  { 300.0f,  55.0f },
  { 380.0f,  70.0f },
  { 420.0f,  75.0f },
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

#define SERIAL_DEBUG         1

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

static const size_t TANK_POINTS = sizeof(TANK_TABLE) / sizeof(TANK_TABLE[0]);
static const float CAPACITY_L = TANK_TABLE[TANK_POINTS - 1].litres;

// State
static float emaHeight = NAN;
static float activity = 0.0f;
static bool sloshing = false;
static bool fault = false;
static float lastSignal = 0.0f;
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
  if (mm <= TANK_TABLE[0].mm) return TANK_TABLE[0].litres;
  for (size_t i = 1; i < TANK_POINTS; i++) {
    if (mm <= TANK_TABLE[i].mm) {
      const TankPoint &a = TANK_TABLE[i - 1], &b = TANK_TABLE[i];
      return a.litres + (b.litres - a.litres) * (mm - a.mm) / (b.mm - a.mm);
    }
  }
  return CAPACITY_L;
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
  return h + ZERO_OFFSET_MM;
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
  if (pending) {
    float t = ds18b20.getTempCByIndex(0);
    temperatureC = (t == DEVICE_DISCONNECTED_C) ? NAN : t;
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
  float level = CAPACITY_L > 0 ? litres / CAPACITY_L * 100.0f : 0.0f;
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
  put16(p + 10, (uint16_t)lroundf(CAPACITY_L * 10.0f));
  put16(p + 12, (uint16_t)lroundf(height));
  put16(p + 14, (uint16_t)(int16_t)(tempValid ? lroundf(temperatureC * 100.0f) : 0));

#if SERIAL_DEBUG
  Serial.printf("signal=%.3f height=%.1fmm litres=%.1f level=%.1f%% temp=%.1f "
                "activity=%.1f%s%s%s%s\n",
                lastSignal, height, litres, level, temperatureC, activity,
                sloshing ? " SLOSH" : "", filling ? " FILLING" : "",
                draining ? " DRAINING" : "", (flags & FLAG_FAULT) ? " FAULT" : "");
#endif
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
#if SERIAL_DEBUG
  Serial.printf("Advertising as %s, MAC %s\n", ADV_NAME,
                BLEDevice::getAddress().toString().c_str());
#endif
}

// ------------------------------------------------------------ setup / loop

void setup() {
#if SERIAL_DEBUG
  Serial.begin(115200);
  delay(500);
#endif
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
}

void loop() {
  static uint32_t tSample = 0, tTrend = 0, tAdv = 0;
  uint32_t now = millis();

  if (now - tSample >= SAMPLE_PERIOD_MS) { tSample = now; sampleLevel(); }
  if (now - tTrend >= TREND_PERIOD_MS)   { tTrend = now; updateTrend(); updateTemperature(); }
  if (now - tAdv >= ADV_UPDATE_MS)       { tAdv = now; updateAdvertisement(); }

  delay(5);
}
