# Ready-made firmware (ESP32-S3)

Built from `firmware/esp32_hydro_tank` with the sketch defaults: QDY30A 0–3.3 V, 2 m range, DS18B20 temperature probe 1 on GPIO5 and probe 2 on GPIO6,
sender id 1, own Wi-Fi `Watertank-1` / `watertank`. Board *ESP32S3 Dev Module*, esp32 core 3.3.13,
USB CDC On Boot enabled, partition scheme *Minimal SPIFFS (1.9MB APP with OTA)*, 4 MB flash or more.

| File | Use it for |
|---|---|
| `FLASH-AT-0x0_esp32_hydro_tank_full.bin` | **First flash** in the browser: https://espressif.github.io/esptool-js/ (Chrome/Edge on a computer), address **0x0** |
| `WEBPAGE-UPDATE-ONLY_esp32_hydro_tank.bin` | **Updates**: from the Venus OS page (Water → Sender → *Update sender firmware*, over Bluetooth, firmware 2.7+), or on the sender's own page (Firmware update); keeps the calibration |
| `VERSION` | the version of these files; the Venus OS page compares it with the sender's and shows when there is a newer one |

Different sensor or settings? Change the CONFIGURATION block and build with the Arduino IDE (docs/GUIDE.md, part 3).
