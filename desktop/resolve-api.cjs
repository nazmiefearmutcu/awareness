"use strict";

const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

/** @param {string} platform */
function pathMod(platform) {
  return platform === "win32" ? path.win32 : path.posix;
}

/**
 * Normalize / absolutize a path for a target platform without using the host
 * path module (so win32 resolution works under tests on macOS/Linux).
 * @param {string} platform
 * @param {string} p
 */
function resolveUserPath(platform, p) {
  const mod = pathMod(platform);
  const s = String(p);
  if (platform === "win32") {
    if (/^[A-Za-z]:[\\/]/.test(s) || s.startsWith("\\\\")) {
      return mod.normalize(s);
    }
    return mod.normalize(s);
  }
  if (mod.isAbsolute(s)) return mod.normalize(s);
  // Relative: resolve against cwd with host semantics for real runs
  return path.resolve(s);
}

/** @param {string} platform @param {...string} parts */
function joinP(platform, ...parts) {
  return pathMod(platform).join(...parts);
}

function defaultExists(p) {
  try {
    return fs.existsSync(p);
  } catch {
    return false;
  }
}

function defaultIsExecutable(p) {
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    // On Windows, X_OK is often unreliable; treat existing .exe as executable.
    if (process.platform === "win32" && defaultExists(p)) return true;
    return false;
  }
}

function defaultReadFile(p) {
  return fs.readFileSync(p, "utf8");
}

/**
 * Walk upward looking for pyproject.toml that mentions "awareness".
 * @param {string} startingAt
 * @param {{ exists?: Function, readFile?: Function, platform?: string }} [opts]
 * @returns {string|null}
 */
function walkForRepoRoot(startingAt, opts = {}) {
  const exists = opts.exists || defaultExists;
  const readFile = opts.readFile || defaultReadFile;
  const platform = opts.platform || process.platform;
  const mod = pathMod(platform);
  let dir = resolveUserPath(platform, startingAt);
  for (let i = 0; i < 16; i++) {
    const py = joinP(platform, dir, "pyproject.toml");
    if (exists(py)) {
      let data = "";
      try {
        data = readFile(py);
      } catch {
        data = "";
      }
      if (String(data).toLowerCase().includes("awareness")) {
        return dir;
      }
    }
    const parent = mod.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

/**
 * Detect Awareness repo root.
 * @param {object} [opts]
 * @param {Record<string,string>} [opts.env]
 * @param {string} [opts.home]
 * @param {Function} [opts.exists]
 * @param {Function} [opts.readFile]
 * @param {string[]} [opts.starts]
 * @returns {string|null}
 */
function detectRepoRoot(opts = {}) {
  const env = opts.env || process.env;
  const home = opts.home != null ? opts.home : os.homedir();
  const exists = opts.exists || defaultExists;
  const readFile = opts.readFile || defaultReadFile;
  const platform = opts.platform || process.platform;

  // Explicit env: only accept if it looks like the awareness project.
  const envRepo = env.AWARENESS_REPO;
  if (envRepo && String(envRepo).trim()) {
    const root = resolveUserPath(platform, String(envRepo).trim());
    const py = joinP(platform, root, "pyproject.toml");
    if (exists(py)) {
      try {
        const data = readFile(py);
        if (String(data).toLowerCase().includes("awareness")) {
          return root;
        }
      } catch {
        /* fall through */
      }
    }
  }

  const starts = [];
  if (Array.isArray(opts.starts)) {
    for (const s of opts.starts) {
      if (s) starts.push(s);
    }
  }
  // Host cwd / argv only when detecting for the real platform
  if (platform === process.platform) {
    starts.push(process.cwd());
    if (process.argv[1]) {
      starts.push(path.dirname(path.resolve(process.argv[1])));
    }
  }
  starts.push(joinP(platform, home, "awareness_dev"));
  // Windows-style USERPROFILE path when home differs (tests inject home).
  if (env.USERPROFILE) {
    starts.push(joinP(platform, env.USERPROFILE, "awareness_dev"));
  }

  const seen = new Set();
  for (const start of starts) {
    if (!start) continue;
    const key = resolveUserPath(platform, start);
    if (seen.has(key)) continue;
    seen.add(key);
    const found = walkForRepoRoot(start, { exists, readFile, platform });
    if (found) return found;
  }
  return null;
}

function pathSeparator(platform) {
  return platform === "win32" ? ";" : ":";
}

function pathDirs(pathEnv, platform) {
  if (!pathEnv) return [];
  return String(pathEnv)
    .split(pathSeparator(platform))
    .map((d) => d.trim())
    .filter(Boolean);
}

/**
 * Resolve how to launch awareness-api.
 *
 * Candidate order:
 * 1. AWARENESS_API_BIN if executable
 * 2. repo .venv awareness-api (unix/win)
 * 3. PATH awareness-api
 * 4. python-module: venv python / python3 with -c run()
 *
 * @param {object} [opts]
 * @returns {{ kind: string, command: string, args?: string[], cwd?: string, env?: Record<string,string> } | null}
 */
function resolveApiLaunch(opts = {}) {
  const env = opts.env || process.env;
  const pathEnv = opts.pathEnv != null ? opts.pathEnv : process.env.PATH || "";
  const home = opts.home != null ? opts.home : os.homedir();
  const platform = opts.platform || process.platform;
  const exists = opts.exists || defaultExists;
  const isExecutable = opts.isExecutable || defaultIsExecutable;
  const readFile = opts.readFile || defaultReadFile;

  // 1) Explicit binary override
  const binOverride = env.AWARENESS_API_BIN;
  if (binOverride && String(binOverride).trim()) {
    const bin = resolveUserPath(platform, String(binOverride).trim());
    if (isExecutable(bin) || (exists(bin) && platform === "win32")) {
      return {
        kind: "bin",
        command: bin,
        args: [],
        cwd: undefined,
        env: {},
      };
    }
  }

  // Resolve repo (env or detection)
  let repo = null;
  if (env.AWARENESS_REPO && String(env.AWARENESS_REPO).trim()) {
    const candidate = resolveUserPath(platform, String(env.AWARENESS_REPO).trim());
    const py = joinP(platform, candidate, "pyproject.toml");
    if (exists(py)) {
      try {
        const data = readFile(py);
        if (String(data).toLowerCase().includes("awareness")) {
          repo = candidate;
        }
      } catch {
        /* ignore */
      }
    }
  }
  if (!repo) {
    repo = detectRepoRoot({
      env,
      home,
      exists,
      readFile,
      starts: opts.starts,
      platform,
    });
  }

  // 2) Venv awareness-api
  if (repo) {
    const venvBins =
      platform === "win32"
        ? [
            joinP(platform, repo, ".venv", "Scripts", "awareness-api.exe"),
            joinP(platform, repo, ".venv", "Scripts", "awareness-api"),
          ]
        : [joinP(platform, repo, ".venv", "bin", "awareness-api")];
    for (const venvBin of venvBins) {
      if (isExecutable(venvBin) || (platform === "win32" && exists(venvBin))) {
        return {
          kind: "bin",
          command: venvBin,
          args: [],
          cwd: repo,
          env: {},
        };
      }
    }
  }

  // 3) PATH awareness-api
  const names =
    platform === "win32"
      ? ["awareness-api.exe", "awareness-api.cmd", "awareness-api"]
      : ["awareness-api"];
  for (const dir of pathDirs(pathEnv, platform)) {
    for (const name of names) {
      const candidate = joinP(platform, dir, name);
      if (isExecutable(candidate) || (platform === "win32" && exists(candidate))) {
        return {
          kind: "bin",
          command: candidate,
          args: [],
          cwd: repo || undefined,
          env: {},
        };
      }
    }
  }

  // 4) Python module fallback
  if (repo) {
    const src = joinP(platform, repo, "src");
    const pythonCandidates =
      platform === "win32"
        ? [
            joinP(platform, repo, ".venv", "Scripts", "python.exe"),
            joinP(platform, repo, ".venv", "Scripts", "python"),
          ]
        : [
            joinP(platform, repo, ".venv", "bin", "python"),
            joinP(platform, repo, ".venv", "bin", "python3"),
          ];

    let python = null;
    for (const p of pythonCandidates) {
      if (isExecutable(p) || (platform === "win32" && exists(p))) {
        python = p;
        break;
      }
    }
    if (!python) {
      // PATH python3 / python
      const pyNames = platform === "win32" ? ["python.exe", "python"] : ["python3", "python"];
      for (const dir of pathDirs(pathEnv, platform)) {
        for (const name of pyNames) {
          const candidate = joinP(platform, dir, name);
          if (isExecutable(candidate) || (platform === "win32" && exists(candidate))) {
            python = candidate;
            break;
          }
        }
        if (python) break;
      }
    }
    if (!python && platform !== "win32") {
      // Common absolute fallbacks (only if executable)
      for (const p of ["/usr/bin/python3", "/usr/local/bin/python3"]) {
        if (isExecutable(p)) {
          python = p;
          break;
        }
      }
    }

    if (python) {
      const pyPath = exists(src) ? src : joinP(platform, repo, "src");
      const sep = platform === "win32" ? ";" : ":";
      let pythonPath = pyPath;
      if (env.PYTHONPATH && String(env.PYTHONPATH).trim()) {
        pythonPath = `${pyPath}${sep}${env.PYTHONPATH}`;
      }
      return {
        kind: "python-module",
        command: python,
        args: ["-c", "from awareness.api.server import run; run()"],
        cwd: repo,
        env: { PYTHONPATH: pythonPath },
      };
    }
  }

  return null;
}

/**
 * Platform-specific API log path.
 * @param {{ platform?: string, home?: string, env?: Record<string,string> }} [opts]
 */
function defaultLogPath(opts = {}) {
  const platform = opts.platform || process.platform;
  const home = opts.home != null ? opts.home : os.homedir();
  const env = opts.env || process.env;
  if (platform === "win32") {
    const appData = env.APPDATA || joinP("win32", home, "AppData", "Roaming");
    return joinP("win32", appData, "Awareness", "logs", "api.log");
  }
  if (platform === "darwin") {
    return joinP("darwin", home, "Library", "Logs", "Awareness", "api.log");
  }
  // Linux and others — XDG state
  const xdg = env.XDG_STATE_HOME || joinP("linux", home, ".local", "state");
  return joinP("linux", xdg, "awareness", "api.log");
}

module.exports = {
  detectRepoRoot,
  resolveApiLaunch,
  defaultLogPath,
  walkForRepoRoot,
};
