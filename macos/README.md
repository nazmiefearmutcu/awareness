# Awareness macOS app

Native SwiftUI shell that starts the Python `awareness-api` backend and loads the dashboard in a `WKWebView`.

**Requirements**

- macOS 14+
- Swift 5.9+ / Xcode command-line tools
- Python project venv with `awareness-api` installable (repo root `uv` / `pip` env)

## Build & install

```bash
# from repo root
./macos/Awareness/Scripts/build-app.sh
./macos/Awareness/Scripts/install-app.sh   # → ~/Applications/Awareness.app
```

### DMG (redistributable)

```bash
# from repo root — ad-hoc signed arm64/x86_64 DMG for GitHub Releases
./macos/Awareness/Scripts/make-dmg.sh
# → dist/Awareness-<version>-<arch>.dmg
# → dist/Awareness-<version>-<arch>.dmg.sha256
```

`make-dmg.sh` sets `RELEASE=1` so the package does **not** embed a machine-local
`AWARENESS_REPO.txt` path. End users still need the Python engine (`uv sync` or
`pip install -e .`); the app resolves `~/awareness_dev` or `awareness-api` on `PATH`.

Produces:

```
dist/Awareness.app/
  Contents/
    Info.plist
    PkgInfo
    MacOS/Awareness
    Resources/
      awareness-api-launcher.sh   # signal-safe fallback API launcher
dist/Awareness-<ver>-arm64.dmg    # drag Awareness.app → Applications
```

Verify:

```bash
test -x dist/Awareness.app/Contents/MacOS/Awareness
plutil -lint dist/Awareness.app/Contents/Info.plist
open dist/Awareness.app   # native window — no Safari/Chrome
```

CLI (after build):

```bash
awareness dashboard          # opens Awareness.app
awareness dashboard --browser  # legacy browser SPA
```

## Run (development)

```bash
# Package + open if already built, else swift run
./macos/Awareness/Scripts/run-dev.sh

# Or directly:
cd macos/Awareness
swift run Awareness
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AW_API_PORT` | `8085` | Preferred loopback port for the API |
| `AW_API_HOST` | `127.0.0.1` | Host bind (loopback only) |
| `AWARENESS_API_BIN` | _(resolved)_ | Absolute path to `awareness-api` executable |
| `AWARENESS_REPO` | _(inferred)_ | Repo root used for `PYTHONPATH` / cwd |

Example:

```bash
export AWARENESS_REPO="$PWD"
export AWARENESS_API_BIN="$PWD/.venv/bin/awareness-api"
export AW_API_PORT=8085
open dist/Awareness.app
```

## Python backend

The app spawns the API on loopback. Resolution order:

1. `AWARENESS_API_BIN` (explicit)
2. `$AWARENESS_REPO/.venv/bin/awareness-api` (or inferred repo root)
3. `awareness-api` on `PATH`
4. Bundled `Resources/awareness-api-launcher.sh` (uses venv python + `PYTHONPATH=src`; reaps the API if the app dies)

Install deps first:

```bash
# from repo root
uv sync   # or: python -m venv .venv && pip install -e .
```

If the app cannot find a venv, set:

```bash
export AWARENESS_REPO="$PWD"
export AWARENESS_API_BIN="$PWD/.venv/bin/awareness-api"
```

Logs: `~/Library/Logs/Awareness/api.log`
