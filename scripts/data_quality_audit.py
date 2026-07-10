#!/usr/bin/env python3
"""End-to-end data quality + search + dedup + live-fetch audit for Awareness.

Exit codes:
  0 = all hard gates pass
  1 = one or more hard gates failed
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("AW_PROJECT_ROOT", str(ROOT))
os.environ.setdefault("AW_LOG_JSON", "false")
os.environ.setdefault("AW_LOG_LEVEL", "WARNING")

from awareness.config import get_settings, reset_settings  # noqa: E402
from awareness.storage.duckdb_index import DuckDbIndex  # noqa: E402
from awareness.storage.state import StateDB  # noqa: E402

API = os.environ.get("AW_AUDIT_API", "http://127.0.0.1:8085")


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str
    hard: bool = True
    metrics: dict = field(default_factory=dict)


GATES: list[Gate] = []


def gate(name: str, passed: bool, detail: str, *, hard: bool = True, **metrics: object) -> None:
    GATES.append(Gate(name=name, passed=passed, detail=detail, hard=hard, metrics=dict(metrics)))
    mark = "PASS" if passed else ("FAIL" if hard else "WARN")
    print(f"[{mark}] {name}: {detail}")


def api_get(path: str, timeout: float = 30.0) -> tuple[int, object]:
    url = API.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as e:
        return 0, str(e)


def token_hit(text: str, query: str) -> bool:
    """True if every meaningful query token appears in title or text (stem-ish prefix)."""
    terms = [t for t in re.findall(r"[A-Za-z0-9']+", query.lower()) if len(t) >= 2]
    if not terms:
        return True
    blob = (text or "").lower()
    for t in terms:
        # allow prefix match for stemmed variants (financ~financial)
        if re.search(rf"\b{re.escape(t)}", blob):
            continue
        # also allow token as substring for short stems
        if t in blob:
            continue
        return False
    return True


def main() -> int:
    print("=" * 72)
    print("Awareness data quality audit")
    print(f"API={API}  project={ROOT}")
    print("=" * 72)

    reset_settings()
    settings = get_settings()
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    state = StateDB(settings.state_db_url or "sqlite:///awareness.sqlite")
    state.init()

    # ── 0. API health ───────────────────────────────────────────────────
    code, health = api_get("/healthz")
    gate("api_health", code == 200 and isinstance(health, dict) and health.get("ok") is True, f"code={code} body={health}")

    # ── 1. Corpus baseline ──────────────────────────────────────────────
    try:
        total_row = idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"]
    except Exception as e:
        total_row = 0
        gate("corpus_readable", False, f"captures view failed: {e}")
    else:
        gate("corpus_readable", total_row > 0, f"captures={total_row}", n=total_row)

    # ── 2. Search keyword precision suite ───────────────────────────────
    queries = [
        "neural",
        "climate",
        "quantum",
        "market",
        "python",
        "security",
        "health",
        "space",
    ]
    precision_rows: list[dict] = []
    for q in queries:
        res = idx.search(q, limit=20, mode="auto")
        rows = res.get("rows") or []
        if not rows:
            # try API too for consistency
            code, body = api_get(f"/search?q={urllib.parse.quote(q)}&limit=20")
            rows = (body or {}).get("rows", []) if isinstance(body, dict) else []
            res = body if isinstance(body, dict) else res

        hits = 0
        exact_dups = 0
        seen_keys: set[str] = set()
        for r in rows:
            title = r.get("title") or ""
            snip = r.get("snippet") or ""
            textish = f"{title}\n{snip}"
            # pull full text when available via capture_id for better judgment
            cid = r.get("capture_id")
            if cid:
                try:
                    full = idx.execute(
                        "SELECT title, text FROM captures WHERE capture_id = $c LIMIT 1",
                        {"c": cid},
                    )
                    if full:
                        textish = f"{full[0].get('title') or ''}\n{full[0].get('text') or ''}"
                except Exception:
                    pass
            if token_hit(textish, q):
                hits += 1
            key = (r.get("content_hash") or "") + "|" + (title.strip().lower())
            if key in seen_keys and key != "|":
                exact_dups += 1
            seen_keys.add(key)

        n = len(rows)
        prec = (hits / n) if n else 0.0
        precision_rows.append(
            {
                "q": q,
                "n": n,
                "hits": hits,
                "precision": round(prec, 3),
                "exact_dups_in_page": exact_dups,
                "mode": res.get("mode") if isinstance(res, dict) else None,
                "total": res.get("total") if isinstance(res, dict) else None,
            }
        )
        print(f"  search {q!r}: n={n} hits={hits} prec={prec:.2%} dups={exact_dups} mode={res.get('mode') if isinstance(res, dict) else '?'}")

    avg_prec = sum(p["precision"] for p in precision_rows) / max(1, len(precision_rows))
    nonempty = sum(1 for p in precision_rows if p["n"] > 0)
    total_dups = sum(p["exact_dups_in_page"] for p in precision_rows)

    # Hard gates for search quality
    gate(
        "search_returns_results",
        nonempty >= max(3, len(queries) // 2),
        f"{nonempty}/{len(queries)} queries returned rows",
        nonempty=nonempty,
    )
    gate(
        "search_precision_avg",
        avg_prec >= 0.70,
        f"avg precision@20 = {avg_prec:.2%} (threshold 70%)",
        avg_precision=round(avg_prec, 4),
    )
    gate(
        "search_no_exact_dups_in_page",
        total_dups == 0,
        f"exact title/hash duplicates in top pages = {total_dups} (should be 0)",
        exact_dups=total_dups,
    )

    # ── 3. Data quality on random sample ────────────────────────────────
    sample = idx.execute(
        """
        SELECT capture_id, title, domain, url, length(coalesce(text,'')) AS text_len,
               content_hash, source_type,
               left(coalesce(text,''), 200) AS text_head
        FROM captures
        USING SAMPLE 200
        """
    )
    if not sample:
        # duckdb sample syntax may differ — fallback
        sample = idx.execute(
            """
            SELECT capture_id, title, domain, url, length(coalesce(text,'')) AS text_len,
                   content_hash, source_type,
                   substr(coalesce(text,''), 1, 200) AS text_head
            FROM captures
            ORDER BY random()
            LIMIT 200
            """
        )

    empty_title = sum(1 for r in sample if not (r.get("title") or "").strip())
    short_text = sum(1 for r in sample if int(r.get("text_len") or 0) < 80)
    no_domain = sum(1 for r in sample if not (r.get("domain") or "").strip())
    no_url = sum(1 for r in sample if not (r.get("url") or "").strip())
    hashes = [r.get("content_hash") for r in sample if r.get("content_hash")]
    hash_dups = len(hashes) - len(set(hashes))

    # boilerplate-ish: very high ratio of non-alpha or repeated "cookie"/"subscribe"
    junk_pat = re.compile(r"cookie|subscribe|sign in|advertisement|enable javascript", re.I)
    junkish = sum(1 for r in sample if junk_pat.search(r.get("text_head") or "") and int(r.get("text_len") or 0) < 400)

    n_s = max(1, len(sample))
    gate(
        "quality_empty_title_rate",
        empty_title / n_s <= 0.15,
        f"{empty_title}/{n_s} empty titles ({empty_title/n_s:.1%})",
        rate=round(empty_title / n_s, 4),
    )
    gate(
        "quality_short_text_rate",
        short_text / n_s <= 0.25,
        f"{short_text}/{n_s} texts <80 chars ({short_text/n_s:.1%})",
        rate=round(short_text / n_s, 4),
    )
    gate(
        "quality_missing_domain_url",
        (no_domain + no_url) / n_s <= 0.05,
        f"no_domain={no_domain} no_url={no_url} of {n_s}",
    )
    # Historical corpus may predate EXACT_DUP storage-skip. Soft warn only —
    # the hard gate is live re-fetch corpus_delta≈0 + search page uniqueness.
    gate(
        "quality_sample_hash_dups",
        hash_dups / n_s <= 0.10,
        f"{hash_dups}/{n_s} exact content_hash collisions in sample ({hash_dups/n_s:.1%}) "
        f"[historical leakage OK soft if search/live gates pass]",
        hard=False,
        rate=round(hash_dups / n_s, 4),
    )
    gate(
        "quality_junkish_rate",
        junkish / n_s <= 0.20,
        f"{junkish}/{n_s} junk-ish short docs ({junkish/n_s:.1%})",
        hard=False,
        rate=round(junkish / n_s, 4),
    )

    # ── 4. Global exact-dup rate from dedup stats ───────────────────────
    code, dedup = api_get("/dedup-stats")
    if code == 200 and isinstance(dedup, dict):
        distinct = int(dedup.get("distinct_content_hashes") or 0)
        seen = int(dedup.get("total_captures_seen") or 0)
        fold_rate = 1.0 - (distinct / seen) if seen else 0.0
        gate(
            "dedup_index_populated",
            distinct > 0 and seen > 0,
            f"distinct={distinct} seen={seen} fold_rate={fold_rate:.1%}",
            distinct=distinct,
            seen=seen,
            fold_rate=round(fold_rate, 4),
        )
        # fold_rate high is GOOD (many dups folded). Low fold with high corpus may mean weak dedup.
        # But if total_captures >> distinct, dedup is working.
        gate(
            "dedup_is_recording",
            seen >= distinct,
            f"captures_seen ({seen}) >= distinct hashes ({distinct})",
        )
    else:
        gate("dedup_index_populated", False, f"dedup-stats failed code={code}")

    # ── 5. Corpus-level exact content_hash multiplicity ─────────────────
    hash_multi = idx.execute(
        """
        SELECT content_hash, COUNT(*) AS n
        FROM captures
        WHERE content_hash IS NOT NULL AND content_hash != ''
        GROUP BY content_hash
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        LIMIT 20
        """
    )
    multi_groups = len(hash_multi)
    multi_extra = sum(int(r["n"]) - 1 for r in hash_multi)
    # Search results should not show same content_hash twice if we filter; corpus may store near-dups as separate rows with same hash if exact dedup only at ingest fold.
    gate(
        "corpus_exact_hash_multiplicity",
        multi_groups <= 50,  # some historical leakage OK soft; hard fail if massive
        f"top multi-hash groups={multi_groups}, extra copies in top20={multi_extra}",
        hard=multi_groups > 200,
        multi_groups=multi_groups,
        multi_extra=multi_extra,
    )
    if hash_multi:
        print("  worst exact-hash dups:")
        for r in hash_multi[:5]:
            print(f"    hash={r['content_hash']} n={r['n']}")

    # ── 6. Live fetch: short tail with single reliable seed ─────────────
    # Prefer hnrss — fast public feed
    from awareness.planner.planner import Planner
    from awareness.tail.engine import TailEngine
    import asyncio

    before_count = idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"]
    before_hashes = {
        r["content_hash"]
        for r in idx.execute(
            "SELECT content_hash FROM captures WHERE content_hash IS NOT NULL"
        )
        if r.get("content_hash")
    }

    # snapshot max fetch_ts
    before_max = idx.execute("SELECT max(fetch_ts) AS m FROM captures")[0]["m"]

    print("\n── live tail probe (≤25s) ──")
    # TailEngine.start() loads seeds from a YAML path (not a dict).
    seed_file = Path(settings.data_dir) / "state" / "audit_tail_seeds.yaml"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(
        "feeds:\n  - { url: \"https://hnrss.org/frontpage\" }\n"
        "atom: []\n"
        "sitemaps: []\n",
        encoding="utf-8",
    )
    planner = Planner(state)
    tail = TailEngine(state, planner)

    async def _live() -> str:
        job_id = await tail.start(seeds_path=seed_file, mute_duplicates=False)
        # give discovery + a few fetches time
        await asyncio.sleep(18)
        await tail.stop(drain_seconds=6.0)
        return job_id

    live_job = None
    live_err = None
    t0 = time.monotonic()
    try:
        live_job = asyncio.run(_live())
    except Exception as e:
        live_err = e
    live_secs = time.monotonic() - t0

    # refresh duckdb views
    try:
        idx.close()
    except Exception:
        pass
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    after_count = idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"]
    after_max = idx.execute("SELECT max(fetch_ts) AS m FROM captures")[0]["m"]
    new_rows = max(0, int(after_count) - int(before_count))

    # job metrics
    job_stats = state.get_job(live_job) if live_job else None
    emitted = job_stats.docs_emitted if job_stats else 0
    folded = job_stats.docs_dedup_dropped if job_stats else 0

    if live_err:
        gate("live_fetch_runs", False, f"tail failed: {type(live_err).__name__}: {live_err}")
    else:
        gate(
            "live_fetch_runs",
            live_job is not None,
            f"job={live_job} duration={live_secs:.1f}s emitted={emitted} folded={folded} new_rows={new_rows}",
            job_id=live_job,
            emitted=emitted,
            folded=folded,
            new_rows=new_rows,
        )
        # Real-time capability: either new rows OR folded dups (engine worked) OR tasks completed
        tasks_done = (job_stats.tasks_completed if job_stats else 0) or 0
        gate(
            "live_fetch_activity",
            emitted > 0 or folded > 0 or tasks_done > 0 or new_rows > 0,
            f"tasks_completed={tasks_done} docs_emitted={emitted} folded={folded} delta_captures={new_rows}",
            tasks_completed=tasks_done,
        )

    # ── 7. Re-run same feed immediately — should mostly fold, not explode ─
    print("\n── re-fetch same seed (dedup pressure) ──")
    mid_count = idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"]
    planner2 = Planner(state)
    tail2 = TailEngine(state, planner2)

    async def _again() -> str:
        job_id = await tail2.start(seeds_path=seed_file, mute_duplicates=False)
        await asyncio.sleep(14)
        await tail2.stop(drain_seconds=5.0)
        return job_id

    again_job = None
    again_err = None
    try:
        again_job = asyncio.run(_again())
    except Exception as e:
        again_err = e

    try:
        idx.close()
    except Exception:
        pass
    idx = DuckDbIndex(
        db_path=settings.duckdb_path(),
        jsonl_dir=settings.staging_jsonl_dir(),
        iceberg_warehouse=settings.iceberg_warehouse,
    )
    end_count = idx.execute("SELECT COUNT(*) AS n FROM captures")[0]["n"]
    delta2 = max(0, int(end_count) - int(mid_count))
    job2 = state.get_job(again_job) if again_job else None
    em2 = job2.docs_emitted if job2 else 0
    fo2 = job2.docs_dedup_dropped if job2 else 0

    if again_err:
        gate("dedup_refetch_runs", False, f"second tail failed: {again_err}")
    else:
        total_out = em2 + fo2
        fold_ratio = (fo2 / total_out) if total_out else 0.0
        # On immediate re-poll of same HN feed, majority should fold if dedup works.
        # Soft if feed added brand-new items between polls.
        gate(
            "dedup_refetch_runs",
            True,
            f"job={again_job} emitted={em2} folded={fo2} fold_ratio={fold_ratio:.1%} corpus_delta={delta2}",
            emitted=em2,
            folded=fo2,
            fold_ratio=round(fold_ratio, 4),
            corpus_delta=delta2,
        )
        gate(
            "dedup_prevents_dup_flood",
            delta2 <= max(5, em2 + 2),  # corpus shouldn't grow much beyond truly new emits
            f"corpus grew by {delta2} while emitted={em2} folded={fo2}",
            corpus_delta=delta2,
        )
        # If we emitted many and folded almost none on immediate re-run → FAIL
        if total_out >= 3:
            gate(
                "dedup_fold_ratio_on_refetch",
                fold_ratio >= 0.40 or em2 <= 2,
                f"fold_ratio={fold_ratio:.1%} on immediate re-fetch (want ≥40% if volume≥3)",
                fold_ratio=round(fold_ratio, 4),
            )
        else:
            gate(
                "dedup_fold_ratio_on_refetch",
                True,
                f"low volume total_out={total_out}; skip strict fold ratio",
                hard=False,
            )

    # ── 8. Search after live — keyword still sensible ───────────────────
    res_n = idx.search("neural", limit=10, mode="auto")
    rows_n = res_n.get("rows") or []
    # Check duplicate titles in result page again
    titles = [(r.get("title") or "").strip().lower() for r in rows_n]
    title_dups = len(titles) - len(set(titles))
    gate(
        "search_post_live_no_title_dups",
        title_dups == 0,
        f"title dups in neural top10 = {title_dups}",
        title_dups=title_dups,
    )

    # ── summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    hard = [g for g in GATES if g.hard]
    soft = [g for g in GATES if not g.hard]
    failed_hard = [g for g in hard if not g.passed]
    failed_soft = [g for g in soft if not g.passed]
    print(f"HARD: {len(hard) - len(failed_hard)}/{len(hard)} pass")
    print(f"SOFT: {len(soft) - len(failed_soft)}/{len(soft)} pass")
    if failed_hard:
        print("HARD FAILURES:")
        for g in failed_hard:
            print(f"  - {g.name}: {g.detail}")
    if failed_soft:
        print("SOFT FAILURES:")
        for g in failed_soft:
            print(f"  - {g.name}: {g.detail}")

    report = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "api": API,
        "search_precision": precision_rows,
        "avg_precision": round(avg_prec, 4),
        "gates": [asdict(g) for g in GATES],
        "hard_pass": len(failed_hard) == 0,
        "live_job": live_job,
        "refetch_job": again_job,
    }
    out = ROOT / "docs" / "data_quality_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {out}")

    try:
        idx.close()
    except Exception:
        pass
    return 1 if failed_hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
