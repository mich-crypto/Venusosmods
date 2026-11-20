import CoreBluetooth
import CryptoKit
import Foundation

// Everything here must match firmware/esp32_hydro_tank/esp32_hydro_tank.ino
// (and data/shared_drivers/esp32_hydro_protocol.py / esp32_ota.py on the Pi).

enum SensorUUID {
    // Nordic UART Service: the serial console, only for a phone paired with the PIN
    static let console = CBUUID(string: "6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
    static let consoleRX = CBUUID(string: "6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
    static let consoleTX = CBUUID(string: "6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
    // Firmware update over the air, protected by the OTA key
    static let ota = CBUUID(string: "8E7F0001-5648-4F54-A1C3-5F2D7C3A9B10")
    static let otaControl = CBUUID(string: "8E7F0002-5648-4F54-A1C3-5F2D7C3A9B10")
    static let otaData = CBUUID(string: "8E7F0003-5648-4F54-A1C3-5F2D7C3A9B10")
}

/// One broadcast of the sender. iOS hands over the manufacturer data with the company id
/// (0xFFFF, little endian) in front of the 16-byte payload.
struct Reading: Equatable {
    var deviceID: Int
    var sequence: Int
    var level: Double          // %
    var volumeL: Double
    var capacityL: Double
    var heightMM: Int
    var temperature: Double?   // °C, nil without a DS18B20
    var filling: Bool
    var draining: Bool
    var fault: Bool
    var sloshing: Bool

    init?(manufacturerData data: Data) {
        let b = [UInt8](data)
        guard b.count >= 18, b[0] == 0xFF, b[1] == 0xFF,      // company id
              b[2] == 0x56, b[3] == 0x48, b[4] == 1 else {     // 'V' 'H', protocol version 1
            return nil
        }
        let p = 2
        func u16(_ i: Int) -> Int { Int(b[p + i]) | Int(b[p + i + 1]) << 8 }
        let flags = b[p + 4]
        deviceID = Int(b[p + 3])
        sequence = Int(b[p + 5])
        level = Double(u16(6)) / 100
        volumeL = Double(u16(8)) / 10
        capacityL = Double(u16(10)) / 10
        heightMM = u16(12)
        let raw = Int16(bitPattern: UInt16(u16(14)))
        temperature = flags & 0x01 != 0 ? Double(raw) / 100 : nil
        filling = flags & 0x02 != 0
        draining = flags & 0x04 != 0
        fault = flags & 0x08 != 0
        sloshing = flags & 0x10 != 0
    }

    var statusText: String {
        if fault { return "Sensor fault" }
        if sloshing { return "Water moving (driving)" }
        if filling { return "Filling" }
        if draining { return "Draining" }
        return "Steady"
    }
}

struct AppError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// A firmware file (.ino.bin), checked the same way as on the Pi before it is sent.
struct FirmwareImage {
    let data: Data
    let version: String
    let deviceID: Int
    let md5: String

    init(data: Data) throws {
        let b = [UInt8](data)
        if b.count >= 2, b[0] == 0xAA, b[1] == 0x50 {
            throw AppError("This is the partition table (.partitions.bin). Pick the .ino.bin file.")
        }
        guard b.count >= 24, b[0] == 0xE9 else {
            throw AppError("This is not an ESP32 firmware file. Pick the .ino.bin file from \"Sketch > Export Compiled Binary\".")
        }
        if b.count > 3 * 1024 * 1024 {
            throw AppError("This is the merged image with the bootloader (.merged.bin). Pick the .ino.bin file.")
        }
        if b.count < 64 * 1024 {
            throw AppError("This is the bootloader (.bootloader.bin). Pick the .ino.bin file.")
        }
        let chip = Int(b[12]) | Int(b[13]) << 8
        guard chip == 9 else {
            throw AppError("This firmware is built for another chip (id \(chip)). The sender is an ESP32-S3: pick \"ESP32S3 Dev Module\" as the board.")
        }
        guard let marker = FirmwareImage.findMarker(b) else {
            throw AppError("This is not the water tank sender firmware (esp32_hydro_tank.ino 1.1.0 or newer).")
        }
        self.data = data
        self.version = marker.version
        self.deviceID = marker.id
        self.md5 = Insecure.MD5.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    /// "VHT-FW 1.1.0 id=1 ;" somewhere in the image (also printed by the "version" command).
    static func findMarker(_ b: [UInt8]) -> (version: String, id: Int)? {
        let needle = Array("VHT-FW ".utf8)
        guard b.count > needle.count else { return nil }
        var i = 0
        while i <= b.count - needle.count {
            if b[i] == needle[0] && Array(b[i..<(i + needle.count)]) == needle {
                let end = min(b.count, i + 48)
                if let text = String(bytes: b[i..<end], encoding: .ascii), let m = parseMarker(text) {
                    return m
                }
            }
            i += 1
        }
        return nil
    }

    static func parseMarker(_ text: String) -> (version: String, id: Int)? {
        guard let r = text.range(of: #"VHT-FW ([0-9A-Za-z._+-]{1,24}) id=(\d{1,3}) ;"#, options: .regularExpression) else {
            return nil
        }
        let parts = text[r].split(separator: " ")
        guard parts.count >= 3, let id = Int(parts[2].dropFirst(3)) else { return nil }
        return (String(parts[1]), id)
    }
}

enum OtaAuth {
    /// hex HMAC-SHA256(key, "<nonce> <size> <md5>"): proves the key without sending it.
    static func proof(key: String, nonce: String, size: Int, md5: String) -> String {
        let mac = HMAC<SHA256>.authenticationCode(for: Data("\(nonce) \(size) \(md5)".utf8),
                                                  using: SymmetricKey(data: Data(key.utf8)))
        return mac.map { String(format: "%02x", $0) }.joined()
    }

    /// "info fw=1.1.0 id=1 nonce=ab.. free=1310720" -> ["fw": "1.1.0", ...]
    static func parseInfo(_ text: String) -> [String: String]? {
        guard text.hasPrefix("info ") else { return nil }
        var out: [String: String] = [:]
        for part in text.dropFirst(5).split(separator: " ") {
            let kv = part.split(separator: "=", maxSplits: 1)
            if kv.count == 2 { out[String(kv[0])] = String(kv[1]) }
        }
        return out
    }
}
