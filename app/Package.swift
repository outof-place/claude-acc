// swift-tools-version: 6.2
import PackageDescription

let settings: [SwiftSetting] = [
    // everything on the main actor unless it says otherwise (SE-0466)
    .defaultIsolation(MainActor.self),
    .enableUpcomingFeature("NonisolatedNonsendingByDefault"),
    .enableUpcomingFeature("InferIsolatedConformances"),
]

// dictation's pure core runs off the main actor: no default isolation there
let core: [SwiftSetting] = [
    .enableUpcomingFeature("NonisolatedNonsendingByDefault"),
    .enableUpcomingFeature("InferIsolatedConformances"),
]

let package = Package(
    name: "ClaudeAcc",
    platforms: [.macOS("26.0")],
    targets: [
        // the menu bar app
        .executableTarget(
            name: "ClaudeAcc", dependencies: ["DictationCore"], path: "Sources/ClaudeAcc", swiftSettings: settings),
        // dictation without AppKit: text rules, the AI Gateway client, audio math, the right ⌥ trigger
        .target(name: "DictationCore", path: "Sources/DictationCore", swiftSettings: core),
        .testTarget(
            name: "DictationCoreTests", dependencies: ["DictationCore"], path: "Tests/DictationCoreTests",
            resources: [.copy("Fixtures")], swiftSettings: core),
        // fan control through the SMC; runs as a root LaunchDaemon, see install-fans.sh
        .executableTarget(name: "fanctl", path: "Sources/fanctl", swiftSettings: settings),
        // the PreToolUse hook's native front: answers most Bash commands without starting Python
        .executableTarget(name: "claude-acc-hook", path: "Sources/hook", swiftSettings: settings),
        // the limit pause hooks after every tool call; plain C, so it starts without a runtime
        .executableTarget(name: "claude-acc-pause", path: "Sources/pause"),
        // natywny pomocnik bramy pulpitu: CGEvent, ScreenCaptureKit, AX dla desktop.py
        .executableTarget(name: "claude-acc-desktop", path: "Sources/desktop", swiftSettings: settings),
    ],
    swiftLanguageModes: [.v6]
)
