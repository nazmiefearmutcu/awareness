import XCTest
@testable import AwarenessCore

final class APIProcessResolverTests: XCTestCase {
    func testDetectRepoRootFindsPyproject() throws {
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }

        try "[project]\nname = \"awareness\"\n".write(
            to: tmp.appendingPathComponent("pyproject.toml"),
            atomically: true,
            encoding: .utf8
        )

        let nested = tmp.appendingPathComponent("macos/Awareness")
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)

        let found = APIProcessResolver.detectRepoRoot(startingAt: nested.path)
        XCTAssertEqual(found, tmp.path)
    }

    func testCandidateBinariesIncludesOverride() {
        let config = AppConfig(
            preferredHost: "127.0.0.1",
            preferredPort: 8085,
            apiBinOverride: "/opt/custom/awareness-api",
            repoRootOverride: nil
        )
        let bins = APIProcessResolver.candidateBinaries(
            config: config,
            pathEnv: nil,
            fileManager: .default
        )
        XCTAssertEqual(bins.first?.path, "/opt/custom/awareness-api")
        XCTAssertTrue(bins.contains { $0.path == "/opt/custom/awareness-api" })
    }

    func testCandidateBinariesIncludesVenvPath() {
        let config = AppConfig(
            preferredHost: "127.0.0.1",
            preferredPort: 8085,
            apiBinOverride: nil,
            repoRootOverride: "/tmp/fake-awareness-repo"
        )
        let bins = APIProcessResolver.candidateBinaries(
            config: config,
            pathEnv: nil,
            fileManager: .default
        )
        XCTAssertTrue(
            bins.contains { $0.path == "/tmp/fake-awareness-repo/.venv/bin/awareness-api" },
            "expected venv path in candidates, got: \(bins.map(\.path))"
        )
    }
}
