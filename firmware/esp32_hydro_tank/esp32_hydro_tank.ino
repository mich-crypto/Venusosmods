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
 *
 * Updates over the air: set OTA_KEY below (or type "ota key <secret>" in the
 * Serial Monitor) and put the same key in config.ini (ota_key). From then on a
 * new firmware (.bin) is flashed from the settings page on the Pi over
 * Bluetooth; no cable needed. See docs/GUIDE.md, "Firmware updates over the air".
 *
 * iPhone app (ios/WaterSensor.swiftpm): set CONSOLE_PIN below (or type
 * "pin 123456" in the Serial Monitor). The app then logs in with that PIN
 * (iOS asks for it once) and gets this serial console over Bluetooth:
 * status, zero, calibration, everything listed by "help".
 */

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEAdvertising.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <BLESecurity.h>
#include <Update.h>
#include <mbedtls/md.h>
#include <esp_random.h>
#include <esp_arduino_version.h>
#include <Preferences.h>

// ============================================================ CONFIGURATION

#define DEVICE_ID            1      // must match device_id in config.ini on the Pi
#define FW_VERSION           "1.1.0"// shown on the settings page; raise it when you change the sketch

// ---- Over-the-air updates over Bluetooth (settings page on the Pi -> Water tank -> Update firmware).
//      "" = off: the sender only broadcasts, nobody can connect to it.
//      Any other text = on, and only someone who knows this key can flash it. Put the same key
//      in config.ini on the Pi ([esp32_hydro] ota_key). 12+ random characters, no spaces.
//      A key set with the serial command "ota key <secret>" is stored in flash and wins.
#define OTA_KEY              ""

// ---- Console over Bluetooth for the iPhone app. 0 = off. Otherwise a 6-digit PIN (100000..999999):
//      the phone has to pair with it once, and the link is encrypted from then on.
//      A PIN set with the serial command "pin <6 digits>" is stored in flash and wins.
#define CONSOLE_PIN          0

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

// The Pi looks for this text in a .bin before it flashes it: is it this sketch, which version,
// which sender id. Keep the format.
#define STR_(x) #x
#define STR(x) STR_(x)
static const char FW_MARKER[] __attribute__((used)) = "VHT-FW " FW_VERSION " id=" STR(DEVICE_ID) " ;";

// Active calibration (sketch defaults or loaded from flash)
#define MAX_POINTS 40
static TankPoint table[MAX_POINTS];
static size_t tablePoints = 0;
static float zeroOffsetMm = ZERO_OFFSET_MM;
static Preferences prefs;
static bool serialLog = SERIAL_LOG;

static float capacityL() { return table[tablePoints - 1].litres; }

// Console output goes to the USB serial port and, while a phone is logged in, to the app.
// Only used from loop() (the Bluetooth task prints to Serial directly), so no locking.
static bool consoleOn = false;                 // console service running
static uint8_t conBuf[2048];                   // waiting to be sent to the phone
static size_t conHead = 0, conLen = 0;

class TeePrint : public Print {
 public:
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *p, size_t n) override {
    Serial.write(p, n);
    if (consoleOn) {
      for (size_t i = 0; i < n; i++) {
        if (conLen == sizeof(conBuf)) { conHead = (conHead + 1) % sizeof(conBuf); conLen--; }  // drop the oldest
        conBuf[(conHead + conLen) % sizeof(conBuf)] = p[i];
        conLen++;
      }
    }
    return n;
  }
};
static TeePrint con;

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
    con.printf("signal=%.3f height=%.1fmm litres=%.1f level=%.1f%% temp=%.1f "
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

// ------------------------------------------------------- updates over the air
//
// GATT service, only when an OTA key is set. Protocol (keep in sync with
// data/shared_drivers/esp32_ota.py):
//
//   control characteristic (write + notify), text:
//     Pi  "info"                       -> "info fw=<version> id=<device id> nonce=<32 hex> free=<bytes>"
//     Pi  "begin <size> <md5> <auth>"  -> "ok begin" | "err auth" | "err begin <reason>"
//          auth = hex HMAC-SHA256(key, "<nonce> <size> <md5>"); each nonce allows one try
//     Pi  "end"                        -> "ok end" (then restarts into the new firmware)
//                                       | "err <reason>"
//     Pi  "abort"                      -> "ok abort"
//   data characteristic (write with response): <offset uint32 LE><image bytes>, in order.
//     A wrong offset or a flash error is reported as "err ..." on the control characteristic.
//
// The image is written to the other app partition (default partition schemes have two) and
// checked against its MD5 before the boot partition is switched. A failed or interrupted
// update leaves the running firmware as it is.

static const char *OTA_SERVICE_UUID = "8e7f0001-5648-4f54-a1c3-5f2d7c3a9b10";
static const char *OTA_CTRL_UUID    = "8e7f0002-5648-4f54-a1c3-5f2d7c3a9b10";
static const char *OTA_DATA_UUID    = "8e7f0003-5648-4f54-a1c3-5f2d7c3a9b10";

static String otaKey;
static BLECharacteristic *otaCtrl = nullptr;
static uint8_t otaNonce[16];
static bool otaActive = false, otaFailed = false;
static uint32_t otaSize = 0, otaWritten = 0;
static volatile uint32_t otaRestartAt = 0;   // millis() of the planned restart, 0 = none

static void otaNewNonce() { esp_fill_random(otaNonce, sizeof(otaNonce)); }

static String hexOf(const uint8_t *p, size_t n) {
  static const char *digits = "0123456789abcdef";
  String s;
  s.reserve(n * 2);
  for (size_t i = 0; i < n; i++) { s += digits[p[i] >> 4]; s += digits[p[i] & 0x0F]; }
  return s;
}

static void otaReply(const String &text) {
  Serial.printf("OTA: %s\n", text.c_str());
  if (!otaCtrl) return;
  otaCtrl->setValue((uint8_t *)text.c_str(), text.length());
  otaCtrl->notify();
}

static bool otaAuthOk(const String &msg, String given) {
  uint8_t mac[32];
  const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (mbedtls_md_hmac(md, (const uint8_t *)otaKey.c_str(), otaKey.length(),
                      (const uint8_t *)msg.c_str(), msg.length(), mac) != 0) return false;
  String expect = hexOf(mac, sizeof(mac));
  given.toLowerCase();
  if (given.length() != expect.length()) return false;
  uint8_t diff = 0;                                   // constant time compare
  for (size_t i = 0; i < expect.length(); i++) diff |= (uint8_t)(given[i] ^ expect[i]);
  return diff == 0;
}

static void otaStop() {
  if (otaActive) Update.abort();
  otaActive = false;
  otaFailed = false;
}

static void otaControl(String line) {
  line.trim();
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String rest = sp < 0 ? String() : line.substring(sp + 1);

  if (cmd == "info") {
    otaReply(String("info fw=") + FW_VERSION + " id=" + DEVICE_ID + " nonce=" + hexOf(otaNonce, sizeof(otaNonce)) +
             " free=" + String((uint32_t)ESP.getFreeSketchSpace()));
  } else if (cmd == "begin") {
    int a = rest.indexOf(' '), b = a < 0 ? -1 : rest.indexOf(' ', a + 1);
    if (a < 0 || b < 0) { otaReply("err usage: begin <size> <md5> <auth>"); return; }
    String sizeText = rest.substring(0, a), md5 = rest.substring(a + 1, b), auth = rest.substring(b + 1);
    bool ok = otaAuthOk(hexOf(otaNonce, sizeof(otaNonce)) + " " + sizeText + " " + md5, auth);
    otaNewNonce();                                    // one try per nonce: no replay, no guessing
    if (!ok) { otaReply("err auth"); return; }
    otaStop();
    long size = sizeText.toInt();
    if (size <= 0 || md5.length() != 32) { otaReply("err begin bad size or md5"); return; }
    if (!Update.begin((size_t)size, U_FLASH)) { otaReply(String("err begin ") + Update.errorString()); return; }
    Update.setMD5(md5.c_str());
    otaActive = true;
    otaSize = (uint32_t)size;
    otaWritten = 0;
    otaReply("ok begin");
  } else if (cmd == "end") {
    if (!otaActive) { otaReply("err not started"); return; }
    if (otaFailed) { otaStop(); otaReply("err failed earlier"); return; }
    if (otaWritten != otaSize) { otaReply("err incomplete " + String(otaWritten)); return; }
    if (!Update.end()) {                              // checks the MD5, then switches the boot partition
      String why = Update.errorString();
      otaStop();
      otaReply("err " + why);
      return;
    }
    otaActive = false;
    otaReply("ok end");
    otaRestartAt = millis() + 1500;                   // let the reply go out first
    if (!otaRestartAt) otaRestartAt = 1;
  } else if (cmd == "abort") {
    otaStop();
    otaReply("ok abort");
  } else {
    otaReply("err unknown command");
  }
}

static void otaData(const uint8_t *p, size_t n) {
  if (!otaActive || otaFailed || n < 5) return;
  uint32_t offset = p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
  if (offset != otaWritten) {
    otaFailed = true;
    otaReply("err offset " + String(otaWritten));
    return;
  }
  const uint8_t *data = p + 4;
  size_t len = n - 4;
  if (otaWritten == 0) {
    // ESP image header: magic 0xE9 at 0, chip id (uint16) at 12
    bool bad = data[0] != 0xE9;
#ifdef CONFIG_IDF_FIRMWARE_CHIP_ID
    if (len >= 14 && (data[12] | (data[13] << 8)) != CONFIG_IDF_FIRMWARE_CHIP_ID) bad = true;
#endif
    if (bad) { otaFailed = true; otaReply("err not a firmware for this chip"); return; }
  }
  if (otaWritten + len > otaSize || Update.write((uint8_t *)data, len) != len) {
    otaFailed = true;
    otaReply(String("err write ") + Update.errorString());
    return;
  }
  otaWritten += len;
}

class OtaCtrlCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    String v = c->getValue();
    otaControl(v);
#else
    std::string v = c->getValue();
    otaControl(String(v.c_str()));
#endif
  }
};

class OtaDataCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    // Runs in the Bluetooth task; the write response goes out when this returns,
    // which is the flow control for the Pi.
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    String v = c->getValue();                         // keeps the length, zero bytes included
    otaData((const uint8_t *)v.c_str(), v.length());
#else
    std::string v = c->getValue();
    otaData((const uint8_t *)v.data(), v.length());
#endif
  }
};

// ---------------------------------------------------- console for the iPhone app
//
// Nordic UART Service, the usual "serial port over Bluetooth" (other terminal apps such as
// Bluefruit Connect or nRF Toolbox work too). Only when a console PIN is set, and only for a
// paired (bonded, encrypted) phone: iOS asks for the PIN once.
//   RX (write)  one command per write, as typed in the Serial Monitor
//   TX (notify) everything the console prints

static const char *CON_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e";
static const char *CON_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e";
static const char *CON_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e";
static const size_t CON_CHUNK = 160;          // per notification; iOS uses an MTU of 185 or more

static uint32_t consolePin = CONSOLE_PIN;
static BLECharacteristic *conTx = nullptr;
static BLE2902 *conTxCccd = nullptr;
static char conRxLine[128];
static volatile bool conRxPending = false;
static volatile uint8_t bleClients = 0;

static void handleCommand(String line);

class ConRxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    // Bluetooth task: only hand the line over, loop() runs it
    if (conRxPending) return;                          // one command at a time
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    String v = c->getValue();
#else
    std::string v = c->getValue();
#endif
    size_t n = min((size_t)v.length(), sizeof(conRxLine) - 1);
    memcpy(conRxLine, v.c_str(), n);
    conRxLine[n] = 0;
    conRxPending = true;
  }
};

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *s) override {
    bleClients++;
    otaNewNonce();
    BLEDevice::startAdvertising();                    // keep broadcasting the level while connected
  }
  void onDisconnect(BLEServer *s) override {
    if (bleClients) bleClients--;
    if (otaActive) { Serial.println("OTA: connection lost, update dropped"); otaStop(); }
    BLEDevice::startAdvertising();
  }
};

static void loadBleSettings() {
  prefs.begin("ota", true);
  otaKey = prefs.getString("key", OTA_KEY);
  prefs.end();
  prefs.begin("ble", true);
  consolePin = prefs.getUInt("pin", CONSOLE_PIN);
  prefs.end();
  if (consolePin && (consolePin < 100000 || consolePin > 999999)) consolePin = 0;
}

static void otaStart(BLEServer *server) {
  otaNewNonce();
  BLEService *service = server->createService(OTA_SERVICE_UUID);
  otaCtrl = service->createCharacteristic(OTA_CTRL_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_NOTIFY);
  otaCtrl->setCallbacks(new OtaCtrlCallbacks());
#if !defined(CONFIG_NIMBLE_ENABLED)
  otaCtrl->addDescriptor(new BLE2902());              // NimBLE adds it by itself
#endif
  BLECharacteristic *data = service->createCharacteristic(OTA_DATA_UUID, BLECharacteristic::PROPERTY_WRITE);
  data->setCallbacks(new OtaDataCallbacks());
  service->start();
}

#if !defined(CONFIG_NIMBLE_ENABLED)
// Bonded phones are remembered; a new PIN should lock them out again.
static void forgetPhones() {
  int n = esp_ble_get_bond_device_num();
  if (n <= 0) return;
  esp_ble_bond_dev_t *list = (esp_ble_bond_dev_t *)malloc(sizeof(esp_ble_bond_dev_t) * n);
  if (!list) return;
  esp_ble_get_bond_device_list(&n, list);
  for (int i = 0; i < n; i++) esp_ble_remove_bond_device(list[i].bd_addr);
  free(list);
}

static void consoleStart(BLEServer *server) {
  BLESecurity *security = new BLESecurity();
  security->setStaticPIN(consolePin);                // passkey pairing, bonding, MITM protection
  BLEService *service = server->createService(CON_SERVICE_UUID);
  BLECharacteristic *rx = service->createCharacteristic(CON_RX_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rx->setAccessPermissions(ESP_GATT_PERM_WRITE_ENC_MITM);
  rx->setCallbacks(new ConRxCallbacks());
  conTx = service->createCharacteristic(CON_TX_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  conTx->setAccessPermissions(ESP_GATT_PERM_READ_ENC_MITM);
  conTxCccd = new BLE2902();
  // subscribing needs the pairing too: this is where iOS asks for the PIN
  conTxCccd->setAccessPermissions(ESP_GATT_PERM_READ_ENC_MITM | ESP_GATT_PERM_WRITE_ENC_MITM);
  conTx->addDescriptor(conTxCccd);
  service->start();
  consoleOn = true;
}
#else
static void forgetPhones() {}
static void consoleStart(BLEServer *) {
  con.println("The Bluetooth console needs the Bluedroid stack; it is off with NimBLE.");
}
#endif

static void consoleLoop() {
  if (!consoleOn) return;
  if (conRxPending) {
    String line(conRxLine);
    conRxPending = false;
    line.trim();
    if (line.length()) {
      con.printf("> %s\n", line.c_str());
      handleCommand(line);
    }
  }
  bool listening = bleClients > 0 && conTxCccd && conTxCccd->getNotifications();
  if (!listening) { conLen = 0; return; }            // nobody to send to: drop it
  for (int i = 0; i < 4 && conLen; i++) {            // a few notifications per pass
    uint8_t chunk[CON_CHUNK];
    size_t n = min(conLen, CON_CHUNK);
    for (size_t k = 0; k < n; k++) chunk[k] = conBuf[(conHead + k) % sizeof(conBuf)];
    conHead = (conHead + n) % sizeof(conBuf);
    conLen -= n;
    conTx->setValue(chunk, n);
    conTx->notify();
  }
}

static void otaLoop() {
  if (otaRestartAt && (int32_t)(millis() - otaRestartAt) >= 0) {
    Serial.println("OTA: restarting into the new firmware");
    Serial.flush();
    ESP.restart();
  }
}

static void bleStart() {
  BLEDevice::init(ADV_NAME);
  bool ota = otaKey.length() > 0, console = consolePin != 0;
  bool connectable = ota || console;
  if (connectable) {
    BLEDevice::setMTU(517);                           // bigger chunks, faster update
    BLEServer *server = BLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());
    if (ota) otaStart(server);
    if (console) consoleStart(server);
  }
  advertising = BLEDevice::getAdvertising();

  uint16_t interval = ADV_INTERVAL_MS * 1000 / 625;  // 0.625 ms units
  advertising->setMinInterval(interval);
  advertising->setMaxInterval(interval + interval / 5);
  advertising->setScanResponse(false);
  // Broadcast only, unless updates over the air or the console are on: then one may connect
#if defined(CONFIG_NIMBLE_ENABLED)
  advertising->setAdvertisementType(connectable ? BLE_GAP_CONN_MODE_UND : BLE_GAP_CONN_MODE_NON);
#else
  advertising->setAdvertisementType(connectable ? ADV_TYPE_IND : ADV_TYPE_NONCONN_IND);
#endif

  updateAdvertisement();
  advertising->start();
  con.printf("Advertising as %s, MAC %s, updates over the air %s, iPhone console %s\n", ADV_NAME,
             BLEDevice::getAddress().toString().c_str(), ota ? "on" : "off (no OTA key)",
             console ? "on" : "off (no PIN)");
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
    con.printf("Loaded calibration: %u points, %.1f L, zero offset %.1f mm\n",
                  (unsigned)n, capacityL(), zeroOffsetMm);
  } else {
    con.println("Using the default tank table from the sketch (no calibration stored)");
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
    con.println("Water is still moving (SLOSH) - wait until it settles and try again.");
    return false;
  }
  double sum = 0;
  uint32_t n = 0;
  float lo = 1e9f, hi = -1e9f;
  for (uint32_t t0 = millis(); millis() - t0 < ms; ) {
    sampleLevel();
    if (fault) { con.println("Sensor fault - check wiring."); return false; }
    sum += lastRawHeight;
    n++;
    lo = min(lo, lastRawHeight);
    hi = max(hi, lastRawHeight);
    delay(SAMPLE_PERIOD_MS);
  }
  out = (float)(sum / n);
  con.printf("  measured %.1f mm (spread %.1f mm over %u bursts)\n", out, hi - lo, (unsigned)n);
  if (hi - lo > 4 * SLOSH_EXIT_MM) {
    con.println("  spread is large - the level was still changing. Wait and repeat.");
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
  con.println("  #   height mm   litres");
  for (size_t i = 0; i < n; i++)
    con.printf("  %-3u %9.1f %8.1f\n", (unsigned)i, t[i].mm, t[i].litres);
}

static void printAsCode() {
  con.println("// paste into DEFAULT_TABLE in the sketch:");
  for (size_t i = 0; i < tablePoints; i++)
    con.printf("  { %6.1ff, %6.1ff },\n", table[i].mm, table[i].litres);
  con.printf("// zero offset: #define ZERO_OFFSET_MM %.1ff\n", zeroOffsetMm);
}

static void calAppend(float mm, float litres) {
  if (calCount >= MAX_POINTS) { con.println("Table full."); return; }
  if (calCount > 0 && mm <= calPts[calCount - 1].mm) {
    con.printf("Height %.1f mm is not above the previous point (%.1f mm) - not added. "
                  "Add more water per step, or check the sensor.\n", mm, calPts[calCount - 1].mm);
    return;
  }
  calPts[calCount++] = { mm, litres };
  calTotal = litres;
  con.printf("Point %u: %.1f mm = %.1f L\n", (unsigned)(calCount - 1), mm, litres);
}

static void printHelp() {
  con.println(
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
    "  reset               forget stored calibration, use the sketch defaults\n"
    "  version             firmware version\n"
    "  ota                 are updates over the air on?\n"
    "  ota key <secret>    switch them on with this key (same as ota_key on the Pi), restarts\n"
    "  ota off             switch them off (back to OTA_KEY from the sketch), restarts\n"
    "  pin                 is the iPhone console on?\n"
    "  pin <6 digits>      switch it on with this PIN; paired phones must pair again, restarts\n"
    "  pin off             switch it off (back to CONSOLE_PIN from the sketch), restarts");
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
    con.printf("Periodic log %s\n", serialLog ? "on" : "off");
  } else if (cmd == "status") {
    con.printf("signal=%.3f raw height=%.1f mm filtered=%.1f mm zero offset=%.1f mm "
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
    con.printf("Zero offset %.1f mm stored.\n", zeroOffsetMm);
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
      con.println("Now pour in a known amount, wait ~30 s, then: cal add <litres>");
    } else if (sub == "add" || sub == "point") {
      if (!calActive) { calActive = true; calCount = 0; calTotal = 0; }
      if (sub == "add") {
        float l = arg.toFloat();
        if (l <= 0) { con.println("Usage: cal add <litres poured in this step>"); return; }
        float h;
        if (!measureSteadyHeight(h)) return;
        calAppend(h, calTotal + l);
      } else {
        int sp3 = arg.indexOf(' ');
        if (sp3 < 0) { con.println("Usage: cal point <mm> <total litres>"); return; }
        calAppend(arg.substring(0, sp3).toFloat(), arg.substring(sp3 + 1).toFloat());
      }
    } else if (sub == "undo") {
      if (calCount) { calCount--; calTotal = calCount ? calPts[calCount - 1].litres : 0; }
      printTable(calPts, calCount);
    } else if (sub == "show") {
      printTable(calPts, calCount);
      con.printf("  total so far: %.1f L\n", calTotal);
    } else if (sub == "save") {
      if (!tableValid(calPts, calCount)) {
        con.println("Need at least 2 points with rising height and litres.");
        return;
      }
      memcpy(table, calPts, calCount * sizeof(TankPoint));
      tablePoints = calCount;
      saveTable();
      calActive = false;
      con.printf("Saved %u points, capacity %.1f L.\n", (unsigned)tablePoints, capacityL());
      printAsCode();
    } else if (sub == "cancel") {
      calActive = false;
      calCount = 0;
      con.println("Calibration discarded.");
    } else {
      con.println("cal start | add <L> | point <mm> <L> | undo | show | save | cancel");
    }
  } else if (cmd == "table") {
    printAsCode();
  } else if (cmd == "reset") {
    prefs.begin("tank", false);
    prefs.clear();
    prefs.end();
    loadDefaults();
    emaHeight = NAN;
    con.println("Stored calibration erased, using sketch defaults.");
  } else if (cmd == "version") {
    con.println(FW_MARKER);
  } else if (cmd == "pin") {
    if (rest.length() == 0) {
      con.printf("iPhone console: %s\n", consolePin ? "on" : "off (no PIN)");
      return;
    }
    long pin = rest.toInt();
    bool off = rest == "off";
    if (!off && (rest.length() != 6 || pin < 100000 || pin > 999999)) {
      con.println("The PIN has 6 digits and does not start with 0.");
      return;
    }
    prefs.begin("ble", false);
    if (off) prefs.clear(); else prefs.putUInt("pin", (uint32_t)pin);
    prefs.end();
    forgetPhones();
    con.println(off ? "Stored PIN removed. Restarting..." : "PIN stored, paired phones forgotten. Restarting...");
    for (uint32_t t0 = millis(); millis() - t0 < 600; ) { consoleLoop(); delay(20); }   // let the phone see it
    Serial.flush();
    ESP.restart();
  } else if (cmd == "ota") {
    if (rest.startsWith("key ")) {
      String key = rest.substring(4);
      key.trim();
      if (key.length() < 8 || key.indexOf(' ') >= 0) { con.println("The key needs 8+ characters and no spaces."); return; }
      prefs.begin("ota", false);
      prefs.putString("key", key);
      prefs.end();
      con.println("OTA key stored. Restarting...");
      Serial.flush();
      ESP.restart();
    } else if (rest == "off") {
      prefs.begin("ota", false);
      prefs.clear();
      prefs.end();
      con.println("Stored OTA key removed. Restarting...");
      Serial.flush();
      ESP.restart();
    } else {
      con.printf("Updates over the air: %s\n", otaKey.length() ? "on" : "off (no key)");
    }
  } else {
    con.printf("Unknown command '%s' - type help\n", cmd.c_str());
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
  con.println(FW_MARKER);
  loadCalibration();
  loadBleSettings();
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
  con.println("Type 'help' for calibration commands.");
}

void loop() {
  static uint32_t tSample = 0, tTrend = 0, tAdv = 0;
  uint32_t now = millis();

  if (now - tSample >= SAMPLE_PERIOD_MS) { tSample = now; sampleLevel(); }
  if (now - tTrend >= TREND_PERIOD_MS)   { tTrend = now; updateTrend(); updateTemperature(); }
  if (now - tAdv >= ADV_UPDATE_MS)       { tAdv = now; updateAdvertisement(); }
  pollSerial();
  otaLoop();
  consoleLoop();

  delay(5);
}
