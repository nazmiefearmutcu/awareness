"use strict";

const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { spawn: defaultSpawn } = require("node:child_process");
const { resolveApiLaunch, defaultLogPath } = require("./resolve-api.cjs");

/**
 * @typedef {{ status: 'stopped'|'starting'|'ready'|'unhealthy'|'failed', port?: number, owned?: boolean, detail?: string }} APIState
 */

class APIManager {
  /**
   * @param {object} [opts]
   * @param {Record<string,string>} [opts.env]
   * @param {string} [opts.platform]
   * @param {string} [opts.home]
   * @param {Function} [opts.spawn]
   * @param {Function} [opts.fetch]
   * @param {Function} [opts.resolve]
   * @param {string} [opts.logPath]
   * @param {number} [opts.startupTimeoutMs]
   * @param {number} [opts.pollIntervalMs]
   * @param {number} [opts.healthIntervalMs]
   * @param {Function} [opts.now]
   * @param {Function} [opts.sleep]
   * @param {Function} [opts.ensureLogDir]
   * @param {Function} [opts.createWriteStream]
   */
  constructor(opts = {}) {
    this._env = opts.env || process.env;
    this._platform = opts.platform || process.platform;
    this._home = opts.home != null ? opts.home : os.homedir();
    this._spawn = opts.spawn || defaultSpawn;
    this._fetch = opts.fetch || globalThis.fetch.bind(globalThis);
    this._resolve =
      opts.resolve ||
      ((resolveOpts) =>
        resolveApiLaunch({
          env: this._env,
          pathEnv: this._env.PATH || "",
          home: this._home,
          platform: this._platform,
          ...resolveOpts,
        }));
    this._logPath =
      opts.logPath ||
      defaultLogPath({
        platform: this._platform,
        home: this._home,
        env: this._env,
      });
    this._startupTimeoutMs = opts.startupTimeoutMs ?? 30_000;
    this._pollIntervalMs = opts.pollIntervalMs ?? 200;
    this._healthIntervalMs = opts.healthIntervalMs ?? 3_000;
    this._now = opts.now || (() => Date.now());
    this._sleep =
      opts.sleep ||
      ((ms) => new Promise((r) => setTimeout(r, ms)));
    this._ensureLogDir =
      opts.ensureLogDir ||
      ((filePath) => {
        fs.mkdirSync(path.dirname(filePath), { recursive: true });
      });
    this._createWriteStream =
      opts.createWriteStream ||
      ((filePath) => {
        this._ensureLogDir(filePath);
        if (!fs.existsSync(filePath)) {
          fs.writeFileSync(filePath, "");
        }
        return fs.createWriteStream(filePath, { flags: "a" });
      });

    this._preferredHost = this._env.AW_API_HOST || "127.0.0.1";
    this._preferredPort = Number.parseInt(this._env.AW_API_PORT || "8085", 10) || 8085;

    /** @type {APIState} */
    this._state = { status: "stopped" };
    /** @type {Map<string, Set<Function>>} */
    this._listeners = new Map();
    this._process = null;
    this._logStream = null;
    this._owned = false;
    this._port = this._preferredPort;
    this._monitorTimer = null;
    this._starting = false;
    this._stopped = false;
  }

  get state() {
    return { ...this._state };
  }

  get logPath() {
    return this._logPath;
  }

  get preferredPort() {
    return this._preferredPort;
  }

  /**
   * EventEmitter-style: only 'state' is used.
   * @param {string} event
   * @param {Function} cb
   */
  on(event, cb) {
    if (typeof cb !== "function") return this;
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(cb);
    return this;
  }

  off(event, cb) {
    const set = this._listeners.get(event);
    if (set) set.delete(cb);
    return this;
  }

  _emit(event, payload) {
    const set = this._listeners.get(event);
    if (!set) return;
    for (const cb of set) {
      try {
        cb(payload);
      } catch {
        /* ignore listener errors */
      }
    }
  }

  /**
   * @param {APIState} next
   */
  _setState(next) {
    this._state = { ...next };
    this._emit("state", this.state);
  }

  async start() {
    if (this._state.status === "ready" || this._state.status === "starting" || this._starting) {
      return;
    }
    this._stopped = false;
    this._starting = true;
    this._clearMonitor();
    this._tearDownProcessOnly();
    this._setState({ status: "starting" });

    const preferred = this._preferredPort;

    try {
      if (await this._healthOK(preferred)) {
        this._port = preferred;
        this._owned = false;
        this._setState({ status: "ready", port: preferred, owned: false });
        this._startHealthMonitor();
        return;
      }

      const launch = this._resolve({});
      if (!launch) {
        this._setState({
          status: "failed",
          detail:
            "Could not find awareness-api. Install the venv (uv sync / pip install -e .) " +
            "or set AWARENESS_API_BIN / AWARENESS_REPO.",
        });
        return;
      }

      const bindPort = preferred;
      try {
        this._spawnLaunch(launch, bindPort);
      } catch (err) {
        this._setState({
          status: "failed",
          detail: `Failed to start awareness-api: ${err && err.message ? err.message : err}`,
        });
        return;
      }

      const deadline = this._now() + this._startupTimeoutMs;
      while (this._now() < deadline) {
        if (this._stopped) {
          this._setState({ status: "failed", detail: "Startup cancelled" });
          return;
        }
        if (await this._healthOK(bindPort)) {
          this._port = bindPort;
          this._owned = true;
          this._setState({ status: "ready", port: bindPort, owned: true });
          this._startHealthMonitor();
          return;
        }
        if (this._process && this._process.exitCode != null) {
          const code = this._process.exitCode;
          this._process = null;
          this._closeLogStream();
          this._setState({
            status: "failed",
            detail:
              `awareness-api exited early (status ${code}). See ${this._logPath}`,
          });
          return;
        }
        // Also treat 'exit' event having fired with killed process
        if (this._process && this._process.killed && this._process.exitCode == null) {
          // still running or transitional — continue polling
        }
        await this._sleep(this._pollIntervalMs);
      }

      this._tearDownProcessOnly();
      this._setState({
        status: "failed",
        detail:
          `awareness-api did not become healthy within ${Math.round(this._startupTimeoutMs / 1000)}s. ` +
          `See ${this._logPath}`,
      });
    } finally {
      this._starting = false;
    }
  }

  async stop() {
    this._stopped = true;
    this._clearMonitor();
    this._tearDownProcessOnly();
    this._owned = false;
    this._setState({ status: "stopped" });
  }

  async restart() {
    await this.stop();
    this._stopped = false;
    await this.start();
  }

  _startHealthMonitor() {
    this._clearMonitor();
    const tick = async () => {
      if (this._stopped) return;
      await this._checkHealthOnce();
      if (!this._stopped && this._monitorTimer !== null) {
        this._monitorTimer = setTimeout(tick, this._healthIntervalMs);
        if (typeof this._monitorTimer.unref === "function") {
          this._monitorTimer.unref();
        }
      }
    };
    this._monitorTimer = setTimeout(tick, this._healthIntervalMs);
    if (typeof this._monitorTimer.unref === "function") {
      this._monitorTimer.unref();
    }
  }

  _clearMonitor() {
    if (this._monitorTimer) {
      clearTimeout(this._monitorTimer);
      this._monitorTimer = null;
    }
  }

  async _checkHealthOnce() {
    const status = this._state.status;
    if (status !== "ready" && status !== "unhealthy") return;
    const currentPort = this._state.port ?? this._port;

    if (await this._healthOK(currentPort)) {
      if (this._state.status === "unhealthy") {
        this._setState({
          status: "ready",
          port: currentPort,
          owned: this._owned,
        });
      }
      return;
    }

    if (this._owned) {
      this._setState({
        status: "unhealthy",
        port: currentPort,
        owned: true,
        detail: "API process not responding — restarting…",
      });
      this._tearDownProcessOnly();
      await this._startOwnedOnPort(currentPort);
    } else {
      this._setState({
        status: "unhealthy",
        port: currentPort,
        owned: false,
        detail: "Attached API is down. Retry to start a local instance.",
      });
    }
  }

  async _startOwnedOnPort(bindPort) {
    this._setState({ status: "starting" });
    const launch = this._resolve({});
    if (!launch) {
      this._setState({
        status: "failed",
        detail: "Could not find awareness-api after crash.",
      });
      return;
    }
    try {
      this._spawnLaunch(launch, bindPort);
    } catch (err) {
      this._setState({
        status: "failed",
        detail: `Restart failed: ${err && err.message ? err.message : err}`,
      });
      return;
    }
    const timeout = Math.min(this._startupTimeoutMs, 20_000);
    const deadline = this._now() + timeout;
    while (this._now() < deadline) {
      if (await this._healthOK(bindPort)) {
        this._port = bindPort;
        this._owned = true;
        this._setState({ status: "ready", port: bindPort, owned: true });
        return;
      }
      if (this._process && this._process.exitCode != null) {
        this._process = null;
        this._closeLogStream();
        this._setState({
          status: "failed",
          detail: `API crashed again. See ${this._logPath}`,
        });
        return;
      }
      await this._sleep(this._pollIntervalMs);
    }
    this._tearDownProcessOnly();
    this._setState({
      status: "failed",
      detail: `API restart timed out. See ${this._logPath}`,
    });
  }

  _spawnLaunch(launch, port) {
    const env = {
      ...this._env,
      AW_API_HOST: this._preferredHost,
      AW_API_PORT: String(port),
      ...(launch.env || {}),
    };
    if (launch.cwd) {
      env.AWARENESS_REPO = launch.cwd;
    }

    let logStream = null;
    try {
      logStream = this._createWriteStream(this._logPath);
      this._logStream = logStream;
    } catch {
      logStream = null;
      this._logStream = null;
    }

    const child = this._spawn(launch.command, launch.args || [], {
      env,
      cwd: launch.cwd || undefined,
      stdio: logStream ? ["ignore", logStream, logStream] : "ignore",
      windowsHide: true,
    });

    // Track exit
    if (child && typeof child.on === "function") {
      child.on("exit", (code) => {
        if (child.exitCode == null) child.exitCode = code;
      });
      child.on("error", () => {
        /* spawn errors surface via exit / health timeout */
      });
    }

    this._process = child;
    this._owned = true;
    return child;
  }

  _tearDownProcessOnly() {
    const proc = this._process;
    this._process = null;
    if (proc && this._owned !== false) {
      this._killProcess(proc);
    } else if (proc) {
      this._killProcess(proc);
    }
    this._closeLogStream();
  }

  _killProcess(proc) {
    if (!proc) return;
    const pid = proc.pid;
    try {
      if (this._platform === "win32") {
        // Kill process tree on Windows
        if (pid) {
          try {
            this._spawn("taskkill", ["/pid", String(pid), "/T", "/F"], {
              stdio: "ignore",
              windowsHide: true,
            });
          } catch {
            try {
              proc.kill();
            } catch {
              /* ignore */
            }
          }
        } else {
          try {
            proc.kill();
          } catch {
            /* ignore */
          }
        }
      } else {
        try {
          // SIGTERM first
          if (typeof proc.kill === "function") {
            proc.kill("SIGTERM");
          }
        } catch {
          /* ignore */
        }
        // Force kill after short grace if still alive (best-effort sync for quit path)
        try {
          if (proc.exitCode == null && !proc.killed && typeof proc.kill === "function") {
            // Schedule force kill; for tests mocks may ignore
            const killer = setTimeout(() => {
              try {
                if (proc.exitCode == null) proc.kill("SIGKILL");
              } catch {
                /* ignore */
              }
            }, 2000);
            if (typeof killer.unref === "function") killer.unref();
          }
        } catch {
          /* ignore */
        }
      }
    } catch {
      /* ignore */
    }
  }

  _closeLogStream() {
    if (this._logStream) {
      try {
        this._logStream.end();
      } catch {
        /* ignore */
      }
      this._logStream = null;
    }
  }

  async _healthOK(port) {
    const url = `http://${this._preferredHost}:${port}/healthz`;
    try {
      const controller =
        typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = controller
        ? setTimeout(() => controller.abort(), 1500)
        : null;
      const res = await this._fetch(url, {
        method: "GET",
        signal: controller ? controller.signal : undefined,
      });
      if (timer) clearTimeout(timer);
      if (!res || res.status !== 200) return false;
      // Prefer JSON { ok: true }
      try {
        if (typeof res.json === "function") {
          const body = await res.json();
          if (body && typeof body.ok === "boolean") return body.ok;
        }
      } catch {
        /* non-json 200 is ok (lenient attach) */
      }
      return true;
    } catch {
      return false;
    }
  }
}

module.exports = { APIManager };
