# Native macOS Awareness App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Awareness as a native macOS `.app` that hosts the existing SPA in WKWebView and owns the local FastAPI process lifecycle — no external browser.

**Architecture:** SwiftUI app shell + `APIServerManager` subprocess + `WKWebView` loading `http://127.0.0.1:{port}/`. CLI `dashboard` opens the app via `open -a` / `open <path>`.

**Tech Stack:** Swift 6 / SwiftUI / WebKit / Foundation Process; existing Python FastAPI SPA; `xcodebuild` or `swift build` + packaging script.

**Spec:** `docs/superpowers/specs/2026-07-10-native-macos-app-design.md`

**Work directory:** repo root (use git worktree `feat/native-macos-app` when isolated).

---

## File map

| Path | Role |
|------|------|
| `macos/Awareness/Package.swift` | SwiftPM package (macOS 14+) |
| `macos/Awareness/Sources/Awareness/AwarenessApp.swift` | `@main` entry |
| `macos/Awareness/Sources/Awareness/AppConfig.swift` | ports, paths, env |
| `macos/Awareness/Sources/Awareness/APIServerManager.swift` | process + health |
| `macos/Awareness/Sources/Awareness/DashboardWebView.swift` | WKWebView bridge |
| `macos/Awareness/Sources/Awareness/RootView.swift` | loading / error / web |
| `macos/Awareness/Sources/Awareness/LoopbackPolicy.swift` | URL allowlist helpers |
| `macos/Awareness/Tests/AwarenessTests/LoopbackPolicyTests.swift` | unit tests |
| `macos/Awareness/Tests/AwarenessTests/AppConfigTests.swift` | unit tests |
| `macos/Awareness/Scripts/build-app.sh` | produce `dist/Awareness.app` |
| `macos/Awareness/Resources/Info.plist` | bundle metadata |
| `macos/README.md` | build & run docs |
| `src/awareness/cli/main.py` | `dashboard` opens native app |
| `tests/unit/test_cli_dashboard_native.py` | CLI unit tests |

---

### Task 1: Swift package scaffold + LoopbackPolicy + tests

**Files:**
- Create: `macos/Awareness/Package.swift`
- Create: `macos/Awareness/Sources/Awareness/LoopbackPolicy.swift`
- Create: `macos/Awareness/Sources/Awareness/AppConfig.swift`
- Create: `macos/Awareness/Tests/AwarenessTests/LoopbackPolicyTests.swift`
- Create: `macos/Awareness/Tests/AwarenessTests/AppConfigTests.swift`

- [ ] **Step 1: Create Package.swift**

```swift
// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Awareness",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Awareness", targets: ["Awareness"]),
    ],
    targets: [
        .executableTarget(
            name: "Awareness",
            path: "Sources/Awareness",
            resources: [.copy("Resources")] // only if Resources live under Sources; else omit and handle in build script
        ),
        .testTarget(
            name: "AwarenessTests",
            dependencies: ["Awareness"],
            path: "Tests/AwarenessTests"
        ),
    ]
)
```

Note: For executable + testable library split, prefer:

```swift
// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Awareness",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Awareness", targets: ["AwarenessApp"]),
        .library(name: "AwarenessCore", targets: ["AwarenessCore"]),
    ],
    targets: [
        .target(name: "AwarenessCore", path: "Sources/AwarenessCore"),
        .executableTarget(
            name: "AwarenessApp",
            dependencies: ["AwarenessCore"],
            path: "Sources/AwarenessApp"
        ),
        .testTarget(
            name: "AwarenessTests",
            dependencies: ["AwarenessCore"],
            path: "Tests/AwarenessTests"
        ),
    ]
)
```

Use the **library + executable** split so XCTest can import `AwarenessCore`.

- [ ] **Step 2: Write failing tests for LoopbackPolicy**

```swift
import XCTest
@testable import AwarenessCore

final class LoopbackPolicyTests: XCTestCase {
    func testAllowsLoopbackHTTP() {
        let u = URL(string: "http://127.0.0.1:8085/")!
        XCTAssertTrue(LoopbackPolicy.isAllowedNavigation(u))
    }

    func testAllowsLocalhost() {
        let u = URL(string: "http://localhost:8085/static/app.js")!
        XCTAssertTrue(LoopbackPolicy.isAllowedNavigation(u))
    }

    func testRejectsExternal() {
        let u = URL(string: "https://example.com/")!
        XCTAssertFalse(LoopbackPolicy.isAllowedNavigation(u))
    }

    func testOutboundIsHttp() {
        XCTAssertTrue(LoopbackPolicy.isOutboundHttp(URL(string: "https://news.ycombinator.com/")!))
        XCTAssertFalse(LoopbackPolicy.isOutboundHttp(URL(string: "file:///etc/passwd")!))
    }
}
```

- [ ] **Step 3: Implement LoopbackPolicy + AppConfig**

`Sources/AwarenessCore/LoopbackPolicy.swift`:

```swift
import Foundation

public enum LoopbackPolicy {
    public static func isAllowedNavigation(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            // allow about:blank
            if url.absoluteString == "about:blank" { return true }
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
```

`Sources/AwarenessCore/AppConfig.swift`:

```swift
import Foundation

public struct AppConfig: Sendable {
    public var preferredHost: String
    public var preferredPort: Int
    public var healthPath: String
    public var apiBinOverride: String?
    public var repoRootOverride: String?
    public var startupTimeout: TimeInterval

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
        baseURL(port: port).appendingPathComponent(healthPath.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
    }
}
```

- [ ] **Step 4: Run tests**

```bash
cd macos/Awareness && swift test
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add macos/Awareness docs/superpowers/specs/2026-07-10-native-macos-app-design.md docs/superpowers/plans/2026-07-10-native-macos-app.md
git commit -m "feat(macos): scaffold AwarenessCore with loopback policy tests"
```

---

### Task 2: APIServerManager (resolve binary, health, spawn, stop)

**Files:**
- Create: `macos/Awareness/Sources/AwarenessCore/APIServerManager.swift`
- Create: `macos/Awareness/Tests/AwarenessTests/APIServerManagerResolveTests.swift`

- [ ] **Step 1: Write tests for binary resolution (pure functions)**

Extract pure helpers testable without spawning:

```swift
// In APIServerManager.swift or APIProcessResolver.swift
public enum APIProcessResolver {
    public static func candidateBinaries(config: AppConfig, fileManager: FileManager = .default) -> [URL] {
        var out: [URL] = []
        if let o = config.apiBinOverride, !o.isEmpty {
            out.append(URL(fileURLWithPath: o))
        }
        if let repo = config.repoRootOverride ?? detectRepoRoot(fileManager: fileManager) {
            let venv = URL(fileURLWithPath: repo).appendingPathComponent(".venv/bin/awareness-api")
            out.append(venv)
        }
        // which awareness-api via PATH — tested with inject later; for unit test only path construction
        return out
    }

    public static func detectRepoRoot(fileManager: FileManager = .default, startingAt: String? = nil) -> String? {
        // Walk up from startingAt or cwd looking for pyproject.toml containing "awareness"
        var dir = URL(fileURLWithPath: startingAt ?? fileManager.currentDirectoryPath)
        for _ in 0..<12 {
            let py = dir.appendingPathComponent("pyproject.toml")
            if fileManager.fileExists(atPath: py.path) {
                if let data = try? String(contentsOf: py, encoding: .utf8),
                   data.contains("awareness") {
                    return dir.path
                }
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return nil
    }
}
```

Test:

```swift
func testDetectRepoRootFindsPyproject() throws {
    // Use temporary directory with pyproject.toml containing name awareness
    let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
    try "[project]\nname = \"awareness\"\n".write(to: tmp.appendingPathComponent("pyproject.toml"), atomically: true, encoding: .utf8)
    let found = APIProcessResolver.detectRepoRoot(startingAt: tmp.path)
    XCTAssertEqual(found, tmp.path)
}
```

- [ ] **Step 2: Implement APIServerManager**

```swift
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

    public init(config: AppConfig = .fromEnvironment()) {
        self.config = config
        self.port = config.preferredPort
    }

    public func start() async {
        if case .ready = state { return }
        state = .starting
        // 1) If health OK on preferred port → ready(owned: false)
        if await healthOK(port: config.preferredPort) {
            port = config.preferredPort
            state = .ready(port: port, owned: false)
            return
        }
        // 2) Resolve binary; spawn with AW_API_HOST/PORT
        // 3) Poll health until timeout
        // 4) Set ready(owned: true) or failed
    }

    public func stop() {
        pollTask?.cancel()
        guard let process, process.isRunning else {
            if case .ready(_, true) = state { /* already dead */ }
            state = .stopped
            return
        }
        process.terminate()
        // wait briefly; interrupt if needed
        process = nil
        state = .stopped
    }

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
```

Implement fully: PATH search via `/usr/bin/which awareness-api`, env inheritance, stderr log file under `~/Library/Logs/Awareness/api.log`.

- [ ] **Step 3: Unit test resolver; smoke that manager starts in `.starting`**

```bash
cd macos/Awareness && swift test
```

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(macos): APIServerManager process lifecycle and health checks"
```

---

### Task 3: SwiftUI app + WKWebView + RootView

**Files:**
- Create: `macos/Awareness/Sources/AwarenessApp/AwarenessApp.swift`
- Create: `macos/Awareness/Sources/AwarenessApp/RootView.swift`
- Create: `macos/Awareness/Sources/AwarenessApp/DashboardWebView.swift`
- Create: `macos/Awareness/Sources/AwarenessCore/LoopbackPolicy.swift` (already)
- Resources Info.plist for build script

- [ ] **Step 1: Implement DashboardWebView**

```swift
import SwiftUI
import WebKit
import AwarenessCore

struct DashboardWebView: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        let wv = WKWebView(frame: .zero, configuration: cfg)
        wv.navigationDelegate = context.coordinator
        wv.setValue(false, forKey: "drawsBackground") // optional
        return wv
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        if context.coordinator.loadedURL != url {
            context.coordinator.loadedURL = url
            webView.load(URLRequest(url: url))
        }
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loadedURL: URL?

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.cancel)
                return
            }
            if LoopbackPolicy.isAllowedNavigation(url) {
                decisionHandler(.allow)
                return
            }
            if LoopbackPolicy.isOutboundHttp(url) {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }
}
```

- [ ] **Step 2: RootView + App**

```swift
import SwiftUI
import AwarenessCore

@main
struct AwarenessApp: App {
    @State private var manager = APIServerManager()

    var body: some Scene {
        WindowGroup("Awareness") {
            RootView(manager: manager)
                .frame(minWidth: 1100, minHeight: 700)
        }
        .defaultSize(width: 1280, height: 840)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

struct RootView: View {
    @Bindable var manager: APIServerManager

    var body: some View {
        Group {
            switch manager.state {
            case .stopped, .starting:
                ProgressView("Starting Awareness API…")
                    .controlSize(.large)
            case .ready(let port, _):
                DashboardWebView(url: AppConfig.fromEnvironment().baseURL(port: port))
            case .failed(let msg):
                VStack(spacing: 12) {
                    Text("API failed to start").font(.title2)
                    Text(msg).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    Button("Retry") { Task { await manager.start() } }
                }
                .padding()
            }
        }
        .task { await manager.start() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in
            manager.stop()
        }
    }
}
```

- [ ] **Step 3: Build executable**

```bash
cd macos/Awareness && swift build -c release
```

Expected: success

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(macos): SwiftUI shell with WKWebView dashboard"
```

---

### Task 4: build-app.sh + Info.plist + README

**Files:**
- Create: `macos/Awareness/Scripts/build-app.sh`
- Create: `macos/Awareness/Resources/Info.plist`
- Create: `macos/README.md`
- Create: `macos/Awareness/Scripts/run-dev.sh`

- [ ] **Step 1: Info.plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Awareness</string>
  <key>CFBundleDisplayName</key><string>Awareness</string>
  <key>CFBundleIdentifier</key><string>dev.awareness.app</string>
  <key>CFBundleVersion</key><string>0.2.0</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Awareness</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
```

- [ ] **Step 2: build-app.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
DIST="$REPO/dist"
APP="$DIST/Awareness.app"
BIN_NAME="AwarenessApp"  # match Package.swift executable target product name "Awareness"

cd "$ROOT"
swift build -c release --product Awareness
BIN="$(swift build -c release --show-bin-path)/Awareness"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Awareness"
cp "$ROOT/Resources/Info.plist" "$APP/Contents/Info.plist"
# PkgInfo
echo -n "APPL????" > "$APP/Contents/PkgInfo"
chmod +x "$APP/Contents/MacOS/Awareness"
echo "Built $APP"
```

Make executable, run, verify structure:

```bash
chmod +x macos/Awareness/Scripts/build-app.sh
./macos/Awareness/Scripts/build-app.sh
test -x dist/Awareness.app/Contents/MacOS/Awareness
```

- [ ] **Step 3: README**

Document build, `open dist/Awareness.app`, env vars, requirement that `awareness-api` is installed (uv sync / venv).

- [ ] **Step 4: Commit**

```bash
git add macos dist/.gitkeep  # do not commit built .app if large; gitignore dist/*.app
git commit -m "feat(macos): package Awareness.app via build-app.sh"
```

Add to `.gitignore`:

```
dist/Awareness.app/
macos/Awareness/.build/
```

---

### Task 5: CLI `dashboard` opens native app (no browser)

**Files:**
- Modify: `src/awareness/cli/main.py` (`dashboard` command ~621–629)
- Create: `tests/unit/test_cli_dashboard_native.py`

- [ ] **Step 1: Write failing test**

```python
"""dashboard opens native app, not webbrowser for SPA."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from awareness.cli.main import app


def test_dashboard_opens_native_app_not_webbrowser(tmp_path):
    runner = CliRunner()
    fake_app = tmp_path / "Awareness.app"
    fake_app.mkdir()
    (fake_app / "Contents").mkdir()
    with (
        patch("awareness.cli.main.webbrowser.open") as wb,
        patch("awareness.cli.main._resolve_native_app_path", return_value=fake_app),
        patch("awareness.cli.main.subprocess.run") as run,
    ):
        run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["dashboard"])
        assert result.exit_code == 0
        wb.assert_not_called()
        run.assert_called()
        args = run.call_args[0][0]
        assert args[0] == "open"
        assert str(fake_app) in args
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd /path/to/repo && PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cli_dashboard_native.py -v
```

- [ ] **Step 3: Implement**

```python
def _resolve_native_app_path() -> Path | None:
    """Locate Awareness.app: env, dist/, /Applications, built products."""
    env = os.environ.get("AWARENESS_APP")
    if env:
        p = Path(env)
        if p.exists():
            return p
    candidates = [
        Path(__file__).resolve().parents[3] / "dist" / "Awareness.app",  # repo root
        Path.home() / "Applications" / "Awareness.app",
        Path("/Applications/Awareness.app"),
    ]
    # also: parents may differ for editable installs — walk for pyproject + dist
    for c in candidates:
        if c.is_dir():
            return c
    return None


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Host (unused when app manages API)"),
    port: int = typer.Option(_default_api_port, "--port", help="Port hint for app env"),
    browser: bool = typer.Option(False, "--browser", help="Force open system browser (legacy)"),
) -> None:
    """Open the Awareness native macOS app (or browser with --browser)."""
    if browser:
        url = f"http://{host}:{port}/"
        rprint(f"[yellow]Opening legacy browser UI: {url}[/yellow]")
        webbrowser.open(url)
        return

    app_path = _resolve_native_app_path()
    if app_path is None:
        rprint("[red]Awareness.app not found.[/red]")
        rprint("Build it: [bold]macos/Awareness/Scripts/build-app.sh[/bold]")
        rprint("Or set [bold]AWARENESS_APP=/path/to/Awareness.app[/bold]")
        rprint("Legacy: [bold]awareness dashboard --browser[/bold]")
        raise typer.Exit(1)

    env = os.environ.copy()
    env["AW_API_HOST"] = host
    env["AW_API_PORT"] = str(port)
    rprint(f"[green]Opening native app:[/green] {app_path}")
    subprocess.run(["open", str(app_path)], check=False, env=env)
```

Note: `open` does not forward env to GUI apps reliably on macOS. Prefer writing port to a small defaults file or launch via:

```python
subprocess.run(["open", "-a", str(app_path)], check=False)
# AND export via `launchctl setenv` is wrong.
# Better: open the executable with env:
subprocess.run(
    ["open", "-n", str(app_path), "--args"],  # limited
)
# Best for env: run the binary directly:
subprocess.Popen(
    [str(app_path / "Contents/MacOS/Awareness")],
    env=env,
    start_new_session=True,
)
```

Use **Popen on the MacOS binary** when path is `.app`, so `AW_API_PORT` is inherited.

- [ ] **Step 4: Tests green + full unit gate subset**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_cli_dashboard_native.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(cli): dashboard launches native Awareness.app"
```

---

### Task 6: Integration verification + docs touch-up

**Files:**
- Modify: `README.md` (short section: Native macOS app)
- Modify: `.gitignore` if needed

- [ ] **Step 1: Build app**

```bash
./macos/Awareness/Scripts/build-app.sh
```

- [ ] **Step 2: Ensure awareness-api works**

```bash
# if not already running
AW_API_PORT=8085 .venv/bin/awareness-api &
# or let the app start it
```

- [ ] **Step 3: Launch app headless check**

```bash
# Launch and curl health after a few seconds
open dist/Awareness.app
sleep 3
curl -sf http://127.0.0.1:8085/healthz
```

If GUI not available in environment, at least verify binary runs:

```bash
timeout 5 dist/Awareness.app/Contents/MacOS/Awareness || true
```

- [ ] **Step 4: Python regression**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q -m "not slow and not smoke" --tb=no
```

- [ ] **Step 5: Update root README** (replace "open browser" with native app instructions)

- [ ] **Step 6: Final commit**

```bash
git commit -am "docs: document native macOS Awareness.app"
```

---

## Spec coverage

| Spec item | Task |
|-----------|------|
| Native window, no browser | 3, 4, 5 |
| Feature parity (SPA) | 3 (WebView) |
| API lifecycle | 2 |
| Offline/failed UI | 3 RootView |
| Buildable | 4 |
| CLI dashboard | 5 |
| Python tests green | 5, 6 |
| Loopback security | 1, 3 |

## Execution

User requested **subagent-driven-development**. Execute tasks 1→6 sequentially with implementer + reviews; do not stop between tasks.
