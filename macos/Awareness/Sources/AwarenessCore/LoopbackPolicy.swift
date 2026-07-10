import Foundation

public enum LoopbackPolicy {
    public static func isAllowedNavigation(_ url: URL) -> Bool {
        if url.absoluteString == "about:blank" { return true }
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            return false
        }
        let host = (url.host ?? "").lowercased()
        return host == "127.0.0.1" || host == "localhost" || host == "::1"
    }

    public static func isOutboundHttp(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            return false
        }
        return !isAllowedNavigation(url)
    }
}
