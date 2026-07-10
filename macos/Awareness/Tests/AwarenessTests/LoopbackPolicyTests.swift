import XCTest
@testable import AwarenessCore

final class LoopbackPolicyTests: XCTestCase {
    func testAllowsLoopbackHosts() {
        XCTAssertTrue(LoopbackPolicy.isAllowedNavigation(URL(string: "http://127.0.0.1:8085/")!))
        XCTAssertTrue(LoopbackPolicy.isAllowedNavigation(URL(string: "http://localhost:8085/healthz")!))
        XCTAssertTrue(LoopbackPolicy.isAllowedNavigation(URL(string: "https://127.0.0.1/")!))
        XCTAssertTrue(LoopbackPolicy.isAllowedNavigation(URL(string: "http://[::1]:8085/")!))
        XCTAssertTrue(LoopbackPolicy.isAllowedNavigation(URL(string: "about:blank")!))
    }

    func testRejectsExternalHosts() {
        XCTAssertFalse(LoopbackPolicy.isAllowedNavigation(URL(string: "http://example.com/")!))
        XCTAssertFalse(LoopbackPolicy.isAllowedNavigation(URL(string: "https://evil.example/")!))
        XCTAssertFalse(LoopbackPolicy.isAllowedNavigation(URL(string: "http://192.168.1.1/")!))
        XCTAssertFalse(LoopbackPolicy.isAllowedNavigation(URL(string: "file:///etc/passwd")!))
        XCTAssertFalse(LoopbackPolicy.isAllowedNavigation(URL(string: "ftp://localhost/")!))
    }

    func testOutboundHttpDetection() {
        XCTAssertTrue(LoopbackPolicy.isOutboundHttp(URL(string: "http://example.com/")!))
        XCTAssertTrue(LoopbackPolicy.isOutboundHttp(URL(string: "https://cdn.example.com/a.js")!))
        XCTAssertFalse(LoopbackPolicy.isOutboundHttp(URL(string: "http://127.0.0.1:8085/")!))
        XCTAssertFalse(LoopbackPolicy.isOutboundHttp(URL(string: "http://localhost/")!))
        XCTAssertFalse(LoopbackPolicy.isOutboundHttp(URL(string: "about:blank")!))
        XCTAssertFalse(LoopbackPolicy.isOutboundHttp(URL(string: "file:///tmp")!))
    }
}
