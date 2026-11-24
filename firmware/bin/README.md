# Ready-made firmware (ESP32-S3)

Built from `firmware/esp32_hydro_tank` with the sketch defaults: QDY30A 0–3.3 V, 2 m range, no DS18B20 temperature probe,
sender id 1, own Wi-Fi `Watertank-1` / `watertank`. Board *ESP32S3 Dev Module*, esp32 core 3.3.13,
USB CDC On Boot enabled, partition scheme *Minimal SPIFFS (1.9MB APP with OTA)*, 4 MB flash or more.

| File | Use it for |
|---|---|
| `esp32_hydro_tank.merged.bin` | **First flash** in the browser: https://espressif.github.io/esptool-js/ (Chrome/Edge on a computer), address **0x0** |
| `esp32_hydro_tank.ino.bin` | **Updates** on the sender's own page (http://192.168.4.1 → Firmware update); keeps the calibration |

Different sensor or settings? Change the CONFIGURATION block and build with the Arduino IDE (docs/GUIDE.md, part 3).
