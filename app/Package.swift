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

// a bare executable's Info.plist in __TEXT,__info_plist: codesign takes the signing identifier from its
// CFBundleIdentifier, so pod-rootd and pod-rootctl keep theirs whoever signs them (the helper's peer
// requirement names them)
func infoPlist(_ target: String) -> [LinkerSetting] {
    let path = Context.packageDirectory + "/Sources/" + target + "/Info.plist"
    return [.unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", path])]
}

let package = Package(
    name: "ClaudeAcc",
    platforms: [.macOS("26.0")],
    dependencies: [
        // claude-acc's state as Swift models; Pod Menu gives its views pod-rootd through AccRootHelper
        .package(url: "https://github.com/outof-place/acc-kit", from: "0.7.0")
    ],
    targets: [
        // the menu bar app
        .executableTarget(
            name: "ClaudeAcc",
            dependencies: ["DictationCore", "PodRootdClient", "SMCKit", .product(name: "AccKit", package: "acc-kit")],
            path: "Sources/ClaudeAcc",
            swiftSettings: settings),
        // dictation without AppKit: text rules, the AI Gateway client, audio math, the right ⌥ trigger
        .target(name: "DictationCore", path: "Sources/DictationCore", swiftSettings: core),
        .testTarget(
            name: "DictationCoreTests", dependencies: ["DictationCore"], path: "Tests/DictationCoreTests",
            resources: [.copy("Fixtures")], swiftSettings: core),
        // the panel's own logic: what a state file or a script's answer means on screen
        .testTarget(
            name: "ClaudeAccTests",
            dependencies: ["ClaudeAcc", "PodRootdCore", .product(name: "AccKit", package: "acc-kit")],
            path: "Tests/ClaudeAccTests", swiftSettings: settings),
        // fan control through the SMC; runs as a root LaunchDaemon, see install-fans.sh
        .executableTarget(name: "fanctl", dependencies: ["SMCKit"], path: "Sources/fanctl", swiftSettings: settings),
        // the SMC and the fans, for fanctl, pod-rootd and the panel (reading needs no root)
        .target(name: "SMCKit", path: "Sources/SMCKit", swiftSettings: settings),
        // the PreToolUse hook's native front: answers most Bash commands without starting Python
        .executableTarget(name: "claude-acc-hook", path: "Sources/hook", swiftSettings: settings),
        // the limit pause hooks after every tool call; plain C, so it starts without a runtime
        .executableTarget(name: "claude-acc-pause", path: "Sources/pause"),
        // natywny pomocnik bramy pulpitu: CGEvent, ScreenCaptureKit, AX dla desktop.py
        .executableTarget(name: "claude-acc-desktop", path: "Sources/desktop", swiftSettings: settings),
        // the program of Pod's launchd agents (SMAppService): HOME from the account, a log in $STATE, acc.py
        .executableTarget(
            name: "pod-acc-run", dependencies: ["PodAccRunCore"], path: "Sources/pod-acc-run", swiftSettings: core),
        .target(name: "PodAccRunCore", path: "Sources/PodAccRunCore", swiftSettings: core),
        .testTarget(
            name: "PodAccRunTests", dependencies: ["PodAccRunCore"], path: "Tests/PodAccRunTests", swiftSettings: core),
        // pod-rootd, Pod's one root helper (docs/pod-rootd.md): the wire types, the client Pod and Pod Menu
        // link, the engine behind a backend protocol, the daemon, and the CLI the scripts call
        .target(name: "PodRootdProtocol", path: "Sources/PodRootdProtocol", swiftSettings: core),
        .target(
            name: "PodRootdClient", dependencies: ["PodRootdProtocol"], path: "Sources/PodRootdClient",
            swiftSettings: core),
        .target(
            name: "PodRootdCore", dependencies: ["PodRootdProtocol"], path: "Sources/PodRootdCore",
            swiftSettings: settings),
        .executableTarget(
            name: "pod-rootd", dependencies: ["PodRootdCore", "SMCKit"], path: "Sources/pod-rootd",
            exclude: ["Info.plist"], swiftSettings: settings, linkerSettings: infoPlist("pod-rootd")),
        .executableTarget(
            name: "pod-rootctl", dependencies: ["PodRootdClient"], path: "Sources/pod-rootctl",
            exclude: ["Info.plist"], swiftSettings: settings, linkerSettings: infoPlist("pod-rootctl")),
        .testTarget(
            name: "PodRootdTests", dependencies: ["PodRootdCore", "PodRootdClient"], path: "Tests/PodRootdTests",
            swiftSettings: settings),
    ],
    swiftLanguageModes: [.v6]
)
