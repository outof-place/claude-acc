// swift-tools-version: 6.2
import PackageDescription

let settings: [SwiftSetting] = [
    // everything on the main actor unless it says otherwise (SE-0466)
    .defaultIsolation(MainActor.self),
    .enableUpcomingFeature("NonisolatedNonsendingByDefault"),
    .enableUpcomingFeature("InferIsolatedConformances"),
]

let package = Package(
    name: "ClaudeAcc",
    platforms: [.macOS("26.0")],
    targets: [
        // the menu bar app
        .executableTarget(name: "ClaudeAcc", path: "Sources/ClaudeAcc", swiftSettings: settings),
        // fan control through the SMC; runs as a root LaunchDaemon, see install-fans.sh
        .executableTarget(name: "fanctl", path: "Sources/fanctl", swiftSettings: settings),
    ],
    swiftLanguageModes: [.v6]
)
