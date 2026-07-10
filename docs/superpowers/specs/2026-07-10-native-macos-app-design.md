# Design: Awareness Native macOS App

**Date:** 2026-07-10  
**Status:** Approved (recommended approach; autonomous execution)  
**Goal:** Ship Awareness UI as a real macOS `.app` — no external browser.

---

## Problem

Today the control surface is a static SPA served by FastAPI (`awareness-api` on `127.0.0.1:8085`). The CLI command `awareness dashboard` opens the system browser via `webbrowser.open`. That is not a desktop product: no Dock app, no process lifecycle ownership, window chrome is the browser, and users must start the API separately.

## Success criteria

1. Double-click (or `open -a Awareness` / `awareness dashboard`) launches a native macOS window — **never Safari/Chrome**.
2. Full feature parity with the existing SPA (Dashboard, Captures, Work, Pipeline, Tail, Settings, ⌘K, reader).
3. App owns the API process: start on launch, health-check, restart on crash, stop on quit.
4. Offline/API-down state is visible inside the app (not a blank WebView).
5. Existing Python tests remain green; new macOS packaging is buildable via `xcodebuild` or documented script.
6. CLI `dashboard` opens the native app (falls back to instructions if `.app` not installed).

## Non-goals (this delivery)

- Pure SwiftUI rewrite of every view (Phase 2 optional).
- App Store distribution / notarization pipeline (local + ad-hoc build is enough).
- Bundling a full Python runtime inside the `.app` (uses project venv / `PATH` awareness tools).
- Changing API contracts or storage.

## Approaches considered

| # | Approach | Pros | Cons |
|---|----------|------|------|
| A | Full SwiftUI rewrite of all 6 views | Truly native widgets | High regression risk; weeks of work; parity hard |
| B | **Native SwiftUI shell + WKWebView + managed API** | Zero UI regression; real `.app`; fast; hatasız | Content still web tech inside the window |
| C | Electron / Tauri | Familiar web tooling | Heavy runtime; less “native macOS” |

**Chosen: B.** Matches “tarayıcıda açılma artık yok” and “hatasız” without rewriting ~4k lines of proven UI. Phase 2 can replace WebView routes with SwiftUI incrementally.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Awareness.app (SwiftUI + AppKit)               │
│  ┌──────────────┐  ┌──────────────────────────┐ │
│  │ Menu / Dock  │  │ ContentView              │ │
│  │ Status item  │  │  · Loading / Offline UI  │ │
│  │ (optional)   │  │  · WKWebView → SPA      │ │
│  └──────────────┘  └────────────▲─────────────┘ │
│                                 │ http://127.0.0.1:PORT/
│  ┌──────────────────────────────┴─────────────┐ │
│  │ APIServerManager                           │ │
│  │  · resolve python + awareness-api          │ │
│  │  · spawn subprocess (env AW_API_*)         │ │
│  │  · poll /healthz                           │ │
│  │  · terminate on app terminate              │ │
│  └────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
          │
          ▼
   awareness-api (existing FastAPI + static SPA)
```

### Components

1. **`AwarenessApp`** — `@main` SwiftUI app, window style, activation policy `.regular`.
2. **`APIServerManager`** — `@MainActor` `ObservableObject` / `@Observable`:
   - Resolves binary: `AWARENESS_API_BIN` env → `which awareness-api` → `<repo>/.venv/bin/awareness-api` → `python -m awareness.api.server` if importable.
   - Picks free port if `8085` busy **only when we own the process**; if healthy existing API already on preferred port, attach without spawning.
   - States: `stopped | starting | ready | unhealthy | failed(String)`.
   - Logs stderr to `~/Library/Logs/Awareness/api.log` (or app support).
3. **`DashboardWebView`** — `NSViewRepresentable` wrapping `WKWebView`:
   - Loads `http://127.0.0.1:{port}/` when ready.
   - Navigation policy: allow only loopback; open external `http(s)` links in default browser (user-initiated outbound).
   - Transparent titlebar integration optional (`titlebarAppearsTransparent`).
4. **`RootView`** — shows spinner while starting; error + Retry when failed; WebView when ready.
5. **CLI bridge** — `awareness dashboard` runs `open -a Awareness` or `open path/to/Awareness.app`; if missing, print install/build path (no `webbrowser.open` for the SPA).

### Repo layout

```
macos/
  Awareness/
    Package.swift                 # or Xcode project
    Sources/Awareness/
      AwarenessApp.swift
      APIServerManager.swift
      DashboardWebView.swift
      RootView.swift
      AppConfig.swift
    Resources/
      Assets.xcassets             # AppIcon
      Info.plist
    Scripts/
      build-app.sh                # swift build / xcodebuild → .app in dist/
  README.md                       # how to build & run
```

Python side (minimal):

- `src/awareness/cli/main.py` — `dashboard` command opens native app.
- Optional: keep serving static SPA (required by WebView); no need to delete `api/web/`.

### Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `AW_API_HOST` | `127.0.0.1` | Bind host (loopback only) |
| `AW_API_PORT` | `8085` | Preferred port |
| `AWARENESS_API_BIN` | (auto) | Explicit path to API executable |
| `AWARENESS_REPO` | (auto) | Repo root for venv discovery |

App Support: `~/Library/Application Support/Awareness/` for optional prefs (port override).

### Lifecycle

1. Launch → `APIServerManager.start()`.
2. If `/healthz` already OK on preferred port → use it (`attached` mode).
3. Else spawn API → poll `/healthz` every 200ms up to 30s.
4. Ready → load WebView.
5. Quit → if we spawned the process, SIGTERM then SIGKILL after grace; if attached, leave it running.

### Security

- Bind loopback only.
- WKWebView: disallow navigation off loopback for app chrome; outbound capture URLs open via `NSWorkspace.shared.open`.
- No remote content in the shell itself.

### Testing

| Layer | What |
|-------|------|
| Swift unit (XCTest) | Port parsing, URL allowlist, process arg building (mockable) |
| Manual / script | `build-app.sh` succeeds; launch; `/healthz` path; dashboard CLI |
| Python | Existing suite still green; CLI dashboard unit test mocks `open` |

### Risks & mitigations

| Risk | Mitigation |
|------|------------|
| `awareness-api` not on PATH | Search venv, env override, clear error UI |
| Port already taken by foreign process | Detect non-Awareness response → pick next free port for spawn |
| SPA cache stale | Existing API no-cache middleware for static |
| WebView blank on slow start | Explicit loading state + timeout message |

## Phase 2 (optional, out of scope)

Replace WebView routes with pure SwiftUI views calling the same REST API, view-by-view, behind a feature flag.

## Acceptance checklist

- [ ] `macos/` builds `Awareness.app`
- [ ] App window shows SPA without opening a browser
- [ ] API auto-starts when not already running
- [ ] App quit stops owned API process
- [ ] `awareness dashboard` opens the app, not the browser
- [ ] Python tests still pass
