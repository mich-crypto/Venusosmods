import SwiftUI

@main
struct WaterSensorApp: App {
    @StateObject private var bluetooth = BluetoothManager()

    var body: some Scene {
        WindowGroup {
            SensorListView()
                .environmentObject(bluetooth)
        }
    }
}

// MARK: - list of sensors in range

struct SensorListView: View {
    @EnvironmentObject var bt: BluetoothManager

    var body: some View {
        NavigationStack {
            List {
                if let problem = bluetoothProblem {
                    Section { Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                }
                Section {
                    if bt.sorted.isEmpty && bluetoothProblem == nil {
                        HStack(spacing: 12) {
                            ProgressView()
                            Text("Looking for water sensors…").foregroundStyle(.secondary)
                        }
                    }
                    ForEach(bt.sorted) { seen in
                        NavigationLink(value: seen.id) { SensorRow(seen: seen) }
                    }
                } header: {
                    Text("In range")
                } footer: {
                    Text("The level comes from the sensor's broadcast, so it shows without logging in. Log in with the sensor's PIN to calibrate it or update its firmware.")
                }
            }
            .navigationTitle("Water Sensor")
            .navigationDestination(for: UUID.self) { id in SensorView(id: id) }
            .onAppear { bt.disconnectAll() }
        }
    }

    private var bluetoothProblem: String? {
        switch bt.state {
        case .poweredOn, .unknown, .resetting: return nil
        case .poweredOff: return "Bluetooth is off. Switch it on in Control Center."
        case .unauthorized: return "This app may not use Bluetooth. Allow it in Settings → Water Sensor."
        case .unsupported: return "This device has no Bluetooth LE."
        @unknown default: return nil
        }
    }
}

struct SensorRow: View {
    let seen: BluetoothManager.Seen

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(seen.name).font(.headline)
                Text(seen.reading.fault ? "Sensor fault" :
                        String(format: "%.0f L of %.0f L · %@", seen.reading.volumeL, seen.reading.capacityL, seen.reading.statusText))
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            Text(seen.reading.fault ? "–" : String(format: "%.0f %%", seen.reading.level))
                .font(.title2.monospacedDigit().weight(.semibold))
        }
        .padding(.vertical, 4)
    }
}

// MARK: - one sensor

struct SensorView: View {
    @EnvironmentObject var bt: BluetoothManager
    let id: UUID

    var body: some View {
        if let seen = bt.sensors[id], let link = bt.link(for: id) {
            SensorDetail(seen: seen, link: link)
        } else {
            Text("This sensor is out of range.").foregroundStyle(.secondary)
        }
    }
}

struct SensorDetail: View {
    let seen: BluetoothManager.Seen
    @ObservedObject var link: SensorLink
    @State private var confirmZero = false

    var body: some View {
        List {
            Section {
                LevelGauge(level: seen.reading.fault ? nil : seen.reading.level)
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            }
            Section("Now") {
                row("Water", String(format: "%.1f L of %.0f L", seen.reading.volumeL, seen.reading.capacityL))
                row("Water height", "\(seen.reading.heightMM) mm")
                if let t = seen.reading.temperature { row("Water temperature", String(format: "%.1f °C", t)) }
                row("State", seen.reading.statusText)
                TimelineView(.periodic(from: .now, by: 1)) { ctx in
                    row("Signal", "\(seen.rssi) dBm · \(max(0, Int(ctx.date.timeIntervalSince(seen.lastSeen)))) s ago")
                }
                if let fw = link.firmware { row("Firmware", fw) }
            }
            Section {
                setupRows
            } header: {
                Text("Setup")
            } footer: {
                Text("The first time, iOS asks for the 6-digit PIN of the sensor (CONSOLE_PIN in the sketch, or set with \"pin\" in the Serial Monitor).")
            }
        }
        .navigationTitle(seen.name)
        .confirmationDialog("Is the tank empty?", isPresented: $confirmZero, titleVisibility: .visible) {
            Button("Yes, set this level as 0 mm") { link.send("zero") }
        } message: {
            Text("The sensor measures for 5 seconds and takes that as empty. The water must be still.")
        }
    }

    @ViewBuilder private var setupRows: some View {
        switch link.state {
        case .disconnected, .failed:
            if case .failed(let message) = link.state {
                Label(message, systemImage: "xmark.octagon").foregroundStyle(.red)
            }
            if seen.connectable {
                Button { link.connect() } label: { Label("Log in", systemImage: "lock.open") }
            } else {
                Text("This sensor does not accept connections. Set CONSOLE_PIN in the sketch and flash it once by USB, or type \"pin 123456\" (your own 6 digits) in the Serial Monitor.")
                    .foregroundStyle(.secondary)
            }
        case .connecting, .loggingIn:
            HStack(spacing: 12) {
                ProgressView()
                Text(link.state == .connecting ? "Connecting…" : "Logging in…")
            }
        case .ready:
            if link.hasConsole {
                NavigationLink { CalibrationView(link: link) } label: { Label("Calibrate the tank", systemImage: "ruler") }
                Button { confirmZero = true } label: { Label("Tank is empty: set zero", systemImage: "arrow.down.to.line") }
                NavigationLink { ConsoleView(link: link) } label: { Label("Console", systemImage: "terminal") }
            } else {
                Text("The sensor has no PIN, so there is no console. Firmware updates still work with the OTA key.")
                    .foregroundStyle(.secondary)
            }
            if link.hasUpdate {
                NavigationLink { FirmwareView(link: link) } label: { Label("Update firmware", systemImage: "arrow.down.circle") }
            }
            Button(role: .destructive) { link.disconnect() } label: { Label("Log out", systemImage: "lock") }
        }
    }

    private func row(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(value).foregroundStyle(.secondary).monospacedDigit()
        }
    }
}

struct LevelGauge: View {
    let level: Double?   // %, nil = fault

    var body: some View {
        ZStack {
            Circle().stroke(Color.secondary.opacity(0.2), lineWidth: 18)
            Circle()
                .trim(from: 0, to: CGFloat((level ?? 0) / 100))
                .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 18, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.easeInOut, value: level)
            VStack {
                Text(level.map { String(format: "%.0f %%", $0) } ?? "–").font(.system(size: 44, weight: .bold).monospacedDigit())
                Text(level == nil ? "sensor fault" : "full").foregroundStyle(.secondary)
            }
        }
        .frame(width: 190, height: 190)
        .padding(.vertical, 8)
    }
}
