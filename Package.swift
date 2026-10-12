// swift-tools-version: 6.2
import PackageDescription

// Only the pod-rootd client, so Pod can depend on this repository by URL:
//   .package(url: "https://github.com/outof-place/claude-acc", from: "<version>")
//   .product(name: "PodRootdClient", package: "claude-acc")
// Everything else (the menu bar app, fanctl, the helper itself) builds from app/Package.swift.
let package = Package(
    name: "claude-acc",
    platforms: [.macOS("26.0")],
    products: [
        .library(name: "PodRootdClient", targets: ["PodRootdClient"]),
    ],
    targets: [
        .target(name: "PodRootdProtocol", path: "app/Sources/PodRootdProtocol"),
        .target(name: "PodRootdClient", dependencies: ["PodRootdProtocol"], path: "app/Sources/PodRootdClient"),
    ],
    swiftLanguageModes: [.v6]
)
