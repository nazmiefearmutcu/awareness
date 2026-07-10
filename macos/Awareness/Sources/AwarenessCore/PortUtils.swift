import Foundation
import Darwin

/// Loopback TCP port helpers for the local API process.
public enum PortUtils {
    /// Returns true if something is already accepting connections on `127.0.0.1:port`.
    public static func isPortInUse(_ port: Int, host: String = "127.0.0.1") -> Bool {
        guard port > 0, port <= 65_535 else { return true }

        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return true }
        defer { close(fd) }

        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        if host == "127.0.0.1" || host == "localhost" {
            addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        } else if host == "0.0.0.0" || host == "::" {
            addr.sin_addr = in_addr(s_addr: INADDR_ANY.bigEndian)
        } else {
            addr.sin_addr = in_addr(s_addr: inet_addr(host))
        }

        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.bind(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result != 0
    }

    /// Prefer `preferred` when free; otherwise scan upward (wrapping within a small range).
    public static func pickFreePort(preferred: Int, host: String = "127.0.0.1", maxAttempts: Int = 40) -> Int {
        let start = max(1, min(preferred, 65_535))
        for offset in 0..<maxAttempts {
            let candidate = start + offset
            if candidate > 65_535 { break }
            if !isPortInUse(candidate, host: host) {
                return candidate
            }
        }
        // Last resort: ephemeral bind to 0 and read assigned port.
        return bindEphemeral(host: host) ?? start
    }

    private static func bindEphemeral(host: String) -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.bind(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return nil }

        var out = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let ok = withUnsafeMutablePointer(to: &out) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                getsockname(fd, sockPtr, &len)
            }
        }
        guard ok == 0 else { return nil }
        return Int(UInt16(bigEndian: out.sin_port))
    }
}
