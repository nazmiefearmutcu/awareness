import XCTest
@testable import AwarenessCore

final class AppConfigTests: XCTestCase {
    func testFromEnvironmentReadsPortAndHost() {
        let config = AppConfig.fromEnvironment(env: [
            "AW_API_PORT": "9090",
            "AW_API_HOST": "localhost",
            "AWARENESS_API_BIN": "/tmp/api",
            "AWARENESS_REPO": "/tmp/repo",
        ])
        XCTAssertEqual(config.preferredPort, 9090)
        XCTAssertEqual(config.preferredHost, "localhost")
        XCTAssertEqual(config.apiBinOverride, "/tmp/api")
        XCTAssertEqual(config.repoRootOverride, "/tmp/repo")
        XCTAssertEqual(config.healthPath, "/healthz")
        XCTAssertEqual(config.startupTimeout, 30)
    }

    func testFromEnvironmentDefaults() {
        let config = AppConfig.fromEnvironment(env: [:])
        XCTAssertEqual(config.preferredPort, 8085)
        XCTAssertEqual(config.preferredHost, "127.0.0.1")
        XCTAssertNil(config.apiBinOverride)
        XCTAssertNil(config.repoRootOverride)
    }

    func testFromEnvironmentInvalidPortFallsBack() {
        let config = AppConfig.fromEnvironment(env: ["AW_API_PORT": "not-a-number"])
        XCTAssertEqual(config.preferredPort, 8085)
    }

    func testBaseURLAndHealthURL() {
        let config = AppConfig(preferredHost: "127.0.0.1", preferredPort: 8085)
        XCTAssertEqual(config.baseURL().absoluteString, "http://127.0.0.1:8085/")
        XCTAssertEqual(config.healthURL().absoluteString, "http://127.0.0.1:8085/healthz")
        XCTAssertEqual(config.baseURL(port: 9000).absoluteString, "http://127.0.0.1:9000/")
        XCTAssertEqual(config.healthURL(port: 9000).absoluteString, "http://127.0.0.1:9000/healthz")
    }
}
