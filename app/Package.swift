// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ClaudeAcc",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "ClaudeAcc", path: "Sources/ClaudeAcc")
    ],
    swiftLanguageModes: [.v5]
)
