# Water Sensor (iPhone / iPad app)

An app for the ESP32 water tank sender (`firmware/esp32_hydro_tank`), straight over Bluetooth,
without the Pi:

* **Level at a glance**: every sender in range with level, litres, water height, temperature and
  state (filling, draining, water moving). This needs no login.
* **Log in with a PIN**: iOS asks for the sensor's 6-digit PIN once, then remembers the
  sensor. The link is encrypted from then on.
* **Calibrate**: zero on an empty tank, then fill-in-steps calibration with buttons.
* **Console**: the sender's Serial Monitor (`status`, `help`, `table`, ...) on the phone.
* **Firmware update**: pick the `.ino.bin` in the Files app, the same check and protocol as the
  Pi's settings page.

## Prepare the sender (once, by USB)

In `esp32_hydro_tank.ino` set your own values and flash it:

```c
#define CONSOLE_PIN   123456              // 6 digits, not starting with 0. 0 = no app login
#define OTA_KEY       "your-own-secret"   // only needed for firmware updates
```

Or keep the sketch as it is and type in the Serial Monitor: `pin 123456` (and `ota key ...`).
Changing the PIN later makes the sender forget all paired phones. On the iPhone, then open
Settings → Bluetooth → ⓘ next to **VHT** → *Forget This Device* before you log in again.

## Install the app

Apple only lets you install your own apps through their tools. The app is a *Swift
Playgrounds app package* (`WaterSensor.swiftpm`), so two ways work:

**With a Mac (free Apple ID is enough)**
1. Install **Xcode** from the Mac App Store.
2. Download this repository and open the `ios/WaterSensor.swiftpm` folder in Xcode.
3. Connect the iPhone by cable, pick it as the run destination, and under *Signing & Capabilities*
   choose your Apple ID as the team. Press ▶.
4. First time on the iPhone: Settings → General → VPN & Device Management → trust your Apple ID.
   (Settings → Privacy & Security → *Developer Mode* must be on.)

With a free Apple ID the app runs for 7 days, then press ▶ in Xcode again. With a paid
developer account it runs for a year, or you can share it with TestFlight.

**With an iPad only**
1. Install **Swift Playgrounds** from the App Store.
2. Safari: download this repository as ZIP (GitHub: *Code → Download ZIP*), open it in the
   Files app (it unzips), and open `ios/WaterSensor.swiftpm` with Swift Playgrounds.
3. Press ▶: the app runs on the iPad. To get it onto an iPhone from there you need a paid
   developer account (*App Settings → Upload to App Store Connect*, then TestFlight).

## How it talks to the sender

| What | How |
|---|---|
| Level | the broadcast the Pi also listens to (manufacturer data 0xFFFF, `VH` payload) |
| Console | Nordic UART Service `6E400001-…`, needs pairing with the PIN (passkey, bonded, MITM) |
| Firmware | service `8E7F0001-…`, HMAC-SHA256 proof of the OTA key with a one-time nonce, chunks with offsets, MD5 checked by the sender |

Other terminal apps (Bluefruit Connect, nRF Toolbox → UART) can use the console too.
