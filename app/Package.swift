// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "ClaudeAcc",
    platforms: [.macOS("26.0")],
    targets: [
        .executableTarget(
            name: "ClaudeAcc",
            path: "Sources/ClaudeAcc",
            swiftSettings: [
                // UI app: everything on the main actor unless it says otherwise (SE-0466)
                .defaultIsolation(MainActor.self),
                .enableUpcomingFeature("NonisolatedNonsendingByDefault"),
                .enableUpcomingFeature("InferIsolatedConformances"),
            ]
        )
    ],
    swiftLanguageModes: [.v6]
)
