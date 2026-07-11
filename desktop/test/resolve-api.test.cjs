"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const os = require("node:os");
const { resolveApiLaunch, detectRepoRoot, defaultLogPath } = require("../resolve-api.cjs");

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
      readFile: (p) => (p.endsWith("pyproject.toml") ? 'name = "awareness"\n' : ""),
    });
    assert.equal(r.kind, "bin");
    assert.equal(r.command, venvBin);
    assert.equal(r.cwd, repo);
  });

  it("uses Windows venv Scripts/awareness-api.exe", () => {
    const repo = "C:\\fake-awareness-repo";
    const venvBin = path.win32.join(repo, ".venv", "Scripts", "awareness-api.exe");
    const pyproject = path.win32.join(repo, "pyproject.toml");
    const r = resolveApiLaunch({
      env: { AWARENESS_REPO: repo },
      pathEnv: "",
      home: "C:\\Users\\test",
      platform: "win32",
      exists: (p) => p === venvBin || p === pyproject,
      isExecutable: (p) => p === venvBin,
      readFile: (p) => (p === pyproject ? 'name = "awareness"\n' : ""),
    });
    assert.ok(r, "expected a launch resolution");
    assert.equal(r.kind, "bin");
    assert.equal(r.command, venvBin);
    assert.equal(r.cwd, path.win32.normalize(repo));
  });

  it("falls back to python-module with -c run", () => {
    const repo = "/tmp/fake-awareness-repo";
    const py = path.join(repo, ".venv", "bin", "python");
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
      readFile: (p) =>
        p.endsWith("pyproject.toml") ? '[project]\nname = "awareness"\n' : "",
    });
    assert.ok(r);
    assert.equal(r.kind, "python-module");
    assert.match(r.command, /python/);
    assert.deepEqual(r.args, ["-c", "from awareness.api.server import run; run()"]);
    assert.equal(r.cwd, repo);
    assert.ok(r.env && r.env.PYTHONPATH && r.env.PYTHONPATH.includes("src"));
    assert.equal(r.command, py);
  });

  it("finds awareness-api on PATH", () => {
    const bin = "/opt/bin/awareness-api";
    const r = resolveApiLaunch({
      env: {},
      pathEnv: "/opt/bin:/usr/bin",
      home: "/tmp",
      platform: "linux",
      exists: (p) => p === bin,
      isExecutable: (p) => p === bin,
      readFile: () => "",
    });
    assert.equal(r.kind, "bin");
    assert.equal(r.command, bin);
  });

  it("returns null when nothing is available", () => {
    const r = resolveApiLaunch({
      env: {},
      pathEnv: "",
      home: "/tmp/no-home",
      platform: "linux",
      exists: () => false,
      isExecutable: () => false,
      readFile: () => "",
      starts: ["/tmp/nowhere"],
    });
    assert.equal(r, null);
  });
});

describe("detectRepoRoot", () => {
  it("accepts AWARENESS_REPO with awareness pyproject", () => {
    const repo = "/tmp/my-awareness";
    const found = detectRepoRoot({
      env: { AWARENESS_REPO: repo },
      home: "/tmp",
      exists: (p) => p === path.join(repo, "pyproject.toml"),
      readFile: () => 'name = "awareness"\n',
      starts: [],
    });
    assert.equal(found, path.resolve(repo));
  });

  it("walks from starts for pyproject", () => {
    const repo = "/tmp/walk/root";
    const start = path.join(repo, "desktop", "nested");
    const found = detectRepoRoot({
      env: {},
      home: "/tmp/other",
      exists: (p) => p === path.join(repo, "pyproject.toml"),
      readFile: () => 'name = "awareness"\n',
      starts: [start],
    });
    assert.equal(found, path.resolve(repo));
  });

  it("checks ~/awareness_dev", () => {
    const home = "/tmp/home-user";
    const repo = path.join(home, "awareness_dev");
    const found = detectRepoRoot({
      env: {},
      home,
      exists: (p) => p === path.join(repo, "pyproject.toml"),
      readFile: () => 'name = "awareness"\n',
      starts: [],
    });
    assert.equal(found, path.resolve(repo));
  });
});

describe("defaultLogPath", () => {
  it("uses APPDATA on Windows", () => {
    const p = defaultLogPath({
      platform: "win32",
      home: "C:\\Users\\x",
      env: { APPDATA: "C:\\Users\\x\\AppData\\Roaming" },
    });
    assert.ok(p.includes("Awareness"));
    assert.ok(p.endsWith("api.log") || p.replace(/\\/g, "/").endsWith("api.log"));
  });

  it("uses XDG state on Linux", () => {
    const p = defaultLogPath({
      platform: "linux",
      home: "/home/u",
      env: {},
    });
    assert.equal(p, path.join("/home/u", ".local", "state", "awareness", "api.log"));
  });

  it("uses Library/Logs on macOS", () => {
    const p = defaultLogPath({
      platform: "darwin",
      home: "/Users/u",
      env: {},
    });
    assert.equal(p, path.join("/Users/u", "Library", "Logs", "Awareness", "api.log"));
  });
});
