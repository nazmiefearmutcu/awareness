# Awareness Desktop (Windows + Linux) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the same Awareness desktop shell (auto-start local API + loopback dashboard WebView) as installable packages for Windows and Linux, and publish them on the GitHub Release.

**Architecture:** Electron main process mirrors the macOS Swift shell: resolve `awareness-api` / project venv, spawn on loopback, poll `/healthz`, load `http://127.0.0.1:PORT/` in a `BrowserWindow`, stop owned process on quit. Packaging via `electron-builder` (NSIS/portable/zip on Windows; AppImage/deb/tar.gz on Linux). Binaries built on GitHub Actions native runners and attached to the release.

**Tech Stack:** Electron 34+, electron-builder 25+, Node 22, existing Python `awareness-api` backend (not bundled — same contract as macOS).

**Worktree:** `/Users/nazmi/awareness_dev/.worktrees/feat-desktop-win-linux`  
**Branch:** `feat/desktop-win-linux` (from `feat/native-macos-app`)  
**Release target:** `v0.3.0` on `nazmiefearmutcu/awareness`

---

## File map

| Path | Responsibility |
|------|----------------|
| `desktop/package.json` | Electron app metadata + electron-builder targets |
| `desktop/main.cjs` | App lifecycle, window, menu, navigation policy |
| `desktop/api-manager.cjs` | Spawn/health/stop awareness-api |
| `desktop/resolve-api.cjs` | Path resolution (env, PATH, ~/awareness_dev, AWARENESS_REPO) |
| `desktop/boot.html` | Loading / error / retry UI (parity with RootView) |
| `desktop/preload.cjs` | Minimal bridge: retry, open-log (contextIsolation) |
| `desktop/icons/` | app icon PNG 256/512 for builder |
| `desktop/README.md` | Build/run/package docs |
| `.github/workflows/desktop-release.yml` | Build Win+Linux on tag/workflow_dispatch; upload assets |
| `docs/superpowers/specs/2026-07-11-desktop-win-linux-design.md` | Short design note |
| `README.md` | Link to desktop packages |

**Non-goals:** Bundle full Python runtime; App Store / Microsoft Store; rewrite SPA; change API contracts.

---

### Task 1: Design note + desktop scaffold

**Files:**
- Create: `docs/superpowers/specs/2026-07-11-desktop-win-linux-design.md`
- Create: `desktop/package.json`
- Create: `desktop/README.md`
- Create: `desktop/boot.html`
- Create: `desktop/preload.cjs`
- Create: `desktop/.gitignore`

- [ ] **Step 1: Write design note** covering Electron shell = macOS parity, env vars, packaging matrix, Python dependency.

- [ ] **Step 2: Create `desktop/package.json`**

```json
{
  "name": "awareness-desktop",
  "version": "0.3.0",
  "description": "Awareness — native desktop shell (Windows/Linux/macOS) for the local awareness-api dashboard",
  "main": "main.cjs",
  "author": {
    "name": "Nazmi Efe Armutcu",
    "email": "nazmiefearmutcu@users.noreply.github.com"
  },
  "license": "MIT",
  "homepage": "https://github.com/nazmiefearmutcu/awareness",
  "private": true,
  "scripts": {
    "start": "electron .",
    "pack": "electron-builder --dir",
    "dist": "electron-builder --publish never",
    "dist:win": "electron-builder --win --publish never",
    "dist:linux": "electron-builder --linux --publish never",
    "dist:mac": "electron-builder --mac --publish never",
    "test:resolve": "node --test test/resolve-api.test.cjs",
    "test:api-manager": "node --test test/api-manager.test.cjs",
    "test": "node --test test/*.test.cjs"
  },
  "devDependencies": {
    "electron": "^34.0.0",
    "electron-builder": "^25.1.8"
  },
  "build": {
    "appId": "dev.awareness.app",
    "productName": "Awareness",
    "copyright": "Copyright © Nazmi Efe Armutcu",
    "directories": {
      "output": "dist-native",
      "buildResources": "icons"
    },
    "files": [
      "main.cjs",
      "api-manager.cjs",
      "resolve-api.cjs",
      "preload.cjs",
      "boot.html",
      "icons/**/*",
      "package.json"
    ],
    "extraMetadata": {
      "main": "main.cjs"
    },
    "win": {
      "target": [
        { "target": "nsis", "arch": ["x64"] },
        { "target": "portable", "arch": ["x64"] },
        { "target": "zip", "arch": ["x64"] }
      ],
      "artifactName": "Awareness-${version}-win-${arch}.${ext}"
    },
    "nsis": {
      "oneClick": false,
      "allowToChangeInstallationDirectory": true,
      "perMachine": false,
      "createDesktopShortcut": true,
      "createStartMenuShortcut": true
    },
    "linux": {
      "category": "Development",
      "target": [
        { "target": "AppImage", "arch": ["x64"] },
        { "target": "deb", "arch": ["x64"] },
        { "target": "tar.gz", "arch": ["x64"] }
      ],
      "artifactName": "Awareness-${version}-linux-${arch}.${ext}",
      "synopsis": "Local public-web awareness engine dashboard",
      "description": "Native shell that starts awareness-api and opens the loopback dashboard."
    },
    "mac": {
      "category": "public.app-category.developer-tools",
      "target": ["dmg", "zip"],
      "artifactName": "Awareness-${version}-mac-${arch}.${ext}",
      "identity": null
    }
  }
}
```

- [ ] **Step 3: Create `desktop/.gitignore`**

```
node_modules/
dist-native/
*.log
.DS_Store
```

- [ ] **Step 4: Create minimal `boot.html`** (dark monochrome, spinner, error + Retry button calling `window.awareness.retry()`).

- [ ] **Step 5: Create `preload.cjs`** exposing `window.awareness = { retry, openLog, onState }` via `contextBridge`.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-07-11-desktop-win-linux-design.md desktop/
git commit -m "feat(desktop): scaffold Electron package for Windows/Linux"
```

---

### Task 2: API resolve + manager (TDD)

**Files:**
- Create: `desktop/resolve-api.cjs`
- Create: `desktop/api-manager.cjs`
- Create: `desktop/test/resolve-api.test.cjs`
- Create: `desktop/test/api-manager.test.cjs`

- [ ] **Step 1: Write failing tests for `resolve-api`**

```js
// test/resolve-api.test.cjs
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");
const { resolveApiLaunch, detectRepoRoot } = require("../resolve-api.cjs");

describe("resolveApiLaunch", () => {
  it("prefers AWARENESS_API_BIN when executable", () => {
    const bin = process.execPath; // always executable
    const r = resolveApiLaunch({
      env: { AWARENESS_API_BIN: bin },
      pathEnv: "",
      home: os.homedir(),
      platform: process.platform,
      exists: (p) => p === bin,
      isExecutable: (p) => p === bin,
    });
    assert.equal(r.kind, "bin");
    assert.equal(r.command, bin);
  });

  it("uses repo/.venv/bin/awareness-api when present", () => {
    const repo = "/tmp/fake-awareness-repo";
    const venvBin = path.join(repo, ".venv", "bin", "awareness-api");
    const r = resolveApiLaunch({
      env: { AWARENESS_REPO: repo },
      pathEnv: "",
      home: "/tmp",
      platform: "linux",
      exists: (p) => p === venvBin || p === path.join(repo, "pyproject.toml"),
      isExecutable: (p) => p === venvBin,
      readFile: (p) => (p.endsWith("pyproject.toml") ? "name = \"awareness\"\n" : ""),
    });
    assert.equal(r.kind, "bin");
    assert.equal(r.command, venvBin);
    assert.equal(r.cwd, repo);
  });

  it("falls back to launcher script + python -c run", () => {
    const repo = "/tmp/fake-awareness-repo";
    const py = "/tmp/fake-python";
    const r = resolveApiLaunch({
      env: { AWARENESS_REPO: repo },
      pathEnv: "",
      home: "/tmp",
      platform: "linux",
      exists: (p) =>
        p === path.join(repo, "pyproject.toml") ||
        p === path.join(repo, ".venv", "bin", "python") ||
        p === path.join(repo, "src"),
      isExecutable: (p) => p === path.join(repo, ".venv", "bin", "python"),
      readFile: (p) => (p.endsWith("pyproject.toml") ? "[project]\nname = \"awareness\"\n" : ""),
    });
    assert.ok(r);
    assert.equal(r.kind, "python-module");
    assert.match(r.command, /python/);
  });
});
```

- [ ] **Step 2: Implement `resolve-api.cjs`** so tests pass. Export:
  - `detectRepoRoot({ env, home, exists, readFile, starts })`
  - `resolveApiLaunch({ env, pathEnv, home, platform, exists, isExecutable, readFile })`
  - Windows: prefer `.venv/Scripts/awareness-api.exe` and `python.exe`
  - Candidates order: `AWARENESS_API_BIN` → `$REPO/.venv/.../awareness-api` → PATH `awareness-api` → python module `from awareness.api.server import run; run()` with `PYTHONPATH=src`

- [ ] **Step 3: Write api-manager unit tests** with injectable `fetch` + mock child_process:
  - attach if health OK on preferred port
  - spawn when not healthy
  - fail when no launch candidate
  - stop sends SIGTERM/kill on Windows via `taskkill`/`proc.kill`

- [ ] **Step 4: Implement `api-manager.cjs`**

Public API:

```js
class APIManager {
  constructor(opts = {}) {}
  async start() {}
  async stop() {}
  async restart() {}
  get state() {} // { status, port?, owned?, detail? }
  on(event, cb) {} // 'state'
}
```

Behavior (match macOS):
1. preferred host/port from `AW_API_HOST` / `AW_API_PORT` (default 127.0.0.1:8085)
2. if GET `http://host:port/healthz` OK → attach (owned=false)
3. else resolve launch, spawn with env `AW_API_HOST`, `AW_API_PORT`, poll every 200ms up to 30s
4. log stdout/stderr to platform log path:
   - Linux: `~/.local/state/Awareness/api.log` (fallback `~/Library/Logs` N/A) — use `~/.local/state/awareness/api.log` or `%APPDATA%/Awareness/logs/api.log` on Windows
5. health monitor every 3s; if owned and unhealthy, restart once path
6. on stop: kill process tree if owned

- [ ] **Step 5: Run tests**

```bash
cd desktop && npm install && npm test
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(desktop): API resolve and process manager with tests"
```

---

### Task 3: Electron main process + boot UI

**Files:**
- Create: `desktop/main.cjs`
- Modify: `desktop/boot.html`, `desktop/preload.cjs`

- [ ] **Step 1: Implement `main.cjs`**
  - `app.whenReady` → create BrowserWindow (1180×820, min 900×640), load `boot.html`
  - start `APIManager`; on ready load `http://127.0.0.1:${port}/`
  - navigation policy: allow only loopback; external http(s) → `shell.openExternal`
  - menu: Reload, Restart API, Open log, Quit
  - window-all-closed: stop API if owned, quit (including on darwin for this multi-platform app when not mac-native path — on linux/win always quit)
  - before-quit: `await api.stop()`

- [ ] **Step 2: Wire boot.html** states via IPC:
  - `starting` → spinner
  - `ready` → main loads dashboard URL (main can loadURL without boot)
  - `failed` / `unhealthy` → message + Retry

- [ ] **Step 3: Smoke run locally**

```bash
cd desktop && npm start
```

Expected: window opens; either attaches to running API or shows clear install error.

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(desktop): Electron main window and boot UI"
```

---

### Task 4: Icons + README + root docs

**Files:**
- Create: `desktop/icons/icon.png` (512×512 — generate from macOS AppIcon.icns or solid monochrome A)
- Create: `desktop/README.md`
- Modify: root `README.md` install section

- [ ] **Step 1: Generate icon** from existing `macos/Awareness/Resources/AppIcon.icns` via `sips`/`iconutil`, or a simple PNG.

```bash
# if icns available:
mkdir -p desktop/icons
sips -s format png macos/Awareness/Resources/AppIcon.icns --out desktop/icons/icon.png -z 512 512
```

- [ ] **Step 2: Document** env vars, `npm start`, GitHub release artifacts, Python prerequisite (same as macOS DMG README).

- [ ] **Step 3: Update root README** with Windows/Linux download table pointing at releases.

- [ ] **Step 4: Commit**

```bash
git commit -am "docs(desktop): icons and install instructions for Win/Linux"
```

---

### Task 5: GitHub Actions desktop release workflow

**Files:**
- Create: `.github/workflows/desktop-release.yml`

- [ ] **Step 1: Workflow**

Triggers: `workflow_dispatch`, `push` tags `v*`

Jobs:
1. `build-linux` on `ubuntu-latest`: checkout, setup-node 22, `cd desktop && npm ci && npm test && npm run dist:linux`, upload-artifact
2. `build-windows` on `windows-latest`: same with `npm run dist:win`
3. `publish` (needs both): if tag ref, `gh release upload` assets to that tag (or create release)

Use `softprops/action-gh-release` or `gh release upload $TAG dist-native/*`.

Also write SHA256SUMS.

- [ ] **Step 2: Commit + push branch**

```bash
git push -u origin feat/desktop-win-linux
```

- [ ] **Step 3: Create tag `v0.3.0` and push** OR run `workflow_dispatch` then attach to new release.

Release notes must list:
- Awareness-0.3.0-win-x64.exe (NSIS)
- Awareness-0.3.0-win-x64.exe (portable if named differently)
- Awareness-0.3.0-linux-x64.AppImage
- Awareness-0.3.0-linux-x64.deb
- Awareness-0.3.0-linux-x64.tar.gz
- Plus existing macOS DMG if rebuilt, or keep v0.2.1 macOS note

Preferred: one unified `v0.3.0` release containing macOS DMG (rebuild/copy) + Win + Linux.

---

### Task 6: Build, publish release, verify

- [ ] **Step 1: Push tag `v0.3.0`** on `feat/desktop-win-linux` (or merge to main first if preferred — match v0.2.1 style: tag from feature branch OK).

- [ ] **Step 2: Watch Actions** until green; confirm assets on release page.

- [ ] **Step 3: Also upload macOS DMG** from previous pipeline or rebuild:

```bash
./macos/Awareness/Scripts/make-dmg.sh
gh release upload v0.3.0 dist/Awareness-0.2.1-arm64.dmg --clobber
# rename/rebuild as Awareness-0.3.0-arm64.dmg if version bumped in Info.plist
```

Bump macOS Info.plist to 0.3.0 for consistency when rebuilding.

- [ ] **Step 4: Verify**

```bash
gh release view v0.3.0 -R nazmiefearmutcu/awareness
```

Expected assets for win + linux present with non-zero sizes.

---

## Verification (end-to-end)

| Check | Command / action |
|-------|------------------|
| Unit tests | `cd desktop && npm test` |
| Local launch | `cd desktop && npm start` |
| Linux artifacts | CI `dist-native/Awareness-0.3.0-linux-*` |
| Windows artifacts | CI `dist-native/Awareness-0.3.0-win-*` |
| Release | GitHub release `v0.3.0` public with assets |

## Parity checklist vs macOS app

- [x] No external browser for dashboard
- [x] Auto-start API / attach if healthy
- [x] Health poll + restart when owned
- [x] Loopback-only navigation
- [x] External links open in OS browser
- [x] Retry on failure
- [x] Log file path documented
- [x] Python engine not bundled (same as macOS design)
