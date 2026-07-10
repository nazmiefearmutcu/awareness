import Foundation

public struct AppConfig: Sendable, Equatable {
    public var preferredHost: String
    public var preferredPort: Int
    public var healthPath: String
    public var apiBinOverride: String?
    public var repoRootOverride: String?
    public var startupTimeout: TimeInterval

    public init(
        preferredHost: String,
        preferredPort: Int,
        healthPath: String = "/healthz",
        apiBinOverride: String? = nil,
        repoRootOverride: String? = nil,
        startupTimeout: TimeInterval = 30
    ) {
        self.preferredHost = preferredHost
        self.preferredPort = preferredPort
        self.healthPath = healthPath
        self.apiBinOverride = apiBinOverride
        self.repoRootOverride = repoRootOverride
        self.startupTimeout = startupTimeout
    }

    public static func fromEnvironment(
        env: [String: String] = ProcessInfo.processInfo.environment
    ) -> AppConfig {
        let port = Int(env["AW_API_PORT"] ?? "") ?? 8085
        let host = env["AW_API_HOST"] ?? "127.0.0.1"
        return AppConfig(
            preferredHost: host,
            preferredPort: port,
            healthPath: "/healthz",
            apiBinOverride: env["AWARENESS_API_BIN"],
            repoRootOverride: env["AWARENESS_REPO"],
            startupTimeout: 30
        )
    }

    public func baseURL(port: Int? = nil) -> URL {
        let p = port ?? preferredPort
        return URL(string: "http://\(preferredHost):\(p)/")!
    }

    public func healthURL(port: Int? = nil) -> URL {
        let base = baseURL(port: port)
        let path = healthPath.hasPrefix("/") ? String(healthPath.dropFirst()) : healthPath
        return base.appendingPathComponent(path)
    }
}
