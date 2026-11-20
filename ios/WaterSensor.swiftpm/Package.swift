// swift-tools-version: 5.9

// Water Sensor: iPhone app for the ESP32 water tank sender.
// Open this folder in Swift Playgrounds (iPad or Mac) or in Xcode on a Mac.
// WARNING: Swift Playgrounds and Xcode regenerate parts of this file when you change the app
// settings in their UI; that is fine.

import PackageDescription
import AppleProductTypes

let package = Package(
    name: "Water Sensor",
    platforms: [
        .iOS("16.0")
    ],
    products: [
        .iOSApplication(
            name: "Water Sensor",
            targets: ["AppModule"],
            bundleIdentifier: "local.venusosmods.watersensor",
            displayVersion: "1.0",
            bundleVersion: "1",
            appIcon: .placeholder(icon: .leaf),
            accentColor: .presetColor(.blue),
            supportedDeviceFamilies: [
                .pad,
                .phone
            ],
            supportedInterfaceOrientations: [
                .portrait,
                .landscapeRight,
                .landscapeLeft,
                .portraitUpsideDown(.when(deviceFamilies: [.pad]))
            ],
            capabilities: [
                .bluetoothAlways(purposeString: "Shows the water level from your tank sensor and lets you set it up.")
            ]
        )
    ],
    targets: [
        .executableTarget(
            name: "AppModule",
            path: "."
        )
    ]
)
