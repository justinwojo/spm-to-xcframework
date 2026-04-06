// swift-tools-version:5.7
import PackageDescription

// Test fixture for spm-to-xcframework Session 1.
//
// Exercises:
//   - Swift target (MiniSwift)
//   - ObjC target (MiniObjC) with publicHeadersPath
//   - Mixed target (MiniMixed) with both .swift and .m
//   - exclude: list (MiniSwift excludes 'Excluded.txt') so the staging
//     two-pass exclude cleanup has something to delete.
//
// This fixture is intentionally tiny so the self-test fetch run stays fast.

let package = Package(
    name: "MiniMixed",
    platforms: [.iOS(.v15)],
    products: [
        .library(name: "MiniSwift", targets: ["MiniSwift"]),
        .library(name: "MiniObjC", targets: ["MiniObjC"]),
        .library(name: "MiniMixed", targets: ["MiniMixed"]),
    ],
    targets: [
        .target(
            name: "MiniSwift",
            path: "Sources/MiniSwift",
            exclude: ["Excluded.txt"]
        ),
        .target(
            name: "MiniObjC",
            path: "Sources/MiniObjC",
            publicHeadersPath: "include"
        ),
        .target(
            name: "MiniMixed",
            path: "Sources/MiniMixed",
            publicHeadersPath: "include"
        ),
    ]
)
