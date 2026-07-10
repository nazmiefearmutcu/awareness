import XCTest
@testable import AwarenessCore

final class PortUtilsTests: XCTestCase {
    func testPickFreePortReturnsPreferredWhenFree() {
        // High ephemeral-ish port is almost certainly free for bind test.
        let preferred = 58_731
        // Free the port if something weird holds it.
        let picked = PortUtils.pickFreePort(preferred: preferred)
        XCTAssertGreaterThanOrEqual(picked, preferred)
        XCTAssertLessThanOrEqual(picked, preferred + 40)
    }

    func testIsPortInUseFalseForHighUnusedPort() {
        // Not a guarantee, but 0 is invalid / reserved handling covered by pick.
        let freeish = PortUtils.pickFreePort(preferred: 59_001)
        // After pickFreePort the bind is released — port should be free again.
        XCTAssertFalse(PortUtils.isPortInUse(freeish))
    }

    func testHealthPathURL() {
        let cfg = AppConfig(preferredHost: "127.0.0.1", preferredPort: 8085)
        XCTAssertEqual(cfg.healthURL().absoluteString, "http://127.0.0.1:8085/healthz")
        XCTAssertEqual(cfg.baseURL(port: 9090).absoluteString, "http://127.0.0.1:9090/")
    }
}
