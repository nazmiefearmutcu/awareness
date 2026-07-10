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
            // Repo-local launcher (signal-safe shell wrapper).
            out.append(
                URL(fileURLWithPath: repo)
                    .appendingPathComponent("macos/Awareness/Scripts/awareness-api-launcher.sh")
            )
        }
        // Bundled launcher next to the .app binary: …/Awareness.app/Contents/Resources/
        if let arg0 = ProcessInfo.processInfo.arguments.first, !arg0.isEmpty {
            let resources = URL(fileURLWithPath: arg0)
                .deletingLastPathComponent() // MacOS
                .deletingLastPathComponent() // Contents
                .appendingPathComponent("Resources/awareness-api-launcher.sh")
            out.append(resources)
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
        var starts: [String] = []
        if let startingAt {
            starts.append(startingAt)
        }
        // Pinned by build-app.sh / install-app.sh inside the .app bundle.
        if let pinned = readPinnedRepoRoot(fileManager: fileManager) {
            starts.append(pinned)
        }
        starts.append(fileManager.currentDirectoryPath)
        // GUI apps often launch with cwd=/; walk up from the executable
        // (…/dist/Awareness.app/Contents/MacOS → repo root with pyproject.toml).
        if let arg0 = ProcessInfo.processInfo.arguments.first, !arg0.isEmpty {
            starts.append(URL(fileURLWithPath: arg0).deletingLastPathComponent().path)
        }
        // Home symlink used on this machine: ~/awareness_dev
        let home = fileManager.homeDirectoryForCurrentUser
        starts.append(home.appendingPathComponent("awareness_dev").path)
        // Common worktree location for this project.
        starts.append(
            home.appendingPathComponent("awareness_dev/.worktrees/feat-native-macos-app").path
        )

        var seen = Set<String>()
        for start in starts {
            if !seen.insert(start).inserted { continue }
            if let found = walkForRepoRoot(startingAt: start, fileManager: fileManager) {
                return found
            }
        }
        return nil
    }

    /// Contents/Resources/AWARENESS_REPO.txt written at package time.
    public static func readPinnedRepoRoot(fileManager: FileManager = .default) -> String? {
        guard let arg0 = ProcessInfo.processInfo.arguments.first, !arg0.isEmpty else {
            return nil
        }
        let file = URL(fileURLWithPath: arg0)
            .deletingLastPathComponent() // MacOS
            .deletingLastPathComponent() // Contents
            .appendingPathComponent("Resources/AWARENESS_REPO.txt")
        guard fileManager.fileExists(atPath: file.path),
              let raw = try? String(contentsOf: file, encoding: .utf8)
        else {
            return nil
        }
        let path = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty, fileManager.fileExists(atPath: path) else { return nil }
        return path
    }

    private static func walkForRepoRoot(
        startingAt: String,
        fileManager: FileManager
    ) -> String? {
        var dir = URL(fileURLWithPath: startingAt)
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
