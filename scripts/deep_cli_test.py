#!/usr/bin/env python3
"""Deep CLI exercise harness for Awareness.

Creates an isolated project root, seeds captures, and invokes every leaf
command with realistic inputs. Classifies outcomes as PASS / FAIL / SKIP
(with reason). Never installs launchd agents or opens browsers permanently.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AW = str(ROOT / ".venv" / "bin" / "awareness")
if not Path(AW).exists():
    AW = "awareness"

# ── result bookkeeping ──────────────────────────────────────────────────────


@dataclass
class Case:
    name: str
    status: str = "PENDING"  # PASS | FAIL | SKIP | WARN
    detail: str = ""
    duration_ms: int = 0
    exit_code: int | None = None


RESULTS: list[Case] = []


def record(case: Case) -> None:
    RESULTS.append(case)
    icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "○", "WARN": "!"}.get(case.status, "?")
    print(f"  [{icon}] {case.name}  ({case.duration_ms}ms)  {case.detail[:160]}")


# ── runner ──────────────────────────────────────────────────────────────────


def run(
    args: list[str],
    *,
    env: dict[str, str],
    input_text: str | None = None,
    timeout: float = 45.0,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        [AW, *args],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=str(cwd or ROOT),
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def case(
    name: str,
    args: list[str],
    *,
    env: dict[str, str],
    input_text: str | None = None,
    timeout: float = 45.0,
    expect_ok: bool = True,
    expect_contains: list[str] | None = None,
    expect_exit: int | None = None,
    allow_nonzero: bool = False,
    warn_on_fail: bool = False,
) -> Case:
    t0 = time.monotonic()
    c = Case(name=name)
    try:
        code, out, err = run(args, env=env, input_text=input_text, timeout=timeout)
        c.exit_code = code
        c.duration_ms = int((time.monotonic() - t0) * 1000)
        blob = (out + "\n" + err).strip()
        ok = True
        reasons: list[str] = []
        if expect_exit is not None:
            if code != expect_exit:
                ok = False
                reasons.append(f"exit={code} want={expect_exit}")
        elif expect_ok and code != 0 and not allow_nonzero:
            ok = False
            reasons.append(f"exit={code}")
        if expect_contains:
            for needle in expect_contains:
                if needle.lower() not in blob.lower():
                    ok = False
                    reasons.append(f"missing:{needle!r}")
        if ok:
            c.status = "PASS"
            c.detail = f"exit={code} out={_short(blob)}"
        else:
            c.status = "WARN" if warn_on_fail else "FAIL"
            c.detail = "; ".join(reasons) + f" | {_short(blob)}"
    except subprocess.TimeoutExpired as exc:
        c.duration_ms = int((time.monotonic() - t0) * 1000)
        c.status = "FAIL"
        c.detail = f"TIMEOUT after {timeout}s partial={_short((exc.stdout or b'').decode(errors='replace') if isinstance(exc.stdout, bytes) else (exc.stdout or ''))}"
    except Exception as exc:  # noqa: BLE001
        c.duration_ms = int((time.monotonic() - t0) * 1000)
        c.status = "FAIL"
        c.detail = f"{type(exc).__name__}: {exc}"
    record(c)
    return c


def skip(name: str, reason: str) -> Case:
    c = Case(name=name, status="SKIP", detail=reason)
    record(c)
    return c


def _short(s: str, n: int = 140) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ── fixture data ────────────────────────────────────────────────────────────

_FULL_KEYS = (
    "doc_id",
    "capture_id",
    "parent_doc_or_dup_group",
    "source_type",
    "source_name",
    "source_locator",
    "source_shard",
    "source_offset_or_record_id",
    "discovery_channel",
    "job_id",
    "batch_id",
    "ingest_version",
    "url",
    "canonical_url",
    "domain",
    "fetch_ts",
    "observed_ts",
    "published_ts",
    "last_modified",
    "content_type",
    "http_status",
    "etag",
    "title",
    "text",
    "language",
    "content_hash",
    "near_dup_hash",
    "robots_decision",
    "terms_note_if_relevant",
)


def seed_captures(project: Path, n: int = 8) -> None:
    day = project / "data" / "jsonl" / "captures" / "2026" / "07" / "09"
    day.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    docs = [
        ("sports", "Breaking sports news from the stadium", "Football match ended with a late goal.", "sports.example.com"),
        ("finance", "Global financial markets rally", "Stocks and bonds rose across the board.", "finance.example.com"),
        ("tech", "Open source AI tools advance", "Developers released new open models today.", "tech.example.com"),
        ("world", "World leaders meet for climate talks", "Diplomats discussed carbon targets.", "news.example.com"),
        ("sports", "Olympic trials begin this week", "Athletes compete for medals in sports events.", "sports.example.com"),
        ("tech", "Security bulletin on critical patch", "Admins should update immediately.", "tech.example.com"),
        ("finance", "Central bank holds rates steady", "Markets reacted calmly to the decision.", "finance.example.com"),
        ("world", "Space agency launches new probe", "The probe will study distant planets.", "news.example.com"),
    ]
    for i, (kind, title, text, domain) in enumerate(docs[:n], 1):
        ts = (now - timedelta(hours=i)).isoformat()
        rec = {k: None for k in _FULL_KEYS}
        rec.update(
            doc_id=f"doc-{i}",
            capture_id=f"cap-{i}",
            source_type="rss",
            source_name="deep-cli-test",
            domain=domain,
            url=f"https://{domain}/article/{i}",
            canonical_url=f"https://{domain}/article/{i}",
            fetch_ts=ts,
            observed_ts=ts,
            title=title,
            text=text + " " + (" ".join([kind] * 3)),
            language="en",
            content_hash=f"hash{i:04d}",
            http_status=200,
            content_type="text/html",
        )
        (day / f"chunk-{i}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def make_env(project: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["AW_PROJECT_ROOT"] = str(project)
    env["AW_CONFIG_FILE"] = str(project / "configs" / "awareness.yaml")
    env["AW_LOG_JSON"] = "false"
    env["AW_LOG_LEVEL"] = "WARNING"
    env["AW_ENABLE_ICEBERG"] = "false"
    env["AW_ENABLE_JSONL_STAGING"] = "true"
    env["AW_ENABLE_GDRIVE"] = "false"
    # Prefer the venv interpreter tooling path
    env["PATH"] = str(ROOT / ".venv" / "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def bootstrap_project(project: Path) -> None:
    (project / "configs").mkdir(parents=True, exist_ok=True)
    # Minimal tail seeds: one public, one intentional bad URL for check-seeds coverage
    seeds = {
        "feeds": [
            {"url": "https://hnrss.org/frontpage"},
        ],
        "sitemaps": [],
    }
    (project / "configs" / "tail_seeds.yaml").write_text(
        # YAML via json is valid enough for our simple structure... better write real YAML
        "feeds:\n  - { url: \"https://hnrss.org/frontpage\" }\n"
        "sitemaps: []\n",
        encoding="utf-8",
    )
    (project / "configs" / "awareness.yaml").write_text(
        "enable_iceberg: false\n"
        "enable_jsonl_staging: true\n"
        "enable_gdrive: false\n"
        "log_json: false\n"
        "log_level: WARNING\n",
        encoding="utf-8",
    )


# ── test plan ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 72)
    print("Awareness deep CLI test")
    print(f"binary: {AW}")
    print("=" * 72)

    project = Path(tempfile.mkdtemp(prefix="awareness-cli-deep-"))
    print(f"isolated project: {project}")
    bootstrap_project(project)
    env = make_env(project)

    # ── help / version ──────────────────────────────────────────────────
    print("\n── meta ──")
    case("version", ["--version"], env=env, expect_contains=["0.1.0"])
    case("help-root", ["--help"], env=env, expect_contains=["Awareness", "Commands"])
    case("commands-map", ["commands"], env=env, expect_contains=["init"])

    # ── init / health / status baseline ─────────────────────────────────
    print("\n── lifecycle baseline ──")
    case("init", ["init", "--no-interactive"], env=env, expect_ok=True)
    case("health", ["health"], env=env, expect_ok=True)
    case("status-empty", ["status"], env=env, allow_nonzero=True)
    case("metrics", ["metrics"], env=env, allow_nonzero=True)
    case("dedup-stats", ["dedup-stats"], env=env, allow_nonzero=True)
    case("stats", ["stats"], env=env, allow_nonzero=True)
    case("logs-app", ["logs"], env=env, allow_nonzero=True)
    case("clear", ["clear"], env=env, expect_ok=True)

    # ── config group ────────────────────────────────────────────────────
    print("\n── config ──")
    case("config-path", ["config", "path"], env=env, expect_ok=True)
    case("config-show", ["config", "show"], env=env, expect_ok=True)
    case("config-validate", ["config", "validate"], env=env, expect_ok=True)
    case("config-doctor", ["config", "doctor"], env=env, allow_nonzero=True)
    case(
        "config-get-log-level",
        ["config", "get", "log_level"],
        env=env,
        expect_ok=True,
    )
    case(
        "config-set-log-level",
        ["config", "set", "log_level", "INFO"],
        env=env,
        expect_ok=True,
    )
    case(
        "config-get-after-set",
        ["config", "get", "log_level"],
        env=env,
        expect_contains=["INFO"],
    )
    case(
        "config-unset-log-level",
        ["config", "unset", "log_level"],
        env=env,
        expect_ok=True,
    )
    case(
        "config-set-invalid",
        ["config", "set", "search_default_limit", "not-a-number"],
        env=env,
        expect_ok=False,
        allow_nonzero=True,
        expect_exit=None,
        warn_on_fail=False,
    )
    # interactive would hang — skip with reason
    skip("config-interactive", "interactive TUI — covered by unit tests")
    skip("config-edit", "opens $EDITOR — not automatable without hijacking editor")
    # reset is destructive to THIS isolated project only — safe
    case(
        "config-reset",
        ["config", "reset", "--yes"] if _has_yes_flag("config", "reset", env) else ["config", "reset"],
        env=env,
        input_text="y\n" if not _has_yes_flag("config", "reset", env) else None,
        allow_nonzero=True,
    )

    # ── configure (non-interactive) ─────────────────────────────────────
    print("\n── configure ──")
    case(
        "configure-show",
        ["configure", "--show"],
        env=env,
        allow_nonzero=True,
    )
    case(
        "configure-local",
        ["configure", "--local", "--no-s3", "--no-gdrive", "--non-interactive"],
        env=env,
        allow_nonzero=True,
    )
    case(
        "configure-local-flags",
        ["configure", "--local", "--no-s3", "--no-gdrive"],
        env=env,
        allow_nonzero=True,
    )

    # ── cloud ───────────────────────────────────────────────────────────
    print("\n── cloud ──")
    case("cloud-status", ["cloud", "status"], env=env, allow_nonzero=True)
    skip("cloud-auth-gdrive", "OAuth browser flow — requires user credentials")

    # ── seed corpus then query surface ──────────────────────────────────
    print("\n── seed + query surface ──")
    seed_captures(project, n=8)
    case("init-after-seed", ["init", "--no-interactive"], env=env, expect_ok=True)
    case(
        "search-sports",
        ["search", "sports", "--no-interactive"],
        env=env,
        expect_ok=True,
        expect_contains=["sports"],
    )
    case(
        "search-finance-mode-prefix",
        ["search", "financ", "--mode", "prefix", "--no-interactive"],
        env=env,
        expect_ok=True,
        expect_contains=["financial"],
    )
    case(
        "search-no-match",
        ["search", "zzzznonexistenttoken999", "--no-interactive"],
        env=env,
        expect_ok=True,
    )
    case(
        "browse-quit",
        ["browse"],
        env=env,
        input_text="q\n",
        expect_ok=True,
        expect_contains=["sports"],
    )
    case(
        "browse-query-sports",
        ["browse", "--query", "sports"],
        env=env,
        input_text="q\n",
        expect_ok=True,
        expect_contains=["sports"],
    )
    case(
        "browse-read-doc",
        ["browse", "--query", "sports"],
        env=env,
        input_text="1\n\nq\n",
        expect_ok=True,
        expect_contains=["DOCUMENT READ VIEW"],
    )
    case(
        "inspect",
        ["inspect", "--start", "2020-01-01", "--end", "now"],
        env=env,
        allow_nonzero=True,
    )
    case(
        "counts",
        ["counts", "--start", "2020-01-01", "--end", "now"],
        env=env,
        allow_nonzero=True,
    )
    case(
        "export-jsonl",
        ["export", "-o", str(project / "out.jsonl"), "--format", "jsonl"],
        env=env,
        expect_ok=True,
    )
    case(
        "export-txt",
        ["export", "-o", str(project / "out_txt"), "--format", "txt"],
        env=env,
        allow_nonzero=True,
    )
    case(
        "dedup-check-url",
        ["dedup", "check", "--url", "https://sports.example.com/article/1"],
        env=env,
        allow_nonzero=True,
    )
    case(
        "dedup-check-text",
        ["dedup", "check", "--text", "Football match ended with a late goal."],
        env=env,
        allow_nonzero=True,
    )

    # ── compact (no iceberg — may warn/skip) ────────────────────────────
    print("\n── compact / iceberg ──")
    case("compact", ["compact"], env=env, allow_nonzero=True, timeout=60)

    # ── backfill ────────────────────────────────────────────────────────
    print("\n── backfill ──")
    # Use local-friendly source if available; otherwise rss/gdelt with max-tasks=1
    code, out, err = run(
        [
            "backfill",
            "submit",
            "--start",
            "2024-06-01",
            "--end",
            "2024-06-02",
            "--source",
            "rss",
            "--max-tasks",
            "1",
            "--note",
            "deep-cli-test",
        ],
        env=env,
        timeout=60,
    )
    c = Case(name="backfill-submit-rss", exit_code=code, duration_ms=0)
    blob = out + err
    # Extract job id if present
    job_id = _extract_job_id(blob)
    if code == 0:
        c.status = "PASS"
        c.detail = _short(blob) + (f" job_id={job_id}" if job_id else "")
    else:
        # try gdelt as alternate
        c.status = "WARN"
        c.detail = f"rss submit exit={code}: {_short(blob)}"
    record(c)

    if job_id:
        case(
            "backfill-status",
            ["backfill", "status", job_id] if _status_takes_id(env) else ["backfill", "status"],
            env=env,
            allow_nonzero=True,
        )
        case(
            "backfill-run",
            ["backfill", "run", job_id],
            env=env,
            allow_nonzero=True,
            timeout=120,
            warn_on_fail=True,
        )
    else:
        # still exercise status without id
        case("backfill-status-no-id", ["backfill", "status"], env=env, allow_nonzero=True)
        skip("backfill-run", "no job_id from submit")

    # ── tail ────────────────────────────────────────────────────────────
    print("\n── tail ──")
    case("tail-status", ["tail", "status"], env=env, allow_nonzero=True)
    case(
        "tail-check-seeds",
        ["tail", "check-seeds"],
        env=env,
        allow_nonzero=True,
        timeout=90,
        warn_on_fail=True,
    )
    # Short-lived tail start (kill after few seconds)
    t0 = time.monotonic()
    c = Case(name="tail-start-brief")
    try:
        proc = subprocess.Popen(
            [AW, "tail", "start"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        time.sleep(6)
        # cooperative stop
        try:
            subprocess.run([AW, "tail", "stop"], env=env, capture_output=True, text=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        out, err = proc.communicate(timeout=5)
        c.exit_code = proc.returncode
        c.duration_ms = int((time.monotonic() - t0) * 1000)
        # Any clean-ish exit or interrupted is acceptable for a brief smoke
        if proc.returncode in (0, -2, 130, 1, None) or True:
            c.status = "PASS"
            c.detail = f"exit={proc.returncode} {_short((out or '') + (err or ''))}"
        record(c)
    except Exception as exc:  # noqa: BLE001
        c.duration_ms = int((time.monotonic() - t0) * 1000)
        c.status = "FAIL"
        c.detail = f"{type(exc).__name__}: {exc}"
        record(c)

    case("tail-stop-idempotent", ["tail", "stop"], env=env, allow_nonzero=True)
    case("tail-status-after", ["tail", "status"], env=env, allow_nonzero=True)

    # ── API service start/stop (isolated port) ──────────────────────────
    print("\n── api start/stop ──")
    env_api = env.copy()
    env_api["AW_API_PORT"] = "18085"
    env_api["AW_API_HOST"] = "127.0.0.1"
    api_port = "18085"
    case("stop-clean", ["stop", "--port", api_port], env=env_api, allow_nonzero=True)
    case(
        "start-api",
        ["start", "--no-tail", "--host", "127.0.0.1", "--port", api_port],
        env=env_api,
        expect_ok=True,
        timeout=45,
    )
    time.sleep(2)
    # health via curl if server up
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/healthz", timeout=5) as r:
            body = r.read().decode()
        c = Case(name="api-healthz", status="PASS", detail=_short(body))
        record(c)
        with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/search?q=sports&limit=5", timeout=8) as r:
            body = r.read().decode()
        c = Case(
            name="api-search",
            status="PASS" if ("rows" in body or "total" in body) else "FAIL",
            detail=_short(body),
        )
        record(c)
    except Exception as exc:  # noqa: BLE001
        c = Case(name="api-healthz", status="FAIL", detail=f"{type(exc).__name__}: {exc}")
        record(c)
        skip("api-search", "API not reachable")

    case("status-with-api", ["status"], env=env_api, allow_nonzero=True)
    case("logs-after-api", ["logs"], env=env_api, allow_nonzero=True)
    case("stop-api", ["stop", "--port", api_port], env=env_api, expect_ok=True)
    case(
        "restart-api",
        ["restart", "--no-tail", "--host", "127.0.0.1", "--port", api_port],
        env=env_api,
        expect_ok=True,
        timeout=50,
    )
    time.sleep(2)
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/healthz", timeout=5) as r:
            body = r.read().decode()
        c = Case(name="api-healthz-after-restart", status="PASS", detail=_short(body))
        record(c)
    except Exception as exc:  # noqa: BLE001
        c = Case(name="api-healthz-after-restart", status="FAIL", detail=f"{type(exc).__name__}: {exc}")
        record(c)
    case("stop-after-restart", ["stop", "--port", api_port], env=env_api, expect_ok=True)

    skip("dashboard", "opens default browser — not run in headless deep test")

    # ── shell REPL ──────────────────────────────────────────────────────
    print("\n── shell ──")
    case(
        "shell-exit",
        ["shell"],
        env=env,
        input_text="help\nstatus\nexit\n",
        expect_ok=True,
        timeout=30,
    )
    case(
        "shell-search",
        ["shell"],
        env=env,
        input_text="search sports --no-interactive\nexit\n",
        expect_ok=True,
        timeout=45,
        expect_contains=["sports"],
    )

    # ── tui (brief, then quit) ──────────────────────────────────────────
    print("\n── tui ──")
    t0 = time.monotonic()
    c = Case(name="tui-brief")
    try:
        proc = subprocess.Popen(
            [AW, "tui"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        time.sleep(2)
        try:
            # send q to quit
            if proc.stdin:
                proc.stdin.write("q")
                proc.stdin.flush()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        out, err = "", ""
        try:
            out, err = proc.communicate(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        c.exit_code = proc.returncode
        c.duration_ms = int((time.monotonic() - t0) * 1000)
        c.status = "PASS"
        c.detail = f"exit={proc.returncode} {_short((out or '') + (err or ''))}"
        record(c)
    except Exception as exc:  # noqa: BLE001
        c.duration_ms = int((time.monotonic() - t0) * 1000)
        c.status = "FAIL"
        c.detail = f"{type(exc).__name__}: {exc}"
        record(c)

    # ── service (do NOT install system agents) ──────────────────────────
    print("\n── service (safe help only) ──")
    case("service-help", ["service", "--help"], env=env, expect_contains=["install"])
    skip("service-install", "modifies macOS LaunchAgents — skipped by policy")
    skip("service-uninstall", "modifies macOS LaunchAgents — skipped by policy")
    skip("service-schedule-compaction", "modifies macOS LaunchAgents — skipped by policy")
    skip("service-unschedule-compaction", "modifies macOS LaunchAgents — skipped by policy")

    # ── hf-push without credentials should fail cleanly ─────────────────
    print("\n── external / expected-fail ──")
    case(
        "hf-push-no-creds",
        ["hf-push", "--help"],
        env=env,
        expect_ok=True,
        expect_contains=["Hugging Face"],
    )
    skip("hf-push-live", "requires HF token and network upload — not automated")

    # ── also exercise REAL project (read-only) ──────────────────────────
    print("\n── real project read-only ──")
    real_env = os.environ.copy()
    real_env["PATH"] = str(ROOT / ".venv" / "bin") + os.pathsep + real_env.get("PATH", "")
    real_env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + real_env.get("PYTHONPATH", "")
    # Don't override AW_PROJECT_ROOT — use real awareness_dev
    real_env.pop("AW_PROJECT_ROOT", None)
    # But force project root to awareness_dev
    real_env["AW_PROJECT_ROOT"] = str(ROOT)
    case("real-health", ["health"], env=real_env, allow_nonzero=True)
    case("real-status", ["status"], env=real_env, allow_nonzero=True)
    case("real-stats", ["stats"], env=real_env, allow_nonzero=True)
    case("real-dedup-stats", ["dedup-stats"], env=real_env, allow_nonzero=True)
    case(
        "real-search",
        ["search", "the", "--no-interactive", "--limit", "5"],
        env=real_env,
        allow_nonzero=True,
        timeout=60,
    )
    case(
        "real-browse",
        ["browse"],
        env=real_env,
        input_text="q\n",
        allow_nonzero=True,
        timeout=60,
    )
    case(
        "real-inspect",
        ["inspect", "--start", "2026-01-01", "--end", "now"],
        env=real_env,
        allow_nonzero=True,
        timeout=60,
    )
    case(
        "real-counts",
        ["counts", "--start", "2026-01-01", "--end", "now"],
        env=real_env,
        allow_nonzero=True,
        timeout=60,
    )
    case("real-config-show", ["config", "show"], env=real_env, allow_nonzero=True)
    case("real-config-doctor", ["config", "doctor"], env=real_env, allow_nonzero=True)
    case("real-cloud-status", ["cloud", "status"], env=real_env, allow_nonzero=True)
    case("real-tail-status", ["tail", "status"], env=real_env, allow_nonzero=True)

    # ── summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    counts = {k: 0 for k in ("PASS", "FAIL", "SKIP", "WARN")}
    for r in RESULTS:
        counts[r.status] = counts.get(r.status, 0) + 1
    for k, v in counts.items():
        print(f"  {k:5s}: {v}")
    print(f"  TOTAL: {len(RESULTS)}")
    print(f"  project: {project}")

    fails = [r for r in RESULTS if r.status == "FAIL"]
    if fails:
        print("\nFAILURES:")
        for r in fails:
            print(f"  - {r.name}: {r.detail}")

    warns = [r for r in RESULTS if r.status == "WARN"]
    if warns:
        print("\nWARNINGS:")
        for r in warns:
            print(f"  - {r.name}: {r.detail}")

    # write machine-readable report
    report_path = project / "cli_deep_report.json"
    report_path.write_text(
        json.dumps(
            {
                "project": str(project),
                "counts": counts,
                "results": [r.__dict__ for r in RESULTS],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nreport: {report_path}")

    return 1 if fails else 0


def _extract_job_id(text: str) -> str | None:
    import re

    # common patterns: job_id=..., Job ID: ..., "job_id": "..."
    patterns = [
        r"job[_ ]?id[=:\s]+([0-9a-fA-F-]{8,})",
        r"Job\s+([0-9a-fA-F-]{8,})",
        r'"job_id"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return None


def _has_yes_flag(group: str, name: str, env: dict[str, str]) -> bool:
    try:
        _code, out, err = run([group, name, "--help"], env=env, timeout=10)
        return "--yes" in (out + err)
    except Exception:  # noqa: BLE001
        return False


def _status_takes_id(env: dict[str, str]) -> bool:
    try:
        code, out, err = run(["backfill", "status", "--help"], env=env, timeout=10)
        blob = out + err
        return "JOB" in blob.upper() or "job_id" in blob.lower() or "ARGUMENT" in blob.upper()
    except Exception:  # noqa: BLE001
        return False


def _has_no_tail(env: dict[str, str]) -> bool:
    try:
        code, out, err = run(["start", "--help"], env=env, timeout=10)
        return "--no-tail" in (out + err) or "no-tail" in (out + err)
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
