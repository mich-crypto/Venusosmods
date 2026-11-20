import Security
import SwiftUI
import UniformTypeIdentifiers

// MARK: - console: the Serial Monitor of the sender, over Bluetooth

struct ConsoleView: View {
    @ObservedObject var link: SensorLink
    @State private var command = ""

    var body: some View {
        VStack(spacing: 0) {
            ConsoleText(text: link.console)
            Divider()
            ScrollView(.horizontal, showsIndicators: false) {
                HStack {
                    ForEach(["status", "help", "log", "table", "cal show", "version"], id: \.self) { c in
                        Button(c) { link.send(c) }.buttonStyle(.bordered)
                    }
                }
                .padding(.horizontal)
                .padding(.top, 8)
            }
            HStack {
                TextField("Command, e.g. status", text: $command)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(send)
                Button("Send", action: send).buttonStyle(.borderedProminent)
            }
            .padding()
        }
        .navigationTitle("Console")
        .toolbar { Button("Clear") { link.clearConsole() } }
    }

    private func send() {
        let c = command.trimmingCharacters(in: .whitespaces)
        guard !c.isEmpty else { return }
        link.send(c)
        command = ""
    }
}

struct ConsoleText: View {
    let text: String

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Text(text.isEmpty ? "Nothing yet. Type help to see the commands." : text)
                    .font(.system(.footnote, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(10)
                Color.clear.frame(height: 1).id("end")
            }
            .background(Color(uiColor: .secondarySystemBackground))
            .onChange(of: text) { _ in proxy.scrollTo("end", anchor: .bottom) }
            .onAppear { proxy.scrollTo("end", anchor: .bottom) }
        }
    }
}

// MARK: - calibration: the "cal" commands with buttons

struct CalibrationView: View {
    @ObservedObject var link: SensorLink
    @State private var litres = 5.0
    @State private var confirmSave = false

    var body: some View {
        VStack(spacing: 0) {
            List {
                Section {
                    Text("Fill the tank in steps and record the height after each one. The sensor stores the table in its flash. Wait until the water is still before each step (≈30 s).")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Section("1. Empty tank") {
                    Button { link.send("cal start") } label: { Label("Tank is empty: start", systemImage: "play") }
                }
                Section("2. After each portion") {
                    Stepper(value: $litres, in: 0.5...100, step: 0.5) {
                        Text(String(format: "Poured in: %.1f L", litres)).monospacedDigit()
                    }
                    Button { link.send(String(format: "cal add %.1f", litres)) } label: {
                        Label("Record this portion", systemImage: "plus.circle")
                    }
                    Button { link.send("cal undo") } label: { Label("Undo the last point", systemImage: "arrow.uturn.backward") }
                    Button { link.send("cal show") } label: { Label("Show the table so far", systemImage: "list.number") }
                }
                Section {
                    Button { confirmSave = true } label: { Label("Tank is full: save", systemImage: "checkmark.circle") }
                    Button(role: .destructive) { link.send("cal cancel") } label: { Label("Discard", systemImage: "trash") }
                } header: {
                    Text("3. Finish")
                } footer: {
                    Text("If the tank was calibrated on the Pi's settings page, the Pi uses that table instead. To use this one there: Water tab → Calibrate → \"Use the sender's own table again\".")
                }
            }
            ConsoleText(text: link.consoleLines.suffix(14).joined(separator: "\n"))
                .frame(height: 200)
        }
        .navigationTitle("Calibrate")
        .confirmationDialog("Save this table? The last point counts as 100 %.", isPresented: $confirmSave, titleVisibility: .visible) {
            Button("Save") { link.send("cal save") }
        }
    }
}

// MARK: - firmware update

struct FirmwareView: View {
    @ObservedObject var link: SensorLink
    @State private var picking = false
    @State private var image: FirmwareImage?
    @State private var fileError: String?
    @State private var key = Keychain.read("ota_key") ?? ""

    var body: some View {
        List {
            Section {
                Text("Arduino IDE: Sketch → Export Compiled Binary, then put the file ending in .ino.bin into the Files app (iCloud Drive, AirDrop…). The sender checks the new firmware before it starts it; if anything goes wrong it keeps the old one.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            Section("Firmware file") {
                Button { picking = true } label: { Label(image == nil ? "Choose the .ino.bin file" : "Choose another file", systemImage: "doc") }
                if let image {
                    LabeledContent("Version", value: image.version)
                    LabeledContent("Size", value: "\(image.data.count / 1024) KB")
                    LabeledContent("Sender ID", value: "\(image.deviceID)")
                }
                if let fileError { Label(fileError, systemImage: "xmark.octagon").foregroundStyle(.red) }
            }
            Section {
                SecureField("OTA key", text: $key)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Text("Update key")
            } footer: {
                Text("OTA_KEY from the sketch, the same as on the Pi. Kept in this iPhone's keychain.")
            }
            Section {
                Button {
                    guard let image else { return }
                    Keychain.save("ota_key", key)
                    link.startUpdate(image, key: key)
                } label: { Label("Send to the sensor", systemImage: "arrow.up.circle.fill") }
                .disabled(image == nil || key.isEmpty || link.state != .ready || running)
                if let update = link.update {
                    ProgressView(value: update.progress)
                    ForEach(Array(update.lines.enumerated()), id: \.offset) { _, line in
                        Text(line).font(.footnote.monospaced())
                            .foregroundStyle(update.finished && line == update.lines.last ? (update.ok ? .green : .red) : .primary)
                    }
                }
            }
        }
        .navigationTitle("Firmware")
        .interactiveDismissDisabled(running)
        .navigationBarBackButtonHidden(running)
        .fileImporter(isPresented: $picking, allowedContentTypes: [.data]) { result in
            fileError = nil
            image = nil
            do {
                let url = try result.get()
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                image = try FirmwareImage(data: Data(contentsOf: url))
            } catch {
                fileError = error.localizedDescription
            }
        }
    }

    private var running: Bool { link.update != nil && link.update?.finished == false }
}

// MARK: - keychain, for the OTA key

enum Keychain {
    private static let service = "local.venusosmods.watersensor"

    static func save(_ account: String, _ value: String) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service, kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
        var add = query
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    static func read(_ account: String) -> String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service, kSecAttrAccount as String: account,
                                    kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
