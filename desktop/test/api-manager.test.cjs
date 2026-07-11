"use strict";

const { describe, it, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { APIManager } = require("../api-manager.cjs");

function mockChild(pid = 4242) {
  const child = new EventEmitter();
  child.pid = pid;
  child.killed = false;
  child.exitCode = null;
  child.kill = (sig) => {
    child.killed = true;
    child.exitCode = sig === "SIGKILL" ? 137 : 0;
    child.emit("exit", child.exitCode);
    return true;
  };
  return child;
}

function okResponse(body = { ok: true }) {
  return {
    status: 200,
    json: async () => body,
  };
}

function failFetch() {
  return Promise.reject(new Error("ECONNREFUSED"));
}

describe("APIManager", () => {
  it("attaches when health is OK on preferred port", async () => {
    let fetches = 0;
    const mgr = new APIManager({
      env: { AW_API_HOST: "127.0.0.1", AW_API_PORT: "18085" },
      platform: "linux",
      home: "/tmp",
      logPath: "/tmp/awareness-test-api.log",
      fetch: async () => {
        fetches += 1;
        return okResponse();
      },
      resolve: () => {
        throw new Error("should not resolve when attaching");
      },
      spawn: () => {
        throw new Error("should not spawn when attaching");
      },
      healthIntervalMs: 60_000,
    });

    const states = [];
    mgr.on("state", (s) => states.push(s.status));

    await mgr.start();

    assert.equal(mgr.state.status, "ready");
    assert.equal(mgr.state.port, 18085);
    assert.equal(mgr.state.owned, false);
    assert.ok(fetches >= 1);
    assert.ok(states.includes("starting"));
    assert.ok(states.includes("ready"));

    await mgr.stop();
    assert.equal(mgr.state.status, "stopped");
  });

  it("spawns when not healthy and launch resolves", async () => {
    let healthy = false;
    let spawned = null;
    const child = mockChild(1001);

    const mgr = new APIManager({
      env: { AW_API_HOST: "127.0.0.1", AW_API_PORT: "18086", PATH: "" },
      platform: "linux",
      home: "/tmp",
      logPath: "/tmp/awareness-test-api.log",
      startupTimeoutMs: 2000,
      pollIntervalMs: 20,
      healthIntervalMs: 60_000,
      createWriteStream: () => ({ end() {} }),
      fetch: async () => {
        if (!healthy) return failFetch();
        return okResponse();
      },
      resolve: () => ({
        kind: "bin",
        command: "/fake/awareness-api",
        args: [],
        cwd: "/repo",
        env: {},
      }),
      spawn: (cmd, args, opts) => {
        spawned = { cmd, args, opts };
        // Become healthy shortly after spawn
        setTimeout(() => {
          healthy = true;
        }, 40);
        return child;
      },
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    });

    await mgr.start();

    assert.ok(spawned, "expected spawn");
    assert.equal(spawned.cmd, "/fake/awareness-api");
    assert.equal(spawned.opts.env.AW_API_PORT, "18086");
    assert.equal(spawned.opts.env.AW_API_HOST, "127.0.0.1");
    assert.equal(mgr.state.status, "ready");
    assert.equal(mgr.state.owned, true);
    assert.equal(mgr.state.port, 18086);

    await mgr.stop();
    assert.equal(child.killed, true);
    assert.equal(mgr.state.status, "stopped");
  });

  it("fails when no launch candidate", async () => {
    const mgr = new APIManager({
      env: { AW_API_HOST: "127.0.0.1", AW_API_PORT: "18087" },
      platform: "linux",
      home: "/tmp",
      logPath: "/tmp/awareness-test-api.log",
      fetch: async () => failFetch(),
      resolve: () => null,
      spawn: () => {
        throw new Error("should not spawn");
      },
    });

    await mgr.start();
    assert.equal(mgr.state.status, "failed");
    assert.match(mgr.state.detail || "", /Could not find awareness-api/);
  });

  it("fails when process exits before healthy", async () => {
    const child = mockChild(2002);

    const mgr = new APIManager({
      env: { AW_API_HOST: "127.0.0.1", AW_API_PORT: "18088" },
      platform: "linux",
      home: "/tmp",
      logPath: "/tmp/awareness-test-api.log",
      startupTimeoutMs: 1000,
      pollIntervalMs: 20,
      createWriteStream: () => ({ end() {} }),
      fetch: async () => failFetch(),
      resolve: () => ({
        kind: "bin",
        command: "/fake/awareness-api",
        args: [],
        cwd: "/repo",
        env: {},
      }),
      spawn: () => {
        setTimeout(() => {
          child.exitCode = 1;
          child.emit("exit", 1);
        }, 30);
        return child;
      },
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    });

    await mgr.start();
    assert.equal(mgr.state.status, "failed");
    assert.match(mgr.state.detail || "", /exited early/);
  });

  it("stop kills process on Windows via taskkill", async () => {
    const child = mockChild(3003);
    const spawns = [];

    const mgr = new APIManager({
      env: { AW_API_HOST: "127.0.0.1", AW_API_PORT: "18089" },
      platform: "win32",
      home: "C:\\Users\\test",
      logPath: "C:\\Users\\test\\AppData\\Roaming\\Awareness\\logs\\api.log",
      startupTimeoutMs: 2000,
      pollIntervalMs: 20,
      healthIntervalMs: 60_000,
      createWriteStream: () => ({ end() {} }),
      fetch: async () => {
        // unhealthy first so we spawn
        if (spawns.length === 0) return failFetch();
        // After first spawn (api), report healthy
        if (spawns.some((s) => s.cmd === "C:\\fake\\awareness-api.exe")) {
          return okResponse();
        }
        return failFetch();
      },
      resolve: () => ({
        kind: "bin",
        command: "C:\\fake\\awareness-api.exe",
        args: [],
        cwd: "C:\\repo",
        env: {},
      }),
      spawn: (cmd, args, opts) => {
        spawns.push({ cmd, args, opts });
        if (cmd === "taskkill") {
          const tk = mockChild(0);
          return tk;
        }
        return child;
      },
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    });

    // Force healthy after a tiny delay so start succeeds
    let n = 0;
    mgr._fetch = async () => {
      n += 1;
      if (n === 1) return failFetch();
      return okResponse();
    };

    await mgr.start();
    assert.equal(mgr.state.status, "ready");
    assert.equal(mgr.state.owned, true);

    await mgr.stop();
    assert.ok(
      spawns.some((s) => s.cmd === "taskkill"),
      `expected taskkill, got ${JSON.stringify(spawns.map((s) => s.cmd))}`
    );
    assert.equal(mgr.state.status, "stopped");
  });

  it("restart transitions through stopped then starting", async () => {
    let ready = true;
    const mgr = new APIManager({
      env: { AW_API_HOST: "127.0.0.1", AW_API_PORT: "18090" },
      platform: "linux",
      home: "/tmp",
      logPath: "/tmp/awareness-test-api.log",
      healthIntervalMs: 60_000,
      fetch: async () => {
        if (!ready) return failFetch();
        return okResponse();
      },
      resolve: () => null,
      spawn: () => {
        throw new Error("no spawn");
      },
    });

    const trail = [];
    mgr.on("state", (s) => trail.push(s.status));

    await mgr.start();
    assert.equal(mgr.state.status, "ready");

    await mgr.restart();
    assert.equal(mgr.state.status, "ready");
    assert.ok(trail.includes("stopped"));
    assert.ok(trail.filter((s) => s === "starting").length >= 2);

    await mgr.stop();
  });
});
