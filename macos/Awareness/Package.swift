// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Awareness",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Awareness", targets: ["AwarenessApp"]),
        .library(name: "AwarenessCore", targets: ["AwarenessCore"]),
    ],
    targets: [
        .target(name: "AwarenessCore", path: "Sources/AwarenessCore"),
        .executableTarget(
            name: "AwarenessApp",
            dependencies: ["AwarenessCore"],
            path: "Sources/AwarenessApp"
        ),
        .testTarget(
            name: "AwarenessTests",
            dependencies: ["AwarenessCore"],
            path: "Tests/AwarenessTests"
        ),
    ]
)
