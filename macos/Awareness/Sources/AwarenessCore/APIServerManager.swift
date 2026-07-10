import Foundation
import Observation

public enum APIServerState: Equatable, Sendable {
    case stopped
    case starting
    case ready(port: Int, owned: Bool)
    case unhealthy(port: Int, detail: String)
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
    private var monitorTask: Task<Void, Never>?
    private var logFileHandle: FileHandle?
    private var owned: Bool = false

    public init(config: AppConfig = .fromEnvironment()) {
        self.config = config
        self.port = config.preferredPort
    }

    public func start() async {
        if case .ready = state { return }
        if case .starting = state { return }

        tearDownProcessOnly()
        state = .starting
        let preferred = config.preferredPort

        // Prefer attaching to an already-healthy server on the preferred port.
        if await healthOK(port: preferred) {
            port = preferred
            owned = false
            state = .ready(port: preferred, owned: false)
            startHealthMonitor()
            return
        }

        // If preferred is busy with a non-healthy service, pick a free port.
        var bindPort = preferred
        if PortUtils.isPortInUse(preferred, host: config.preferredHost) {
            bindPort = PortUtils.pickFreePort(preferred: preferred + 1, host: config.preferredHost)
        }

        guard let launch = resolveLaunch(port: bindPort) else {
            state = .failed(
                "Could not find awareness-api. Install the venv (uv sync / pip install -e .) "
                    + "or set AWARENESS_API_BIN / AWARENESS_REPO."
            )
            return
        }

        do {
            try spawn(launch: launch, port: bindPort)
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
            if await healthOK(port: bindPort) {
                port = bindPort
                owned = true
                state = .ready(port: bindPort, owned: true)
                startHealthMonitor()
                return
            }
            if let process, !process.isRunning {
                let code = process.terminationStatus
                self.process = nil
                closeLogHandle()
                state = .failed(
                    "awareness-api exited early (status \(code)). "
                        + "See ~/Library/Logs/Awareness/api.log"
                )
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }

        tearDownProcessOnly()
        state = .failed(
            "awareness-api did not become healthy within \(Int(config.startupTimeout))s. "
                + "See ~/Library/Logs/Awareness/api.log"
        )
    }

    public func stop() {
        monitorTask?.cancel()
        monitorTask = nil
        pollTask?.cancel()
        pollTask = nil
        tearDownProcessOnly()
        owned = false
        state = .stopped
    }

    /// Force a full restart (used by Retry / menu).
    public func restart() async {
        stop()
        await start()
    }

    // MARK: - Health monitor

    private func startHealthMonitor() {
        monitorTask?.cancel()
        monitorTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard let self else { return }
                await self.checkHealthOnce()
            }
        }
    }

    private func checkHealthOnce() async {
        let currentPort: Int
        switch state {
        case .ready(let p, _):
            currentPort = p
        case .unhealthy(let p, _):
            currentPort = p
        default:
            return
        }

        if await healthOK(port: currentPort) {
            if case .unhealthy = state {
                state = .ready(port: currentPort, owned: owned)
            }
            return
        }

        // Lost health.
        if owned {
            state = .unhealthy(port: currentPort, detail: "API process not responding — restarting…")
            tearDownProcessOnly()
            await startOwnedOnPort(currentPort)
        } else {
            state = .unhealthy(
                port: currentPort,
                detail: "Attached API is down. Retry to start a local instance."
            )
        }
    }

    private func startOwnedOnPort(_ bindPort: Int) async {
        state = .starting
        guard let launch = resolveLaunch(port: bindPort) else {
            state = .failed("Could not find awareness-api after crash.")
            return
        }
        do {
            try spawn(launch: launch, port: bindPort)
        } catch {
            state = .failed("Restart failed: \(error.localizedDescription)")
            return
        }
        let deadline = Date().addingTimeInterval(min(config.startupTimeout, 20))
        while Date() < deadline {
            if await healthOK(port: bindPort) {
                port = bindPort
                owned = true
                state = .ready(port: bindPort, owned: true)
                return
            }
            if let process, !process.isRunning {
                state = .failed("API crashed again. See ~/Library/Logs/Awareness/api.log")
                self.process = nil
                closeLogHandle()
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        tearDownProcessOnly()
        state = .failed("API restart timed out. See ~/Library/Logs/Awareness/api.log")
    }

    // MARK: - Process teardown

    private func tearDownProcessOnly() {
        if let process {
            let pid = process.processIdentifier
            if pid > 0 {
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
    }

    private static func signalProcessTree(rootPid: Int32, signal: Int32) {
        let sigName = signal == SIGKILL ? "KILL" : "TERM"
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = [
            "-c",
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

        guard let repo = config.repoRootOverride
            ?? APIProcessResolver.detectRepoRoot()
        else {
            return nil
        }

        let python = resolvePython3(repo: repo)
        let src = URL(fileURLWithPath: repo).appendingPathComponent("src").path
        var env: [String: String] = ["PYTHONPATH": src]
        if let existing = ProcessInfo.processInfo.environment["PYTHONPATH"], !existing.isEmpty {
            env["PYTHONPATH"] = "\(src):\(existing)"
        }
        return Launch(
            executable: python,
            arguments: ["-c", "from awareness.api.server import run; run()"],
            extraEnv: env
        )
    }

    private func resolvePython3(repo: String) -> URL {
        let candidates = [
            "\(repo)/.venv/bin/python",
            "\(FileManager.default.homeDirectoryForCurrentUser.path)/awareness_dev/.venv/bin/python",
        ]
        let fm = FileManager.default
        for path in candidates where fm.isExecutableFile(atPath: path) {
            return URL(fileURLWithPath: path)
        }
        let pathEnv = ProcessInfo.processInfo.environment["PATH"]
            ?? "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
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
        let guiPathExtras = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin",
            "\(FileManager.default.homeDirectoryForCurrentUser.path)/awareness_dev/.venv/bin",
        ]
        if let repo {
            env["PATH"] = (
                ["\(repo)/.venv/bin"] + guiPathExtras + [env["PATH"] ?? "/usr/bin:/bin"]
            ).joined(separator: ":")
            env["AWARENESS_REPO"] = repo
        } else {
            let existingPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
            env["PATH"] = (guiPathExtras + [existingPath]).joined(separator: ":")
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
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
                return false
            }
            // Prefer JSON bodies that look like our /healthz ({"ok":true,...}).
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let ok = obj["ok"] as? Bool {
                return ok
            }
            // Accept bare 200 if body is empty/non-json (lenient attach).
            return true
        } catch {
            return false
        }
    }
}
