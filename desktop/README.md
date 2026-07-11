# Awareness Desktop (Electron)

Cross-platform desktop shell for the Awareness dashboard. Mirrors the macOS native app: resolves and starts local `awareness-api`, polls `GET /healthz`, loads `http://127.0.0.1:PORT/` in a `BrowserWindow`, and stops the owned process on quit.

**Primary targets:** Windows and Linux (packaged via `electron-builder`). macOS can run the same shell, but the recommended macOS product remains the Swift `Awareness.app` under [`macos/`](../macos/).

## Prerequisites

- **Node.js 20+** (22 recommended) for building and `npm start`
- **Python 3.11+** with the Awareness project installed — the shell does **not** bundle a Python runtime

You need one of:

1. `awareness-api` on `PATH`, or `AWARENESS_API_BIN` pointing at it  
2. A repo checkout with a working venv:
   - Unix: `.venv/bin/awareness-api` or `.venv/bin/python` + importable `src/`
   - Windows: `.venv\Scripts\awareness-api.exe` or `.venv\Scripts\python.exe`

```bash
# from repo root
uv sync
# or: pip install -e .
```

## Run (development)

```bash
cd desktop
npm install
npm start
```

The window opens with a monochrome boot screen, then either:

- attaches to an already-healthy API on the preferred port, or  
- spawns `awareness-api` / python-module fallback, or  
- shows a clear error with **Retry** / **Open API log**.

## Tests

```bash
cd desktop
npm test
```

Unit tests cover `resolve-api` candidate order and `APIManager` attach / spawn / fail / stop (injectable `fetch` + `spawn`).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AW_API_PORT` | `8085` | Preferred loopback port |
| `AW_API_HOST` | `127.0.0.1` | Host bind / attach (loopback) |
| `AWARENESS_API_BIN` | _(auto)_ | Absolute path to `awareness-api` |
| `AWARENESS_REPO` | _(auto)_ | Repo root for venv / `PYTHONPATH` |

## API process resolve order

1. `AWARENESS_API_BIN` if executable  
2. `$REPO/.venv/bin/awareness-api` or `$REPO/.venv/Scripts/awareness-api.exe`  
3. `awareness-api` on `PATH`  
4. Python module: `python -c "from awareness.api.server import run; run()"` with `PYTHONPATH=<repo>/src`

Repo discovery: `AWARENESS_REPO`, walk from cwd / argv, then `~/awareness_dev` (and `%USERPROFILE%\awareness_dev` on Windows).

## Logs

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\Awareness\logs\api.log` |
| Linux | `~/.local/state/awareness/api.log` |
| macOS | `~/Library/Logs/Awareness/api.log` |

Menu: **Awareness → Open API log** (or the boot UI button).

## Package

```bash
cd desktop
npm install
npm run dist:win     # NSIS + portable + zip (x64) — run on Windows or with wine setup
npm run dist:linux   # AppImage + deb + tar.gz (x64)
npm run dist         # current platform defaults
```

Artifacts land in `desktop/dist-native/`:

| Artifact pattern | Format |
|---|---|
| `Awareness-0.3.0-win-x64.exe` | NSIS installer / portable |
| `Awareness-0.3.0-win-x64.zip` | Zip |
| `Awareness-0.3.0-linux-x64.AppImage` | AppImage |
| `Awareness-0.3.0-linux-x64.deb` | Debian package |
| `Awareness-0.3.0-linux-x64.tar.gz` | Tarball |

Official builds are published on [GitHub Releases](https://github.com/nazmiefearmutcu/awareness/releases) (tag `v0.3.0` and later).

**Note:** Packaged apps still require a local Python engine (`awareness-api` or project venv). Same policy as the macOS DMG.

## Menu

| Action | Shortcut |
|---|---|
| Restart API | ⌘/Ctrl+Shift+R |
| Reload | ⌘/Ctrl+R |
| Open API log | — |
| Quit | ⌘Q / Alt+F4 |

## Layout

```
desktop/
  main.cjs           # Electron app lifecycle, window, menu, navigation policy
  api-manager.cjs    # Spawn / health / stop
  resolve-api.cjs    # Path resolution
  preload.cjs        # contextBridge: retry, openLog, onState
  boot.html          # Loading / error / retry UI
  icons/icon.png     # 512×512 app icon
  test/              # node:test unit tests
  package.json
```

## Security

- Dashboard chrome navigates only to loopback HTTP(S).  
- External links open in the system browser (`shell.openExternal`).  
- `contextIsolation: true`, no Node integration in the renderer.
