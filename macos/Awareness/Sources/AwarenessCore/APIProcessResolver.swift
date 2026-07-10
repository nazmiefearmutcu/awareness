import Foundation

public enum APIProcessResolver {
    /// Ordered candidate paths for awareness-api binary.
    public static func candidateBinaries(
        config: AppConfig,
        pathEnv: String? = ProcessInfo.processInfo.environment["PATH"],
        fileManager: FileManager = .default
    ) -> [URL] {
        var out: [URL] = []
        if let o = config.apiBinOverride, !o.isEmpty {
            out.append(URL(fileURLWithPath: o))
        }
        if let repo = config.repoRootOverride ?? detectRepoRoot(fileManager: fileManager) {
            out.append(URL(fileURLWithPath: repo).appendingPathComponent(".venv/bin/awareness-api"))
        }
        // Search PATH for awareness-api
        if let pathEnv {
            for dir in pathEnv.split(separator: ":") {
                let candidate = URL(fileURLWithPath: String(dir)).appendingPathComponent("awareness-api")
                if fileManager.isExecutableFile(atPath: candidate.path) {
                    out.append(candidate)
                }
            }
        }
        // Deduplicate by path
        var seen = Set<String>()
        return out.filter { seen.insert($0.path).inserted }
    }

    public static func firstExistingExecutable(
        config: AppConfig,
        pathEnv: String? = ProcessInfo.processInfo.environment["PATH"],
        fileManager: FileManager = .default
    ) -> URL? {
        for u in candidateBinaries(config: config, pathEnv: pathEnv, fileManager: fileManager) {
            if fileManager.isExecutableFile(atPath: u.path) { return u }
        }
        return nil
    }

    public static func detectRepoRoot(
        fileManager: FileManager = .default,
        startingAt: String? = nil
    ) -> String? {
        var dir = URL(fileURLWithPath: startingAt ?? fileManager.currentDirectoryPath)
        for _ in 0..<16 {
            let py = dir.appendingPathComponent("pyproject.toml")
            if fileManager.fileExists(atPath: py.path),
               let data = try? String(contentsOf: py, encoding: .utf8),
               data.lowercased().contains("awareness") {
                return dir.path
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return nil
    }
}
