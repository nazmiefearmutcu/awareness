# Design: Awareness Desktop (Windows + Linux)

**Date:** 2026-07-11  
**Status:** Approved (implementation)  
**Goal:** Ship the same Awareness desktop shell as installable packages for Windows and Linux.

---

## Problem

macOS already has a native SwiftUI + WKWebView shell (`Awareness.app`) that owns the local `awareness-api` process and loads the loopback dashboard. Windows and Linux users still open a browser or start the API by hand. There is no Dock/taskbar app, no process lifecycle ownership, and no installable artifact on GitHub Releases for those platforms.

## Success criteria

1. Double-click / installer launches a desktop window — **not** the system browser for the dashboard chrome.
2. Full SPA parity (same FastAPI static UI served at `http://127.0.0.1:PORT/`).
3. App owns the API process: start on launch, health-check, restart when owned and unhealthy, stop on quit (leave attached APIs alone).
4. Offline / API-down state is visible (boot UI with Retry).
5. Packages build via `electron-builder` and publish on GitHub Release (`v0.3.0` style).
6. Python API contracts and storage are unchanged.

## Non-goals

- Bundling a full Python runtime inside the Electron package (same contract as macOS).
- Rewriting the SPA or changing REST contracts.
- App Store / Microsoft Store distribution.
- Replacing the macOS Swift app (Electron is the cross-platform shell; macOS may still prefer the native `.app`).

## Chosen approach

**Electron main process + BrowserWindow**, mirroring the macOS shell:

| macOS (Swift) | Electron (this package) |
|---|---|
| `APIProcessResolver` | `desktop/resolve-api.cjs` |
| `APIServerManager` | `desktop/api-manager.cjs` |
| `RootView` loading / error | `boot.html` + preload bridge |
| `DashboardWebView` | `BrowserWindow` → loopback URL |
| Menu Restart / Open log | Application menu |

Electron is heavier than pure native UI but gives one codebase for Windows + Linux packaging, reuses the proven SPA, and matches lifecycle semantics already validated on macOS.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Awareness (Electron)                           │
│  ┌──────────────┐  ┌──────────────────────────┐ │
│  │ Menu         │  │ BrowserWindow            │ │
│  │              │  │  · boot.html (loading)   │ │
│  │              │  │  · dashboard URL ready   │ │
│  └──────────────┘  └────────────▲─────────────┘ │
│                                 │ http://127.0.0.1:PORT/
│  ┌──────────────────────────────┴─────────────┐ │
│  │ APIManager                                 │ │
│  │  · resolveApiLaunch (env / venv / PATH)    │ │
│  │  · spawn subprocess (env AW_API_*)         │ │
│  │  · poll GET /healthz                       │ │
│  │  · terminate on app quit if owned          │ │
│  └────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
          │
          ▼
   awareness-api (existing FastAPI + static SPA)
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AW_API_HOST` | `127.0.0.1` | Bind / attach host (loopback) |
| `AW_API_PORT` | `8085` | Preferred port |
| `AWARENESS_API_BIN` | _(auto)_ | Explicit path to `awareness-api` executable |
| `AWARENESS_REPO` | _(auto)_ | Repo root for venv / `PYTHONPATH=src` discovery |

Also used by the Python process once spawned: same `AW_API_*` contract as the CLI / macOS shell.

## Python prerequisite

The desktop shell **does not** embed CPython or project dependencies. End users must have one of:

1. `awareness-api` on `PATH` (or `AWARENESS_API_BIN`), or
2. A cloned repo with a working venv (`.venv/bin/awareness-api` / `.venv/Scripts/awareness-api.exe`) and/or importable package under `src/`, discoverable via `AWARENESS_REPO` or `~/awareness_dev`.

This matches the macOS DMG policy (`RELEASE=1` packages do not pin a machine-local repo path).

## Resolve order (`resolveApiLaunch`)

1. `AWARENESS_API_BIN` if executable  
2. `$REPO/.venv/bin/awareness-api` (Unix) or `$REPO/.venv/Scripts/awareness-api.exe` (Windows)  
3. `awareness-api` / `awareness-api.exe` on `PATH`  
4. Python module fallback: venv `python` / `python3` with  
   `args = ["-c", "from awareness.api.server import run; run()"]`,  
   `cwd = repo`, `env.PYTHONPATH = <repo>/src`

Repo root detection: `AWARENESS_REPO` (if `pyproject.toml` contains `"awareness"`), walk from `starts` / cwd / argv, then `~/awareness_dev` and `%USERPROFILE%/awareness_dev`.

## Lifecycle

1. Launch → show `boot.html` → `APIManager.start()`.
2. If `/healthz` already OK on preferred port → attach (`owned: false`).
3. Else spawn → poll every **200 ms** up to **30 s**.
4. Ready → `loadURL(http://127.0.0.1:PORT/)`.
5. Health monitor every ~3 s; if owned and unhealthy, attempt restart.
6. Quit → if owned, SIGTERM (Windows: `taskkill` / `proc.kill`) then force-kill after grace; if attached, leave running.

## Logs

| Platform | Path |
|---|---|
| Windows | `%APPDATA%/Awareness/logs/api.log` |
| Linux | `~/.local/state/awareness/api.log` |
| macOS (Electron path) | `~/Library/Logs/Awareness/api.log` |

## Security

- Bind / navigate loopback only for app chrome.
- External `http(s)` links open via `shell.openExternal`.
- `contextIsolation: true`; preload exposes only `retry`, `openLog`, `onState`.

## Packaging matrix (`electron-builder`)

| OS | Targets | Artifact name |
|---|---|---|
| Windows x64 | NSIS, portable, zip | `Awareness-${version}-win-${arch}.${ext}` |
| Linux x64 | AppImage, deb, tar.gz | `Awareness-${version}-linux-${arch}.${ext}` |
| macOS (optional) | dmg, zip | `Awareness-${version}-mac-${arch}.${ext}` |

Output directory: `desktop/dist-native/`.  
App id: `dev.awareness.app`, product name: **Awareness**, package version **0.3.0**.

CI (separate task): GitHub Actions native runners build Win + Linux and upload assets to the release tag.

## Testing

| Layer | What |
|---|---|
| Node unit (`node:test`) | `resolve-api` candidate order; `APIManager` attach / spawn / fail / stop with injectable deps |
| Manual | `npm start` — window + attach or clear install error |
| CI | `npm test` then `npm run dist:win` / `dist:linux` |

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| No Python / venv on user machine | Clear `failed` UI + README prerequisite |
| Port busy with foreign process | Attach only on healthy `/healthz`; otherwise spawn (prefer free port if needed) |
| Slow API start | 30 s poll window + spinner state |
| Electron size | Acceptable for Win/Linux; macOS keeps lighter Swift shell |

## Acceptance checklist

- [x] Design documents Electron = macOS parity  
- [ ] Unit tests for resolve + manager  
- [ ] Boot UI + main window load dashboard  
- [ ] Icons + desktop README + root install links  
- [ ] Packages on GitHub Release (follow-up CI task)  
