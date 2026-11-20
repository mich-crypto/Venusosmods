import CoreBluetooth
import Foundation

/// Listens for the senders' broadcasts (no connection needed for the level) and hands out
/// one SensorLink per sensor for logging in.
final class BluetoothManager: NSObject, ObservableObject, CBCentralManagerDelegate {
    struct Seen: Identifiable {
        let id: UUID
        let peripheral: CBPeripheral
        var reading: Reading
        var rssi: Int
        var lastSeen: Date
        var connectable: Bool
        var name: String { "Water tank \(reading.deviceID)" }
    }

    @Published private(set) var state: CBManagerState = .unknown
    @Published private(set) var sensors: [UUID: Seen] = [:]
    private(set) var central: CBCentralManager!
    private var links: [UUID: SensorLink] = [:]

    override init() {
        super.init()
        central = CBCentralManager(delegate: self, queue: .main)
    }

    var sorted: [Seen] { sensors.values.sorted { ($0.reading.deviceID, $0.id.uuidString) < ($1.reading.deviceID, $1.id.uuidString) } }

    func link(for id: UUID) -> SensorLink? {
        if let l = links[id] { return l }
        guard let seen = sensors[id] else { return nil }
        let l = SensorLink(peripheral: seen.peripheral, manager: self)
        links[id] = l
        return l
    }

    func disconnectAll() { links.values.forEach { $0.disconnect() } }

    // MARK: scanning

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        state = central.state
        if central.state == .poweredOn {
            // duplicates on: every broadcast (every 2 s) refreshes the level while the app is open
            central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard let data = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data,
              let reading = Reading(manufacturerData: data) else { return }
        let connectable = (advertisementData[CBAdvertisementDataIsConnectable] as? NSNumber)?.boolValue ?? false
        let rssi = RSSI.intValue == 127 ? (sensors[peripheral.identifier]?.rssi ?? 0) : RSSI.intValue
        sensors[peripheral.identifier] = Seen(id: peripheral.identifier, peripheral: peripheral, reading: reading,
                                              rssi: rssi, lastSeen: Date(), connectable: connectable)
    }

    // MARK: connections, passed on to the link

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        links[peripheral.identifier]?.didConnect()
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        links[peripheral.identifier]?.didDisconnect(error)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        links[peripheral.identifier]?.didDisconnect(error)
    }
}

/// A connection to one sensor: console (after the PIN login) and firmware update.
final class SensorLink: NSObject, ObservableObject, CBPeripheralDelegate {
    enum State: Equatable {
        case disconnected
        case connecting
        case loggingIn
        case ready
        case failed(String)
    }

    struct Update {
        var progress: Double = 0
        var lines: [String] = []
        var finished = false
        var ok = false
    }

    @Published private(set) var state = State.disconnected
    @Published private(set) var console = ""
    @Published private(set) var hasConsole = false
    @Published private(set) var hasUpdate = false
    @Published private(set) var firmware: String?
    @Published private(set) var update: Update?

    let peripheral: CBPeripheral
    private weak var manager: BluetoothManager?
    private var rx, tx, otaControl, otaData: CBCharacteristic?
    private var servicesLeft = 0
    private var pendingLine = ""

    init(peripheral: CBPeripheral, manager: BluetoothManager) {
        self.peripheral = peripheral
        self.manager = manager
    }

    var consoleLines: [String] { console.split(separator: "\n", omittingEmptySubsequences: false).map(String.init) }

    // MARK: connect / log in

    func connect() {
        guard let central = manager?.central, state != .connecting, state != .loggingIn, state != .ready else { return }
        state = .connecting
        hasConsole = false
        hasUpdate = false
        peripheral.delegate = self
        central.connect(peripheral)
    }

    func disconnect() {
        if state != .disconnected { manager?.central.cancelPeripheralConnection(peripheral) }
        if case .failed = state { return }
        state = .disconnected
    }

    func didConnect() {
        state = .loggingIn
        peripheral.discoverServices([SensorUUID.console, SensorUUID.ota])
    }

    func didDisconnect(_ error: Error?) {
        rx = nil; tx = nil; otaControl = nil; otaData = nil
        if update != nil && !(update?.finished ?? true) {
            finishUpdate(false, "The connection was lost. The sender keeps its old firmware.")
        }
        if case .failed = state { return }
        if update?.ok == true {
            state = .disconnected          // the sender restarted into the new firmware
        } else {
            state = error.map { State.failed(SensorLink.explain($0)) } ?? .disconnected
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        let services = peripheral.services ?? []
        servicesLeft = services.count
        if services.isEmpty { finishSetup() }
        for s in services { peripheral.discoverCharacteristics(nil, for: s) }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        for c in service.characteristics ?? [] {
            switch c.uuid {
            case SensorUUID.consoleRX: rx = c
            case SensorUUID.consoleTX: tx = c
            case SensorUUID.otaControl: otaControl = c
            case SensorUUID.otaData: otaData = c
            default: break
            }
        }
        servicesLeft -= 1
        if servicesLeft <= 0 { finishSetup() }
    }

    private func finishSetup() {
        hasUpdate = otaControl != nil && otaData != nil
        if let otaControl { peripheral.setNotifyValue(true, for: otaControl) }
        if let tx {
            // the console needs the pairing: the first time, iOS asks for the PIN here
            hasConsole = true
            peripheral.setNotifyValue(true, for: tx)
        } else {
            state = .ready          // no console (no PIN on the sender); updates may still work
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard characteristic.uuid == SensorUUID.consoleTX else { return }
        if let error {
            state = .failed(SensorLink.explain(error))
            manager?.central.cancelPeripheralConnection(peripheral)
            return
        }
        state = .ready
        send("version")
    }

    static func explain(_ error: Error) -> String {
        let ns = error as NSError
        if ns.domain == CBATTErrorDomain,
           [CBATTError.Code.insufficientAuthentication.rawValue, CBATTError.Code.insufficientEncryption.rawValue].contains(ns.code) {
            return "Login failed: wrong PIN, or the pairing was cancelled. Try again."
        }
        if ns.domain == CBErrorDomain, ns.code == CBError.Code.peerRemovedPairingInformation.rawValue {
            return "The sensor no longer knows this iPhone (its PIN was changed). Open Settings → Bluetooth, tap ⓘ next to VHT, \"Forget This Device\", then log in again."
        }
        return error.localizedDescription
    }

    // MARK: console

    func send(_ command: String) {
        guard let rx, state == .ready else { return }
        peripheral.writeValue(Data(command.utf8), for: rx, type: .withResponse)
    }

    func clearConsole() { console = "" }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard let value = characteristic.value else { return }
        if characteristic.uuid == SensorUUID.consoleTX {
            let text = String(decoding: value, as: UTF8.self).replacingOccurrences(of: "\r", with: "")
            console += text
            if console.count > 30000 { console = String(console.suffix(20000)) }
            pendingLine += text
            while let nl = pendingLine.firstIndex(of: "\n") {
                let line = String(pendingLine[..<nl])
                pendingLine = String(pendingLine[pendingLine.index(after: nl)...])
                if let m = FirmwareImage.parseMarker(line) { firmware = m.version }
            }
        } else if characteristic.uuid == SensorUUID.otaControl {
            otaAnswer(String(decoding: value, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if characteristic.uuid == SensorUUID.otaData {
            otaWritten(error)
        } else if let error, characteristic.uuid == SensorUUID.consoleRX {
            console += "(not sent: \(SensorLink.explain(error)))\n"
        } else if let error, characteristic.uuid == SensorUUID.otaControl {
            finishUpdate(false, "Could not talk to the sender: \(error.localizedDescription)")
        }
    }

    // MARK: firmware update (same protocol as esp32_ota.py on the Pi)

    private enum Stage { case idle, info, begin, data, end }
    private var stage = Stage.idle
    private var image: FirmwareImage?
    private var key = ""
    private var offset = 0
    private var chunk = 180
    private var watchdog: Timer?
    private var lastStep = Date()
    private var started = Date()

    func startUpdate(_ image: FirmwareImage, key: String) {
        guard let otaControl, state == .ready, stage == .idle || update?.finished == true else { return }
        self.image = image
        self.key = key
        update = Update()
        log("New firmware \(image.version), \(image.data.count / 1024) KB.")
        stage = .info
        step()
        watchdog?.invalidate()
        watchdog = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, self.stage != .idle else { return }
            if Date().timeIntervalSince(self.lastStep) > 20 { self.finishUpdate(false, "The sender stopped answering.") }
        }
        peripheral.writeValue(Data("info".utf8), for: otaControl, type: .withResponse)
    }

    private func step() { lastStep = Date() }

    private func log(_ line: String) { update?.lines.append(line) }

    private func otaAnswer(_ text: String) {
        guard stage != .idle, let image, let otaControl, let otaData else { return }
        step()
        if text.hasPrefix("err") {
            finishUpdate(false, "The sender says: \(text.dropFirst(3).trimmingCharacters(in: .whitespaces)). It keeps its old firmware.")
            return
        }
        switch stage {
        case .info:
            guard let info = OtaAuth.parseInfo(text), let nonce = info["nonce"] else { return }
            log("The sender runs firmware \(info["fw"] ?? "?") (id \(info["id"] ?? "?")).")
            if let id = info["id"], id != String(image.deviceID) {
                finishUpdate(false, "The new firmware has DEVICE_ID \(image.deviceID), this sender is \(id). Change DEVICE_ID in the sketch.")
                return
            }
            if let free = Int(info["free"] ?? ""), free > 0, image.data.count > free {
                finishUpdate(false, "The firmware does not fit the sender's update slot (\(free / 1024) KB).")
                return
            }
            stage = .begin
            let proof = OtaAuth.proof(key: key, nonce: nonce, size: image.data.count, md5: image.md5)
            peripheral.writeValue(Data("begin \(image.data.count) \(image.md5) \(proof)".utf8), for: otaControl, type: .withResponse)
        case .begin where text == "ok begin":
            stage = .data
            offset = 0
            started = Date()
            chunk = max(16, min(508, peripheral.maximumWriteValueLength(for: .withoutResponse) - 4))
            log("Key accepted. Sending in chunks of \(chunk) bytes…")
            sendChunk(otaData)
        case .end where text == "ok end":
            finishUpdate(true, "Done: the sender checked firmware \(image.version) and restarts into it. Its readings are back in a few seconds.")
        default:
            break
        }
    }

    private func sendChunk(_ c: CBCharacteristic) {
        guard let image else { return }
        let end = min(image.data.count, offset + chunk)
        var frame = Data()
        withUnsafeBytes(of: UInt32(offset).littleEndian) { frame.append(contentsOf: $0) }
        frame.append(image.data.subdata(in: offset..<end))
        peripheral.writeValue(frame, for: c, type: .withResponse)
    }

    private func otaWritten(_ error: Error?) {
        guard stage == .data, let image, let otaData, let otaControl else { return }
        if let error {
            finishUpdate(false, "Sending failed: \(error.localizedDescription). The sender keeps its old firmware.")
            return
        }
        step()
        offset = min(image.data.count, offset + chunk)
        let previous = Int((update?.progress ?? 0) * 10)
        update?.progress = Double(offset) / Double(image.data.count)
        if Int((update?.progress ?? 0) * 10) != previous {
            let kbs = Double(offset) / 1024 / max(0.1, Date().timeIntervalSince(started))
            log(String(format: "%3.0f %%  %.1f KB/s", (update?.progress ?? 0) * 100, kbs))
        }
        if offset >= image.data.count {
            stage = .end
            log("All sent, the sender checks it…")
            peripheral.writeValue(Data("end".utf8), for: otaControl, type: .withResponse)
        } else {
            sendChunk(otaData)
        }
    }

    private func finishUpdate(_ ok: Bool, _ message: String) {
        guard stage != .idle else { return }
        stage = .idle
        watchdog?.invalidate()
        watchdog = nil
        log(message)
        update?.finished = true
        update?.ok = ok
        if !ok, let otaControl, peripheral.state == .connected {
            peripheral.writeValue(Data("abort".utf8), for: otaControl, type: .withResponse)
        }
    }
}
