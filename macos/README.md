# Awareness macOS app

Native SwiftUI shell that starts the Python `awareness-api` backend and loads the dashboard in a `WKWebView`.

**Requirements**

- macOS 14+
- Swift 5.9+ / Xcode command-line tools
- Python project venv with `awareness-api` installable (repo root `uv` / `pip` env)

## Build `.app` bundle

```bash
# from repo root
./macos/Awareness/Scripts/build-app.sh
```

Produces:

```
dist/Awareness.app/
  Contents/
    Info.plist
    PkgInfo
    MacOS/Awareness
    Resources/
```

Verify:

```bash
test -x dist/Awareness.app/Contents/MacOS/Awareness
plutil -lint dist/Awareness.app/Contents/Info.plist
open dist/Awareness.app
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

The app spawns `awareness-api` on loopback. Install the package into a venv first:

```bash
# from repo root
uv sync   # or: python -m venv .venv && pip install -e .
# ensure .venv/bin/awareness-api (or PATH) is available
```

If resolution fails, set `AWARENESS_API_BIN` explicitly.
