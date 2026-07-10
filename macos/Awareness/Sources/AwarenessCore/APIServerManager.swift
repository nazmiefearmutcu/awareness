import Foundation
import Observation

public enum APIServerState: Equatable, Sendable {
    case stopped
    case starting
    case ready(port: Int, owned: Bool)
    case failed(String)
}

@MainActor
@Observable
public final class APIServerManager {
    public private(set) var state: APIServerState = .stopped
    public private(set) var port: Int

    private var process: Process?
    private var config: AppConfig
    private var pollTask: Task<Void, Never>?
    private var logFileHandle: FileHandle?

    public init(config: AppConfig = .fromEnvironment()) {
        self.config = config
        self.port = config.preferredPort
    }

    public func start() async {
        if case .ready = state { return }
        // Cancel any previous poll and clean up a leftover process before restart.
        pollTask?.cancel()
        pollTask = nil
        if let existing = process, existing.isRunning {
            existing.terminate()
        }
        process = nil
        closeLogHandle()

        state = .starting
        let preferred = config.preferredPort

        // Prefer attaching to an already-healthy server on the preferred port.
        if await healthOK(port: preferred) {
            port = preferred
            state = .ready(port: preferred, owned: false)
            return
        }

        // Resolve launch target: dedicated binary, else python3 module fallback.
        guard let launch = resolveLaunch(port: preferred) else {
            state = .failed(
                "Could not find awareness-api. Build the venv (pip install -e .) "
                    + "or set AWARENESS_API_BIN to the binary path."
            )
            return
        }

        do {
            try spawn(launch: launch, port: preferred)
        } catch {
            state = .failed("Failed to start awareness-api: \(error.localizedDescription)")
            return
        }

        let deadline = Date().addingTimeInterval(config.startupTimeout)
        while Date() < deadline {
            if Task.isCancelled {
                state = .failed("Startup cancelled")
                return
            }
            if await healthOK(port: preferred) {
                port = preferred
                state = .ready(port: preferred, owned: true)
                return
            }
            // Process exited early → fail fast.
            if let process, !process.isRunning {
                let code = process.terminationStatus
                state = .failed(
                    "awareness-api exited early (status \(code)). "
                        + "See ~/Library/Logs/Awareness/api.log"
                )
                self.process = nil
                closeLogHandle()
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000) // 200ms
        }

        // Timed out — tear down owned process.
        if let process, process.isRunning {
            process.terminate()
        }
        self.process = nil
        closeLogHandle()
        state = .failed(
            "awareness-api did not become healthy within \(Int(config.startupTimeout))s. "
                + "See ~/Library/Logs/Awareness/api.log"
        )
    }

    public func stop() {
        pollTask?.cancel()
        pollTask = nil

        if let process {
            let pid = process.processIdentifier
            if pid > 0 {
                // Kill descendants first (shell wrappers / uvicorn reloader kids),
                // then the direct child.
                Self.signalProcessTree(rootPid: pid, signal: SIGTERM)
                process.terminate()
                let deadline = Date().addingTimeInterval(2)
                while process.isRunning, Date() < deadline {
                    Thread.sleep(forTimeInterval: 0.05)
                }
                if process.isRunning {
                    Self.signalProcessTree(rootPid: pid, signal: SIGKILL)
                }
            }
        }
        self.process = nil
        closeLogHandle()
        state = .stopped
    }

    /// Best-effort recursive signal via `pkill -P` (macOS).
    private static func signalProcessTree(rootPid: Int32, signal: Int32) {
        let sigName = signal == SIGKILL ? "KILL" : "TERM"
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = [
            "-c",
            // Depth-first: kill children, then root.
            "pkill -\(sigName) -P \(rootPid) 2>/dev/null; kill -\(sigName) \(rootPid) 2>/dev/null; true",
        ]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        try? task.run()
        task.waitUntilExit()
    }

    // MARK: - Launch resolution

    private struct Launch {
        let executable: URL
        let arguments: [String]
        let extraEnv: [String: String]
    }

    private func resolveLaunch(port: Int) -> Launch? {
        if let bin = APIProcessResolver.firstExistingExecutable(config: config) {
            return Launch(executable: bin, arguments: [], extraEnv: [:])
        }

        // Fallback: python3 -c "from awareness.api.server import run; run()"
        // only when a repo root is available so PYTHONPATH=src can work.
        guard let repo = config.repoRootOverride
            ?? APIProcessResolver.detectRepoRoot()
        else {
            return nil
        }

        let python = resolvePython3()
        let src = URL(fileURLWithPath: repo).appendingPathComponent("src").path
        var env: [String: String] = ["PYTHONPATH": src]
        // Preserve existing PYTHONPATH entries if present.
        if let existing = ProcessInfo.processInfo.environment["PYTHONPATH"], !existing.isEmpty {
            env["PYTHONPATH"] = "\(src):\(existing)"
        }
        return Launch(
            executable: python,
            arguments: ["-c", "from awareness.api.server import run; run()"],
            extraEnv: env
        )
    }

    private func resolvePython3() -> URL {
        let pathEnv = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/local/bin"
        let fm = FileManager.default
        for dir in pathEnv.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(dir)).appendingPathComponent("python3")
            if fm.isExecutableFile(atPath: candidate.path) {
                return candidate
            }
        }
        return URL(fileURLWithPath: "/usr/bin/python3")
    }

    // MARK: - Spawn

    private func spawn(launch: Launch, port: Int) throws {
        let logURL = try prepareLogFile()
        let repo = config.repoRootOverride ?? APIProcessResolver.detectRepoRoot()

        var env = ProcessInfo.processInfo.environment
        env["AW_API_HOST"] = config.preferredHost
        env["AW_API_PORT"] = String(port)
        // GUI apps inherit a tiny PATH; expand so venv tools and Homebrew python resolve.
        let guiPathExtras = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin",
        ]
        let existingPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = (guiPathExtras + [existingPath]).joined(separator: ":")
        if let repo {
            env["AWARENESS_REPO"] = repo
        }
        for (k, v) in launch.extraEnv {
            env[k] = v
        }

        let proc = Process()
        proc.executableURL = launch.executable
        proc.arguments = launch.arguments
        proc.environment = env
        if let repo {
            proc.currentDirectoryURL = URL(fileURLWithPath: repo)
        }

        let handle = try FileHandle(forWritingTo: logURL)
        // Append: seek to end.
        try handle.seekToEnd()
        proc.standardError = handle
        proc.standardOutput = handle
        logFileHandle = handle

        try proc.run()
        process = proc
    }

    private func prepareLogFile() throws -> URL {
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Awareness", isDirectory: true)
        try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logURL = logs.appendingPathComponent("api.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        return logURL
    }

    private func closeLogHandle() {
        try? logFileHandle?.close()
        logFileHandle = nil
    }

    // MARK: - Health

    private func healthOK(port: Int) async -> Bool {
        let url = config.healthURL(port: port)
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.5
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}
