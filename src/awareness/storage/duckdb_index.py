"""DuckDB-backed query/index layer.

Two views over the same corpus:

1. ``staging_captures`` — read JSONL chunks from
   ``data/jsonl/captures/YYYY/MM/DD/*.jsonl`` / ``*.jsonl.gz`` (not writer temps)
   directly. This is always present and is the source-of-truth for the latest writes.
2. ``iceberg_captures`` — read the Iceberg table when present.

A combined ``captures`` view UNIONs both with row-level dedup on
``capture_id``. This makes range queries trivial:

    SELECT count(*) FROM captures
     WHERE fetch_ts BETWEEN '2024-01-01' AND '2024-12-31';
"""

from __future__ import annotations

from datetime import datetime
import contextlib
import hashlib
import random
import threading
import time
from pathlib import Path
from typing import Any

import duckdb

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

from awareness.obs.logging import get_logger
from awareness.util.lang import append_language_filter, primary_language_sql

logger = get_logger("storage.duckdb")


# Fingerprint of the on-disk corpus used to decide whether a persisted FTS
# index is still valid (path/mtime/size tuples can be large; store a digest).
def _signature_digest(sig: tuple[Any, ...] | None) -> str:
    if sig is None:
        return ""
    return hashlib.sha256(repr(sig).encode("utf-8")).hexdigest()


# ── search configuration surface ─────────────────────────────────────────────
# Columns the caller may point a search at. Mapped 1:1 onto the ``captures``
# view so an attacker can never inject an arbitrary column name.
ALLOWED_SEARCH_FIELDS: tuple[str, ...] = ("title", "text", "domain", "url")
DEFAULT_SEARCH_FIELDS: tuple[str, ...] = ("title", "text")

# Matching strategies, from strict to broad:
#   fts       — BM25-ranked full-text (stemmed, stop-worded). Precise, fast.
#   prefix    — stem-root substring per token (finance -> financ% -> financial).
#   substring — raw ILIKE on the whole query string. No tokenization.
#   auto      — FTS first; if it returns nothing, fall back to prefix. Default.
# Quoted whole-query ("phrase") uses substring matching; response mode is "phrase".
SEARCH_MODES: tuple[str, ...] = ("auto", "fts", "prefix", "substring")
DEFAULT_SEARCH_MODE = "auto"
# Hard ceiling on rows materialized in a single search call (overload guard).
DEFAULT_SEARCH_MAX_RESULTS = 200

# ── re-rank (BM25F-style) tuning ──────────────────────────────────────────────
# DuckDB FTS scores title+text as one blob, so it cannot field-boost. We re-rank
# the top-`max_results` BM25 candidates with independent, bounded multiplicative
# factors; all-neutral collapses to identity (pure BM25 order preserved).
_RERANK_TITLE_BOOST = 0.5        # Wt: full title-term coverage multiplies score by 1+Wt
_RERANK_LEN_PIVOT = 4000         # chars; docs up to here are not length-damped
_RERANK_LEN_FLOOR = 0.75         # the most a very long doc can be damped to
_RERANK_RECENCY_WEIGHT = 0.0     # Wr: 0 disables the recency prior (off by default)
_RERANK_RECENCY_HALFLIFE_DAYS = 30.0

# Canonical captures columns in UNION order. The staging projection is built
# from this so a JSONL chunk missing any of these still yields a buildable
# `captures` view (absent columns become typed NULLs) instead of Binder-erroring
# the whole view out of existence.
_CAPTURE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("doc_id", "VARCHAR"), ("capture_id", "VARCHAR"), ("parent_doc_or_dup_group", "VARCHAR"),
    ("source_type", "VARCHAR"), ("source_name", "VARCHAR"), ("source_locator", "VARCHAR"),
    ("source_shard", "VARCHAR"), ("source_offset_or_record_id", "VARCHAR"),
    ("discovery_channel", "VARCHAR"), ("job_id", "VARCHAR"), ("batch_id", "VARCHAR"),
    ("ingest_version", "VARCHAR"), ("url", "VARCHAR"), ("canonical_url", "VARCHAR"),
    ("domain", "VARCHAR"),
    ("fetch_ts", "TIMESTAMPTZ"), ("observed_ts", "TIMESTAMPTZ"),
    ("published_ts", "TIMESTAMPTZ"), ("last_modified", "TIMESTAMPTZ"),
    ("content_type", "VARCHAR"), ("http_status", "INTEGER"), ("etag", "VARCHAR"),
    ("title", "VARCHAR"), ("text", "VARCHAR"), ("language", "VARCHAR"),
    ("content_hash", "VARCHAR"), ("near_dup_hash", "BIGINT"),
    ("robots_decision", "VARCHAR"), ("terms_note_if_relevant", "VARCHAR"),
)
_TS_COLUMNS = frozenset({"fetch_ts", "observed_ts", "published_ts", "last_modified"})



def _is_staging_jsonl(path: Path) -> bool:
    """True for finalized staging chunks (plain or gzip), not writer temps.

    Writer temps are named ``*.jsonl.tmp`` / ``*.jsonl.gz.tmp``; a bare
    suffix-star glob would pick those up mid-write. Only exact final
    suffixes are accepted.
    """
    name = path.name
    return name.endswith(".jsonl") or name.endswith(".jsonl.gz")


def _iter_staging_jsonl(captures_root: Path):
    """Yield finalized ``*.jsonl`` / ``*.jsonl.gz`` files under *captures_root*."""
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        yield from captures_root.rglob(pattern)


def _staging_projection(present: set[str]) -> str:
    """SELECT-list over staging_captures_raw that NULL-fills absent columns and
    TRY_CASTs timestamps / near_dup_hash, matching the iceberg-union order."""
    parts: list[str] = []
    for name, typ in _CAPTURE_COLUMNS:
        if name not in present:
            parts.append(f"NULL::{typ} AS {name}")
        elif name in _TS_COLUMNS:
            parts.append(f"TRY_CAST({name} AS TIMESTAMPTZ) AS {name}")
        elif name == "near_dup_hash":
            parts.append(f"TRY_CAST({name} AS BIGINT) AS {name}")
        else:
            parts.append(name)
    return ",\n                      ".join(parts)


def _clean_fields(fields: list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize + whitelist the requested search columns.

    Unknown columns are dropped silently; an empty/None request falls back
    to :data:`DEFAULT_SEARCH_FIELDS`. This is the *only* place a field name
    reaches SQL, so the whitelist here is the injection boundary.
    """
    if not fields:
        return list(DEFAULT_SEARCH_FIELDS)
    seen: list[str] = []
    for f in fields:
        key = (f or "").strip().lower()
        if key in ALLOWED_SEARCH_FIELDS and key not in seen:
            seen.append(key)
    return seen or list(DEFAULT_SEARCH_FIELDS)


@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout: float = 60.0):
    """Process-level file lock with exponential backoff retry."""
    if not HAS_FCNTL:
        yield
        return

    lock_file = None
    start_time = time.monotonic()
    attempt = 0
    base_delay = 0.05
    max_delay = 2.0
    
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w")
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - start_time >= timeout:
                    raise TimeoutError(f"Timeout waiting for file lock on {lock_path}")
                delay = min(max_delay, base_delay * (2 ** attempt))
                time.sleep(delay * (0.5 + random.random()))
                attempt += 1
        yield
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError:
                pass
            lock_file.close()

# ── search configuration surface ─────────────────────────────────────────────
# Columns the caller may point a search at. Mapped 1:1 onto the ``captures``
# view so an attacker can never inject an arbitrary column name.
ALLOWED_SEARCH_FIELDS: tuple[str, ...] = ("title", "text", "domain", "url")
DEFAULT_SEARCH_FIELDS: tuple[str, ...] = ("title", "text")

# Matching strategies, from strict to broad:
#   fts       — BM25-ranked full-text (stemmed, stop-worded). Precise, fast.
#   prefix    — stem-root substring per token (finance -> financ% -> financial).
#   substring — raw ILIKE on the whole query string. No tokenization.
#   auto      — FTS first; if it returns nothing, fall back to prefix. Default.
SEARCH_MODES: tuple[str, ...] = ("auto", "fts", "prefix", "substring")
DEFAULT_SEARCH_MODE = "auto"
# Hard ceiling on rows materialized in a single search call (overload guard).
DEFAULT_SEARCH_MAX_RESULTS = 200


def _clean_fields(fields: list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize + whitelist the requested search columns.

    Unknown columns are dropped silently; an empty/None request falls back
    to :data:`DEFAULT_SEARCH_FIELDS`. This is the *only* place a field name
    reaches SQL, so the whitelist here is the injection boundary.
    """
    if not fields:
        return list(DEFAULT_SEARCH_FIELDS)
    seen: list[str] = []
    for f in fields:
        key = (f or "").strip().lower()
        if key in ALLOWED_SEARCH_FIELDS and key not in seen:
            seen.append(key)
    return seen or list(DEFAULT_SEARCH_FIELDS)


def _collapse_key(row: dict[str, Any]) -> str:
    """Key used to fold near/exact-duplicate search hits into one result.

    Priority:
      1. ``parent_doc_or_dup_group`` — near-dup / exact-dup cluster id when set
      2. ``content_hash`` — true content identity
      3. lower(title)|domain — syndicated copies missing hash + parent
    """
    parent = row.get("parent_doc_or_dup_group")
    if parent is not None:
        parent_s = str(parent).strip()
        if parent_s:
            return f"p:{parent_s}"
    ch = row.get("content_hash")
    if ch is not None:
        ch_s = str(ch).strip()
        if ch_s:
            return f"h:{ch_s}"
    title = (row.get("title") or "").strip().lower()
    domain = (row.get("domain") or "").strip().lower()
    return f"t:{title}|{domain}"


def _collapse_search_rows(
    rows: list[dict[str, Any]],
    *,
    prefer_higher_score: bool = True,
) -> list[dict[str, Any]]:
    """Collapse rows to unique content.

    Input order is preserved for first-seen keys. When ``prefer_higher_score``
    is True (default) and a later row shares the same collapse key with a
    strictly higher ``score``, it replaces the earlier one (ties keep the
    first). When False, first-seen always wins — used after re-rank so the
    best final-rank hit is not displaced by a later higher raw BM25 near-dup.
    Unscored rows (prefix/substring) keep first-seen order (already sorted by
    fetch_ts DESC upstream).
    """
    if not rows:
        return []
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in rows:
        key = _collapse_key(r)
        prev = best.get(key)
        if prev is None:
            best[key] = r
            order.append(key)
            continue
        if not prefer_higher_score:
            continue
        prev_score = prev.get("score")
        cur_score = r.get("score")
        if cur_score is None:
            continue
        if prev_score is None or float(cur_score) > float(prev_score):
            best[key] = r
    return [best[k] for k in order]



def _serialize_window_bound(value: Any) -> Any:
    """JSON-friendly form of a search window bound (datetime → ISO-8601)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def build_search_diagnostics(
    *,
    mode_used: str,
    fts_available: bool,
    query_terms: list[str],
    corpus_size: int | None = None,
    start: Any = None,
    end: Any = None,
    source: str | None = None,
    domain: str | None = None,
    language: str | None = None,
    requested_mode: str | None = None,
) -> dict[str, Any]:
    """Build an empty-result diagnostic payload with actionable hints.

    Heavier fields such as ``corpus_size`` are expected to be precomputed by
    the caller only when ``total == 0`` so successful queries stay cheap.
    """
    hints: list[str] = []
    if corpus_size is not None and corpus_size <= 0:
        hints.append("No documents in index yet — run a backfill or start tail.")
    else:
        # Non-empty corpus (or unknown size) but zero hits.
        if not fts_available and (requested_mode or mode_used) in ("auto", "fts", "prefix", "substring"):
            if mode_used in ("prefix", "substring") or not fts_available:
                hints.append("FTS unavailable; used prefix/substring mode.")
        if start is not None:
            hints.append("Date window may exclude older captures.")
        if source or domain or language:
            parts: list[str] = []
            if domain:
                parts.append("domain")
            if source:
                parts.append("source")
            if language:
                parts.append("language")
            hints.append(
                f"{'/'.join(parts).capitalize()} filter may exclude matching captures."
            )
        if corpus_size is None or corpus_size > 0:
            # Always offer a recall tip when the corpus is non-empty / unknown.
            if mode_used == "phrase":
                # Quoted exact-phrase path — do not suggest substring (already used).
                hints.append(
                    "No exact phrase matches; try without quotes or fewer words."
                )
            elif mode_used != "substring" or len(query_terms) > 1:
                hints.append("Try fewer terms or substring mode.")
            else:
                # substring + single term still missed — keep hints non-empty.
                hints.append(
                    "No substring matches; try different terms or drop domain/source/language filters."
                )

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_hints: list[str] = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            unique_hints.append(h)

    diag: dict[str, Any] = {
        "mode_used": mode_used,
        "fts_available": bool(fts_available),
        "query_terms": list(query_terms),
        "hints": unique_hints,
    }
    if corpus_size is not None:
        diag["corpus_size"] = int(corpus_size)
    if start is not None or end is not None:
        diag["window"] = {
            "start": _serialize_window_bound(start),
            "end": _serialize_window_bound(end),
        }
    # Surface active filters so CLI/SPA empty-state UX can show what was applied
    # (hints alone don't make the active domain/source/language filter obvious).
    filters: dict[str, str] = {}
    if domain:
        filters["domain"] = str(domain)
    if source:
        filters["source"] = str(source)
    if language:
        filters["language"] = str(language)
    if filters:
        diag["filters"] = filters
    return diag


class DuckDbIndex:
    """Thin wrapper around a DuckDB connection that knows our layout."""

    _extensions_installed = False
    _instances: dict[Path, DuckDbIndex] = {}

    def __new__(cls, db_path: Path, jsonl_dir: Path, iceberg_warehouse: Path | str | None) -> DuckDbIndex:
        resolved_db_path = Path(db_path).resolve()
        if resolved_db_path not in cls._instances:
            inst = super().__new__(cls)
            cls._instances[resolved_db_path] = inst
            inst._initialized = False
        return cls._instances[resolved_db_path]

    def __init__(self, db_path: Path, jsonl_dir: Path, iceberg_warehouse: Path | str | None) -> None:
        if getattr(self, "_initialized", False):
            return
        self._db_path = Path(db_path).resolve()
        self._jsonl_dir = Path(jsonl_dir).resolve()
        self._iceberg_warehouse = iceberg_warehouse
        self._lock = threading.RLock()
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._fts_available: bool | None = None
        self._fts_built_for_count: int = -1
        self._fts_built_signature: tuple[Any, ...] | None = None
        # Cache the (path, size, mtime) signature of the source files so we
        # only rebuild views when the corpus on disk actually changed. Without
        # this, every query paid a full view-refresh (~150ms over a few JSONL
        # chunks); steady-state queries are now bound by the query itself.
        self._views_signature: tuple[Any, ...] | None = None
        # BM25F avg field lengths (title, text) keyed by views_signature so
        # repeated searches skip the full-table AVG(length(...)) scan while
        # the corpus fingerprint is unchanged.
        self._bm25_avg_lengths: tuple[float, float] | None = None
        self._bm25_avg_lengths_signature: tuple[Any, ...] | None = None
        # Observability / tests: how FTS was last satisfied on this instance.
        self._fts_full_rebuilds: int = 0
        self._fts_incremental_appends: int = 0
        self._fts_restores: int = 0
        self._initialized = True

    @contextlib.contextmanager
    def _connection_context(self):
        """Yield the long-lived DuckDB connection for a locked critical section.

        Callers (``search``/``execute``/``refresh``) hold ``self._lock`` for the
        full critical section. We reuse ``connect()``'s memoized ``self._conn``
        instead of open/close per call so hot search paths skip connection setup
        and extension reload. View freshness is preserved via
        ``_refresh_views_if_stale`` on every entry (same as ``related()``).
        """
        # connect() re-enters self._lock (RLock) and opens at most once.
        conn = self.connect()
        self._refresh_views_if_stale(conn)
        yield conn

    def _configure_connection(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Apply session defaults and load optional extensions on a new connection."""
        # Canonical session timezone so TIMESTAMPTZ values stay UTC
        # (otherwise local TZ shifts display/filter edges by hours).
        try:
            conn.execute("SET TimeZone = 'UTC'")
        except duckdb.Error as exc:
            logger.info("duckdb_timezone_set_failed", err=str(exc))

        if not DuckDbIndex._extensions_installed:
            try:
                conn.execute("INSTALL iceberg")
                conn.execute("INSTALL fts")
                DuckDbIndex._extensions_installed = True
            except duckdb.Error as exc:
                logger.info("duckdb_extensions_install_failed", err=str(exc))

        try:
            conn.execute("LOAD iceberg")
            conn.execute("SET unsafe_enable_version_guessing = true;")
        except duckdb.Error as exc:
            logger.info("duckdb_iceberg_extension_unavailable", err=str(exc))

        try:
            conn.execute("LOAD fts")
            self._fts_available = True
        except duckdb.Error as exc:
            logger.info("duckdb_fts_extension_unavailable", err=str(exc))
            self._fts_available = False

    def connect(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            
            lock_path = self._db_path.with_suffix(".lock")
            with _file_lock(lock_path):
                conn = None
                max_retries = 10
                base_delay = 0.05
                max_delay = 1.0
                
                for attempt in range(max_retries):
                    try:
                        conn = duckdb.connect(str(self._db_path))
                        break
                    except duckdb.Error as exc:
                        exc_str = str(exc).lower()
                        is_lock_error = any(
                            msg in exc_str
                            for msg in ("lock", "resource temporarily unavailable", "permission", "locked", "io error")
                        )
                        if is_lock_error and attempt < max_retries - 1:
                            time.sleep(min(max_delay, base_delay * (2 ** attempt)) * (0.5 + random.random()))
                        else:
                            raise
                
                self._configure_connection(conn)
                self._refresh_views_if_stale(conn)
                self._conn = conn
                return conn

    def _staging_globs(self) -> list[str]:
        # Recursive fallback when partition scan finds nothing: only patterns
        # that match at least one finalized chunk (DuckDB rejects empty globs).
        captures_root = self._jsonl_dir / "captures"
        globs: list[str] = []
        try:
            if any(p.is_file() for p in captures_root.rglob("*.jsonl") if _is_staging_jsonl(p)):
                globs.append(str(captures_root / "**" / "*.jsonl"))
            if any(p.is_file() for p in captures_root.rglob("*.jsonl.gz") if _is_staging_jsonl(p)):
                globs.append(str(captures_root / "**" / "*.jsonl.gz"))
        except OSError:
            return []
        return globs

    def _get_partition_globs(self) -> list[str]:
        """Scan captures directory to find leaf partition directories containing JSONL files.

        Returns dual glob patterns per active partition (``*.jsonl`` and
        ``*.jsonl.gz``) so writer temps (``*.jsonl.tmp`` / ``*.jsonl.gz.tmp``)
        are never read. Example:
        ``['/path/to/captures/2026/06/01/*.jsonl', '.../01/*.jsonl.gz']``.
        """
        captures_root = self._jsonl_dir / "captures"
        if not captures_root.exists():
            return []

        partition_dirs = set()
        try:
            for p in _iter_staging_jsonl(captures_root):
                try:
                    if p.is_file() and _is_staging_jsonl(p):
                        partition_dirs.add(p.parent)
                except OSError:
                    continue
        except OSError:
            pass

        if not partition_dirs:
            return []

        # Only emit patterns that match at least one finalized file — DuckDB
        # read_json_auto fails if any list entry matches nothing.
        globs: list[str] = []
        for d in sorted(partition_dirs):
            if any(p.is_file() and _is_staging_jsonl(p) for p in d.glob("*.jsonl")):
                globs.append(str(d / "*.jsonl"))
            if any(p.is_file() and _is_staging_jsonl(p) for p in d.glob("*.jsonl.gz")):
                globs.append(str(d / "*.jsonl.gz"))
        return globs

    def _source_signature(self) -> tuple[Any, ...]:
        """Cheap fingerprint of the on-disk corpus (JSONL chunks + Iceberg metadata).

        Per-file path + mtime_ns + size so content replacement at the same
        path (or same row count) still invalidates views and FTS. Iceberg
        metadata mtimes cover snapshot advances.
        """
        captures_root = self._jsonl_dir / "captures"
        jsonl: tuple[Any, ...] = ()
        if captures_root.exists():
            entries = []
            try:
                for p in _iter_staging_jsonl(captures_root):
                    try:
                        if p.is_file() and _is_staging_jsonl(p):
                            st = p.stat()
                            entries.append((str(p), st.st_mtime_ns, st.st_size))
                    except OSError:
                        continue
            except OSError:
                pass
            jsonl = tuple(sorted(entries))
        iceberg: tuple[Any, ...] = ()
        wh = self._iceberg_warehouse
        if wh and not str(wh).startswith(("s3://", "s3a://", "gs://", "gcs://")):
            meta_dir = Path(wh).resolve() / "awareness" / "captures" / "metadata"
            if meta_dir.exists():
                try:
                    iceberg = tuple(
                        sorted((str(p), p.stat().st_mtime_ns) for p in meta_dir.glob("*.json"))
                    )
                except OSError:
                    iceberg = ()
        return (jsonl, iceberg)

    def _refresh_views_if_stale(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Rebuild views only when the source-file signature changed."""
        sig = self._source_signature()
        if sig != self._views_signature:
            self._refresh_views(conn)
            self._views_signature = sig

    def _refresh_views(self, conn: duckdb.DuckDBPyConnection) -> None:
        captures_root = self._jsonl_dir / "captures"
        has_files = False
        if captures_root.exists():
            try:
                for p in _iter_staging_jsonl(captures_root):
                    if p.is_file() and _is_staging_jsonl(p):
                        has_files = True
                        break
            except OSError:
                pass

        if has_files:
            globs = self._get_partition_globs()
            if not globs:
                globs = self._staging_globs()

            glob_list = ", ".join(f"'{g}'" for g in globs)
            conn.execute(  # nosemgrep
                f"""
                CREATE OR REPLACE VIEW staging_captures_raw AS
                SELECT *
                FROM read_json_auto([{glob_list}], union_by_name=true);
                """
            )
        else:
            conn.execute(
                """
                CREATE OR REPLACE VIEW staging_captures_raw AS
                SELECT
                  NULL::VARCHAR AS doc_id, NULL::VARCHAR AS capture_id,
                  NULL::VARCHAR AS source_type, NULL::VARCHAR AS source_name,
                  NULL::VARCHAR AS fetch_ts, NULL::VARCHAR AS observed_ts,
                  NULL::VARCHAR AS published_ts, NULL::VARCHAR AS last_modified,
                  NULL::VARCHAR AS url, NULL::VARCHAR AS canonical_url,
                  NULL::VARCHAR AS domain, NULL::VARCHAR AS text,
                  NULL::VARCHAR AS title, NULL::VARCHAR AS language,
                  NULL::VARCHAR AS content_hash, NULL::BIGINT AS near_dup_hash,
                  NULL::VARCHAR AS discovery_channel,
                  NULL::VARCHAR AS source_locator, NULL::VARCHAR AS source_shard,
                  NULL::VARCHAR AS source_offset_or_record_id,
                  NULL::VARCHAR AS job_id, NULL::VARCHAR AS batch_id,
                  NULL::VARCHAR AS parent_doc_or_dup_group,
                  NULL::VARCHAR AS ingest_version,
                  NULL::VARCHAR AS robots_decision,
                  NULL::VARCHAR AS terms_note_if_relevant,
                  NULL::VARCHAR AS content_type, NULL::INTEGER AS http_status,
                  NULL::VARCHAR AS etag
                WHERE 1=0;
                """
            )

        # Build a unified ``captures`` view that casts timestamps to TIMESTAMPTZ
        # so BETWEEN/range queries against datetime parameters work.
        # We try to load and union the Iceberg warehouse captures when available.
        present = {
            str(r[0]) for r in conn.execute("DESCRIBE staging_captures_raw").fetchall()
        }
        staging_proj = _staging_projection(present)
        iceberg_ok = False
        if self._iceberg_warehouse:
            try:
                if str(self._iceberg_warehouse).startswith(("s3://", "s3a://", "gs://", "gcs://")):
                    table_path = f"{self._iceberg_warehouse}/awareness/captures"
                else:
                    table_path = str(Path(self._iceberg_warehouse).resolve() / "awareness" / "captures")
                
                is_valid = True
                if not str(self._iceberg_warehouse).startswith(("s3://", "s3a://", "gs://", "gcs://")):
                    is_valid = Path(table_path).exists()
                
                if is_valid:
                    conn.execute("SET unsafe_enable_version_guessing = true;")
                    # table_path is derived from operator config, not request input.
                    conn.execute(  # nosemgrep
                        f"""
                        CREATE OR REPLACE VIEW iceberg_captures_raw AS
                        SELECT * FROM iceberg_scan('{table_path}');
                        """
                    )
                    iceberg_ok = True
            except Exception as exc:
                logger.info("duckdb_iceberg_view_setup_skipped", err=str(exc))

        try:
            if iceberg_ok:
                conn.execute(  # nosemgrep
                    f"""
                    CREATE OR REPLACE VIEW captures_raw_union AS
                    SELECT
                      {staging_proj}
                    FROM staging_captures_raw
                    UNION ALL
                    SELECT
                      doc_id, capture_id, parent_doc_or_dup_group,
                      source_type, source_name, source_locator,
                      source_shard, source_offset_or_record_id,
                      discovery_channel, job_id, batch_id, ingest_version,
                      url, canonical_url, domain,
                      TRY_CAST(fetch_ts AS TIMESTAMPTZ) AS fetch_ts,
                      TRY_CAST(observed_ts AS TIMESTAMPTZ) AS observed_ts,
                      TRY_CAST(published_ts AS TIMESTAMPTZ) AS published_ts,
                      TRY_CAST(last_modified AS TIMESTAMPTZ) AS last_modified,
                      content_type, http_status, etag, title, text, language,
                      content_hash, TRY_CAST(near_dup_hash AS BIGINT) AS near_dup_hash, robots_decision,
                      terms_note_if_relevant
                    FROM iceberg_captures_raw;
                    """
                )
                conn.execute(
                    """
                    CREATE OR REPLACE VIEW captures AS
                    SELECT * EXCLUDE (rn) FROM (
                        SELECT *,
                               ROW_NUMBER() OVER (PARTITION BY capture_id ORDER BY fetch_ts DESC) AS rn
                        FROM captures_raw_union
                    ) WHERE rn = 1;
                    """
                )
            else:
                conn.execute(  # nosemgrep
                    f"""
                    CREATE OR REPLACE VIEW captures AS
                    SELECT
                      {staging_proj}
                    FROM staging_captures_raw;
                    """
                )
            # Backwards-compat alias.
            conn.execute("CREATE OR REPLACE VIEW staging_captures AS SELECT * FROM captures;")
        except duckdb.Error as exc:
            logger.warning("duckdb_view_setup_failed", err=str(exc))

    def refresh(self) -> None:
        with self._lock, self._connection_context() as conn:
            self._refresh_views(conn)
            self._views_signature = self._source_signature()

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connection_context() as conn:
            cur = conn.execute(sql, params or {})
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def related(self, capture_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """Sibling captures in the same dup-group, lock-guarded.

        Wraps the module-level :func:`find_related_captures` under ``self._lock``
        so a process-wide singleton can serve ``/related`` from FastAPI's
        threadpool without ever touching the raw connection unsynchronized.
        """
        with self._lock:
            conn = self.connect()
            self._refresh_views_if_stale(conn)
            return find_related_captures(conn, capture_id, limit=limit)

    def related_count(self, capture_id: str) -> int:
        """Total sibling captures in the same dup-group (excludes *capture_id*)."""
        with self._lock:
            conn = self.connect()
            self._refresh_views_if_stale(conn)
            return count_related_captures(conn, capture_id)

    def health_snapshot(self) -> dict[str, Any]:
        """Cheap readiness probe for /healthz (views, row count, FTS flags).

        Does not force an FTS rebuild — only reports whether the extension
        loaded and whether an FTS index has already been built for the
        current corpus signature.
        """
        try:
            with self._lock:
                conn = self.connect()
                self._refresh_views_if_stale(conn)
                try:
                    row = conn.execute("SELECT COUNT(*) FROM captures").fetchone()
                    captures = int(row[0]) if row else 0
                except duckdb.Error as exc:
                    return {
                        "ready": False,
                        "error": f"captures_view: {exc}",
                        "fts_extension": bool(self._fts_available),
                        "fts_built": False,
                        "captures": 0,
                    }
                fts_built = (
                    self._fts_built_signature is not None
                    and self._fts_built_signature == self._views_signature
                    and self._fts_built_for_count >= 0
                )
                return {
                    "ready": True,
                    "captures": captures,
                    "fts_extension": bool(self._fts_available),
                    "fts_built": bool(fts_built),
                    "fts_built_for_count": int(self._fts_built_for_count),
                    "views_signature_present": self._views_signature is not None,
                    "db_path": str(self._db_path),
                    "jsonl_dir": str(self._jsonl_dir),
                }
        except Exception as exc:  # noqa: BLE001 — health must never raise
            return {
                "ready": False,
                "error": f"{type(exc).__name__}: {exc}",
                "fts_extension": bool(self._fts_available) if self._fts_available is not None else False,
                "fts_built": False,
                "captures": 0,
            }

    # ── full-text search ────────────────────────────────────────────────
    _CAPTURES_IDX_SELECT = """
                SELECT
                  capture_id, doc_id, parent_doc_or_dup_group,
                  source_type, source_name, discovery_channel,
                  url, canonical_url, domain,
                  fetch_ts, observed_ts, published_ts,
                  title, text, language, content_hash, near_dup_hash,
                  robots_decision
                FROM captures
                """

    _FTS_CREATE_PRAGMA = (
        "PRAGMA create_fts_index('captures_idx', 'capture_id', 'title', 'text', "
        "overwrite=1, stemmer='english', stopwords='english')"
    )

    @staticmethod
    def _main_table_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
        try:
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name = ? LIMIT 1",
                [name],
            ).fetchone()
            return row is not None
        except duckdb.Error:
            return False

    def _persist_fts_signature(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Store corpus fingerprint so a later process can reuse captures_idx+FTS."""
        dig = _signature_digest(self._views_signature)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS awareness_index_meta (
              key VARCHAR PRIMARY KEY,
              value VARCHAR
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO awareness_index_meta VALUES (?, ?)",
            ["fts_source_signature", dig],
        )

    def _load_persisted_fts_signature(self, conn: duckdb.DuckDBPyConnection) -> str | None:
        try:
            if not self._main_table_exists(conn, "awareness_index_meta"):
                return None
            row = conn.execute(
                "SELECT value FROM awareness_index_meta WHERE key = ? LIMIT 1",
                ["fts_source_signature"],
            ).fetchone()
            return str(row[0]) if row and row[0] is not None else None
        except duckdb.Error:
            return None

    def _mark_fts_ready(self, count: int) -> None:
        self._fts_built_for_count = count
        self._fts_built_signature = self._views_signature
        # captures_idx content changed → drop stale BM25F length cache.
        self._bm25_avg_lengths = None
        self._bm25_avg_lengths_signature = None

    def _try_restore_persisted_fts(self, conn: duckdb.DuckDBPyConnection) -> bool:
        """Reuse on-disk captures_idx + FTS when corpus fingerprint still matches.

        Avoids a full rematerialize + create_fts_index after process restart when
        JSONL/Iceberg sources are unchanged (C3-T1 persisted FTS).
        """
        if self._views_signature is None:
            return False
        if not self._main_table_exists(conn, "captures_idx"):
            return False
        stored = self._load_persisted_fts_signature(conn)
        if stored is None or stored != _signature_digest(self._views_signature):
            return False
        try:
            # Probe FTS schema still present and queryable.
            conn.execute("SELECT COUNT(*) FROM fts_main_captures_idx.docs").fetchone()
            row = conn.execute("SELECT COUNT(*) FROM captures_idx").fetchone()
            count = int(row[0]) if row else 0
            if count == 0:
                return False
            self._fts_built_for_count = count
            self._fts_built_signature = self._views_signature
            self._fts_restores += 1
            logger.info("duckdb_fts_index_restored", rows=count)
            return True
        except duckdb.Error:
            return False

    def _try_incremental_fts_append(self, conn: duckdb.DuckDBPyConnection, new_count: int) -> bool:
        """If captures grew by insert-only ids, delta-INSERT then rebuild FTS.

        DuckDB FTS has no public partial-update API, so the inverted index is
        still rebuilt; we skip re-scanning the entire JSONL-backed view into a
        brand-new captures_idx table when old rows are still valid.
        """
        if not self._main_table_exists(conn, "captures_idx"):
            return False
        try:
            old_row = conn.execute("SELECT COUNT(*) FROM captures_idx").fetchone()
            old_count = int(old_row[0]) if old_row else 0
        except duckdb.Error:
            return False
        if old_count <= 0 or new_count <= old_count:
            return False
        try:
            missing = conn.execute(
                """
                SELECT COUNT(*) FROM captures_idx i
                WHERE NOT EXISTS (
                  SELECT 1 FROM captures c WHERE c.capture_id = i.capture_id
                )
                """
            ).fetchone()
            if missing is None or int(missing[0]) > 0:
                return False
            conn.execute(
                """
                INSERT INTO captures_idx
                SELECT
                  c.capture_id, c.doc_id, c.parent_doc_or_dup_group,
                  c.source_type, c.source_name, c.discovery_channel,
                  c.url, c.canonical_url, c.domain,
                  c.fetch_ts, c.observed_ts, c.published_ts,
                  c.title, c.text, c.language, c.content_hash, c.near_dup_hash,
                  c.robots_decision
                FROM captures c
                WHERE NOT EXISTS (
                  SELECT 1 FROM captures_idx i WHERE i.capture_id = c.capture_id
                )
                """
            )
            conn.execute(self._FTS_CREATE_PRAGMA)
            self._mark_fts_ready(new_count)
            self._persist_fts_signature(conn)
            self._fts_incremental_appends += 1
            logger.info(
                "duckdb_fts_index_appended",
                rows=new_count,
                prev_rows=old_count,
            )
            return True
        except duckdb.Error as exc:
            logger.warning("duckdb_fts_incremental_failed", err=str(exc))
            return False

    def _full_rebuild_fts(self, conn: duckdb.DuckDBPyConnection, count: int) -> bool:
        try:
            conn.execute(
                f"""
                CREATE OR REPLACE TABLE captures_idx AS
                {self._CAPTURES_IDX_SELECT}
                """
            )
            conn.execute(self._FTS_CREATE_PRAGMA)
            self._mark_fts_ready(count)
            self._persist_fts_signature(conn)
            self._fts_full_rebuilds += 1
            logger.info("duckdb_fts_index_built", rows=count)
            return True
        except duckdb.Error as exc:
            logger.warning("duckdb_fts_build_failed", err=str(exc))
            self._fts_available = False
            return False

    def _ensure_fts(self, conn: duckdb.DuckDBPyConnection) -> bool:
        """Build/refresh the FTS index on a materialized captures table.

        DuckDB's FTS extension requires a real table. We materialize the
        captures view into ``captures_idx`` and rebuild whenever the source
        signature changes (not merely row count), so content replacement at
        the same cardinality still refreshes BM25.

        C3-T1 optimizations:
        * **Persisted reuse** — if ``captures_idx`` + FTS + meta fingerprint
          still match the corpus, skip rebuild after process restart.
        * **Append-only** — when only new ``capture_id``s appear, INSERT the
          delta into ``captures_idx`` then recreate the FTS index (no full
          rematerialize from JSONL).

        Returns True if FTS is ready to use.
        """
        if not self._fts_available:
            return False
        # Fast path: the source files are unchanged since we last built the
        # index, so skip the COUNT(*) probe (which scans the JSON-backed view).
        if self._fts_built_signature is not None and self._fts_built_signature == self._views_signature:
            return True
        # Process-restart / cold open: reuse on-disk FTS if fingerprint matches.
        if self._try_restore_persisted_fts(conn):
            return True
        # Current corpus size.
        try:
            row = conn.execute("SELECT COUNT(*) FROM captures").fetchone()
            if not row:
                return False
            count = int(row[0])
        except duckdb.Error:
            return False
        if count == 0:
            return False
        # Pure growth: delta-INSERT then rebuild inverted index only.
        if self._try_incremental_fts_append(conn, count):
            return True
        return self._full_rebuild_fts(conn, count)

    def _bm25_field_avg_lengths(self, conn: duckdb.DuckDBPyConnection) -> tuple[float, float]:
        """Return (avg_title_len, avg_text_len) for BM25F, memoized by views_signature.

        Avg field lengths are corpus-global constants for a given index
        fingerprint. Recomputing ``AVG(length(...))`` over ``captures_idx`` on
        every search is pure overhead on steady-state queries; cache until the
        source signature (and thus FTS rebuild) changes.
        """
        sig = self._views_signature
        if (
            self._bm25_avg_lengths is not None
            and self._bm25_avg_lengths_signature is not None
            and self._bm25_avg_lengths_signature == sig
        ):
            return self._bm25_avg_lengths
        avg_row = conn.execute(
            "SELECT CAST(avg(length(coalesce(title, ''))) AS DOUBLE) as avg_title, "
            "CAST(avg(length(coalesce(text, ''))) AS DOUBLE) as avg_text FROM captures_idx"
        ).fetchone()
        avg_title = float(avg_row[0] or 1.0) if avg_row else 1.0
        avg_text = float(avg_row[1] or 1.0) if avg_row else 1.0
        if avg_title <= 0.0:
            avg_title = 1.0
        if avg_text <= 0.0:
            avg_text = 1.0
        lengths = (avg_title, avg_text)
        self._bm25_avg_lengths = lengths
        self._bm25_avg_lengths_signature = sig
        return lengths

    def _bm25_term_dfs(self, conn: duckdb.DuckDBPyConnection, terms: list[str]) -> dict[str, int]:
        """Batch-fetch document frequencies for stemmed terms (one dict query).

        Missing terms map to df=0. Dedupes *terms* so multi-hit queries pay
        one round-trip regardless of repeated stems.
        """
        if not terms:
            return {}
        unique = list(dict.fromkeys(terms))
        placeholders = ", ".join(f"$df_term_{i}" for i in range(len(unique)))
        params = {f"df_term_{i}": t for i, t in enumerate(unique)}
        try:
            rows = conn.execute(
                f"SELECT term, CAST(df AS INTEGER) FROM fts_main_captures_idx.dict "
                f"WHERE term IN ({placeholders})",
                params,
            ).fetchall()
        except duckdb.Error:
            return {t: 0 for t in unique}
        found = {str(term): int(df) for term, df in rows}
        return {t: found.get(t, 0) for t in unique}

    def _stem_roots(self, conn: duckdb.DuckDBPyConnection, terms: list[str]) -> list[str]:
        """Reduce each query token to its Snowball stem (its prefix root).

        ``finance`` -> ``financ`` so a substring match on the root also
        catches ``financial``/``finances``/``financing``. Falls back to the
        raw token if the ``stem`` scalar is unavailable.
        """
        roots: list[str] = []
        for t in terms:
            root = t
            if self._fts_available:
                try:
                    val = conn.execute("SELECT stem(?, 'english')", [t]).fetchone()
                    if val and val[0]:
                        root = str(val[0])
                except duckdb.Error:
                    root = t
            if root and root not in roots:
                roots.append(root)
        return roots


    def _match_facets(
        self,
        conn: duckdb.DuckDBPyConnection,
        *,
        kind: str,
        params: dict[str, Any],
        base: str | None = None,
        where_sql: str | None = None,
        limit: int = 10,
    ) -> dict[str, list[dict[str, Any]]]:
        """Top domains/sources for the current match set (cheap SQL GROUP BY).

        ``kind`` is ``"fts"`` (BM25 join via *base*) or ``"where"`` (ILIKE path
        with *where_sql*). Only called when the match total is > 0 so the extra
        aggregates stay cheap relative to the search itself.
        """
        lim = max(1, min(int(limit), 50))
        if kind == "fts" and base:
            # Placeholders live only inside *base* (terms + range/source filters).
            p = {
                k: v
                for k, v in params.items()
                if f"${k}" in base or k.startswith("term_")
            }
            # Case-normalize domains so Example.com / example.com roll into one
            # SPA chip (matches lower(domain) filter semantics).
            dom_sql = (
                f"SELECT lower(CAST(c.domain AS VARCHAR)) AS domain, "
                f"COUNT(DISTINCT c.capture_id)::BIGINT AS n "
                f"{base} "
                f"WHERE c.domain IS NOT NULL AND CAST(c.domain AS VARCHAR) != '' "
                f"GROUP BY 1 ORDER BY n DESC LIMIT {lim}"
            )
            # Case-normalize so RSS / rss / Rss roll into one SPA chip (matches
            # lower(source_type) filter semantics used by source= query params).
            src_sql = (
                f"SELECT lower(CAST(c.source_type AS VARCHAR)) AS source_type, "
                f"COUNT(DISTINCT c.capture_id)::BIGINT AS n "
                f"{base} "
                f"WHERE c.source_type IS NOT NULL AND CAST(c.source_type AS VARCHAR) != '' "
                f"GROUP BY 1 ORDER BY n DESC LIMIT {lim}"
            )
            # Primary BCP-47 tags so en / en-US / en_GB roll into one "en" chip.
            lang_prim = primary_language_sql("c.language")
            lang_sql = (
                f"SELECT {lang_prim} AS language, "
                f"COUNT(DISTINCT c.capture_id)::BIGINT AS n "
                f"{base} "
                f"WHERE c.language IS NOT NULL AND CAST(c.language AS VARCHAR) != '' "
                f"AND {lang_prim} != '' "
                f"GROUP BY 1 ORDER BY n DESC LIMIT {lim}"
            )
        else:
            w = where_sql or "1=1"
            p = {k: v for k, v in params.items() if f"${k}" in w}
            # Case-normalize domains so mixed-case corpus values share one chip.
            dom_sql = (
                f"SELECT lower(CAST(domain AS VARCHAR)) AS domain, "
                f"COUNT(*)::BIGINT AS n FROM captures "
                f"WHERE ({w}) AND domain IS NOT NULL AND CAST(domain AS VARCHAR) != '' "
                f"GROUP BY 1 ORDER BY n DESC LIMIT {lim}"
            )
            # Case-normalize so RSS / rss roll into one chip (filter is lower()).
            src_sql = (
                f"SELECT lower(CAST(source_type AS VARCHAR)) AS source_type, "
                f"COUNT(*)::BIGINT AS n FROM captures "
                f"WHERE ({w}) AND source_type IS NOT NULL "
                f"AND CAST(source_type AS VARCHAR) != '' "
                f"GROUP BY 1 ORDER BY n DESC LIMIT {lim}"
            )
            # Primary BCP-47 tags so SPA chips match filter semantics (language=en).
            lang_prim = primary_language_sql("language")
            lang_sql = (
                f"SELECT {lang_prim} AS language, COUNT(*)::BIGINT AS n FROM captures "
                f"WHERE ({w}) AND language IS NOT NULL "
                f"AND CAST(language AS VARCHAR) != '' "
                f"AND {lang_prim} != '' "
                f"GROUP BY 1 ORDER BY n DESC LIMIT {lim}"
            )
        try:
            domains = self._rows(conn, dom_sql, p)
            sources = self._rows(conn, src_sql, p)
            languages = self._rows(conn, lang_sql, p)
        except duckdb.Error as exc:
            logger.warning("search_facets_failed", kind=kind, err=str(exc))
            return {"domains": [], "sources": [], "languages": []}
        for row in domains:
            row["n"] = int(row["n"])
        for row in sources:
            row["n"] = int(row["n"])
        for row in languages:
            row["n"] = int(row["n"])
        return {"domains": domains, "sources": sources, "languages": languages}

    def search(
        self,
        query: str,
        *,
        limit: int = 30,
        offset: int = 0,
        source: str | None = None,
        domain: str | None = None,
        language: str | None = None,
        start: Any = None,
        end: Any = None,
        mode: str = DEFAULT_SEARCH_MODE,
        fields: list[str] | tuple[str, ...] | None = None,
        max_results: int | None = DEFAULT_SEARCH_MAX_RESULTS,
    ) -> dict[str, Any]:
        """Search the corpus.

        ``mode`` selects the matching strategy (see :data:`SEARCH_MODES`).
        ``auto`` runs BM25 FTS first and, only if it finds nothing, retries
        with stem-root prefix matching so e.g. ``finance`` still surfaces
        ``financial``. ``fields`` restricts substring/prefix matching to a
        whitelisted subset of :data:`ALLOWED_SEARCH_FIELDS`. ``max_results``
        is a hard ceiling on rows materialized in one call (overload guard).
        ``language`` filters by BCP-47 tag (case-insensitive). Primary tags
        (``en``) also match regional subtags (``en-US``, ``en-GB``).
        """
        query = (query or "").strip()
        mode = (mode or DEFAULT_SEARCH_MODE).strip().lower()
        if mode not in SEARCH_MODES:
            mode = DEFAULT_SEARCH_MODE
        # Quoted whole-query → exact phrase (ILIKE %phrase%), skip FTS/tokenization.
        # Detect leading+trailing double quotes on the stripped query.
        # Matching uses the substring path; response mode is labeled "phrase".
        phrase_query = _phrase_query(query)
        is_phrase = phrase_query is not None
        if is_phrase:
            query = phrase_query
            mode = "substring"
        cols = _clean_fields(fields)

        # Page bounds. Do NOT rewrite offset past max_results — that corrupts
        # client page state (SPA/CLI keep their own offset and see the last
        # window forever). Over-range pages return empty rows instead.
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        page_beyond_cap = False
        if max_results is not None and max_results > 0:
            if offset >= max_results:
                page_beyond_cap = True
            else:
                # A single page never extends past the overload ceiling.
                limit = max(1, min(limit, max_results - offset))
        # Candidate window for collapse-then-page: fetch up to the overload
        # ceiling (no SQL offset) so exact dups don't under-fill the page.
        candidate_cap = (
            int(max_results)
            if max_results is not None and max_results > 0
            else DEFAULT_SEARCH_MAX_RESULTS
        )
        # Still honor an explicit offset+limit inside the cap for the window.
        if not page_beyond_cap:
            candidate_cap = max(candidate_cap, offset + limit)

        mode_label = "phrase" if is_phrase else mode
        empty = {
            "total": 0, "limit": limit, "offset": offset, "rows": [],
            "ranked": False, "mode": mode_label, "fields": cols, "query": query,
        }
        if not query:
            empty["diagnostics"] = build_search_diagnostics(
                mode_used=mode_label,
                fts_available=bool(self._fts_available),
                query_terms=[],
                corpus_size=None,
                start=start,
                end=end,
                source=source,
                domain=domain,
                language=language,
                requested_mode=mode,
            )
            return empty

        with self._lock, self._connection_context() as conn:

            # Shared range/source/domain/language filters.
            def base_filters() -> tuple[list[str], dict[str, Any]]:
                w: list[str] = []
                p: dict[str, Any] = {}
                if source:
                    # Case-insensitive: SPA/CLI may pass RSS vs rss / Tail_Recrawl.
                    w.append("lower(source_type) = $src")
                    p["src"] = str(source).strip().lower()
                if domain:
                    # Case-insensitive: users/SPA may pass Example.COM while
                    # captures store lower-cased eTLD+1 from URL parsing.
                    w.append("lower(domain) = $dom")
                    p["dom"] = str(domain).strip().lower()
                # BCP-47: primary tags match regional subtags (en → en-US).
                append_language_filter(w, p, language)
                if start is not None:
                    w.append("fetch_ts >= $start")
                    p["start"] = start
                if end is not None:
                    w.append("fetch_ts <= $end")
                    p["end"] = end
                return w, p

            total = 0
            rows: list[dict[str, Any]] = []
            used_mode = mode
            requested_mode = mode
            # (kind, base|None, where_sql|None, params) for cheap facet GROUP BY.
            facet_ctx: tuple[str, str | None, str | None, dict[str, Any]] | None = None

            # FTS indexes title+text as one blob, so it cannot honor a
            # narrowed ``fields`` request. Explicit ``fts`` still runs (escape
            # hatch); ``auto`` only takes the ranked path when fields are the
            # default, otherwise it routes to field-aware prefix matching.
            fts_eligible = set(cols) == set(DEFAULT_SEARCH_FIELDS)

            # ── 1) FTS (ranked) path: used for fts/auto when available ──────
            if mode == "fts" or (mode == "auto" and fts_eligible):
                fts = self._ensure_fts(conn)
                if fts:
                    import math
                    from awareness.config import get_settings

                    where, params = base_filters()
                    terms = _tokenize_query(query)
                    stemmed_terms = []
                    for t in terms:
                        val = conn.execute("SELECT stem(?, 'english')", [t]).fetchone()
                        stemmed_terms.append(val[0] if (val and val[0]) else t)

                    stemmed_terms = list(dict.fromkeys(stemmed_terms))

                    if stemmed_terms:
                        # Get total docs from stats or captures_idx
                        num_docs_row = conn.execute("SELECT CAST(num_docs AS INTEGER) FROM fts_main_captures_idx.stats LIMIT 1").fetchone()
                        N = num_docs_row[0] if num_docs_row else 1
                        if N <= 0:
                            count_row = conn.execute("SELECT COUNT(*) FROM captures_idx").fetchone()
                            N = count_row[0] if count_row else 1

                        # Avg field lengths: memoized per views_signature.
                        avg_title, avg_text = self._bm25_field_avg_lengths(conn)

                        # Batch DF lookups (one dict query) then BM25 IDF.
                        term_dfs = self._bm25_term_dfs(conn, stemmed_terms)
                        term_idfs = {}
                        for st in stemmed_terms:
                            df = term_dfs.get(st, 0)
                            idf = math.log(1.0 + (N - df + 0.5) / (df + 0.5))
                            term_idfs[st] = max(0.0, idf)

                        # IDF threshold validation. Skip pruning on tiny corpora:
                        # with N docs a term present in every document still has
                        # IDF ≈ log(1.33) ≈ 0.29, so a default threshold of 1.0
                        # would wipe legitimate queries during smoke/tests and
                        # early ingestion.
                        settings = get_settings()
                        threshold = float(getattr(settings, "search_idf_threshold", 1.0) or 0.0)
                        recency_weight = float(getattr(settings, "search_recency_boost", 0.0) or 0.0)
                        _MIN_DOCS_FOR_IDF_PRUNE = 20
                        if N < _MIN_DOCS_FOR_IDF_PRUNE or threshold <= 0.0:
                            valid_terms = list(stemmed_terms)
                        else:
                            valid_terms = [st for st in stemmed_terms if term_idfs.get(st, 0.0) >= threshold]
                            if len(valid_terms) < len(stemmed_terms):
                                dropped = [st for st in stemmed_terms if term_idfs.get(st, 0.0) < threshold]
                                logger.info(
                                    "bm25f_low_idf_terms_dropped",
                                    dropped=dropped,
                                    threshold=threshold,
                                )
                            # Never drop every term — fall back to the full query.
                            if not valid_terms:
                                valid_terms = list(stemmed_terms)

                        # Build BM25F SQL using FTS join tables
                        term_placeholders = ", ".join(f"$term_{i}" for i in range(len(valid_terms)))
                        for i, st in enumerate(valid_terms):
                            params[f"term_{i}"] = st

                        params["avg_title"] = max(1.0, avg_title)
                        params["avg_text"] = max(1.0, avg_text)
                        params["num_docs_n"] = max(1, N)

                        where_sql = (" AND " + " AND ".join(where)) if where else ""

                        base = f"""
                            FROM (
                              SELECT 
                                c.capture_id,
                                d.df,
                                sum(CASE WHEN t.fieldid = 0 THEN 1 ELSE 0 END) AS tf_title,
                                sum(CASE WHEN t.fieldid = 1 THEN 1 ELSE 0 END) AS tf_text
                              FROM captures_idx c
                              JOIN fts_main_captures_idx.docs docs ON c.capture_id = docs.name
                              JOIN fts_main_captures_idx.terms t ON docs.docid = t.docid
                              JOIN fts_main_captures_idx.dict d ON t.termid = d.termid
                              WHERE d.term IN ({term_placeholders})
                                {where_sql}
                              GROUP BY c.capture_id, t.termid, d.df
                            ) sub
                            JOIN captures_idx c USING (capture_id)
                        """

                        count_params = {k: v for k, v in params.items() if f"${k}" in base or "term_" in k}
                        total_row = conn.execute(f"SELECT COUNT(DISTINCT capture_id) {base}", count_params).fetchone()
                        total = int(total_row[0]) if total_row else 0
                        if total > 0:
                            sql = f"""
                                SELECT
                                  c.capture_id, c.doc_id, c.parent_doc_or_dup_group,
                                  c.source_type, c.source_name, c.discovery_channel,
                                  c.url, c.canonical_url, c.domain,
                                  c.fetch_ts, c.observed_ts, c.published_ts,
                                  c.title, c.text, c.language, c.content_hash,
                                  sum(
                                    ln(1.0 + ($num_docs_n - sub.df + 0.5) / (sub.df + 0.5)) * (
                                      (
                                        (2.0 * sub.tf_title / greatest(1.0 + 0.3 * (length(coalesce(c.title, '')) / $avg_title - 1.0), 0.0001)) + 
                                        (1.0 * sub.tf_text / greatest(1.0 + 0.75 * (length(coalesce(c.text, '')) / $avg_text - 1.0), 0.0001))
                                      ) / (
                                        1.2 + (
                                          (2.0 * sub.tf_title / greatest(1.0 + 0.3 * (length(coalesce(c.title, '')) / $avg_title - 1.0), 0.0001)) + 
                                          (1.0 * sub.tf_text / greatest(1.0 + 0.75 * (length(coalesce(c.text, '')) / $avg_text - 1.0), 0.0001))
                                        )
                                      )
                                    )
                                  ) as score
                                {base}
                                GROUP BY 
                                  c.capture_id, c.doc_id, c.parent_doc_or_dup_group,
                                  c.source_type, c.source_name, c.discovery_channel,
                                  c.url, c.canonical_url, c.domain,
                                  c.fetch_ts, c.observed_ts, c.published_ts,
                                  c.title, c.text, c.language, c.content_hash
                                ORDER BY score DESC, c.capture_id ASC
                                LIMIT {int(candidate_cap)}
                            """
                            candidates = self._rows(conn, sql, params)
                            # Title/length (and optional recency) re-rank before
                            # collapse+page so field boost is not lost to LIMIT.
                            # recency_weight from settings.search_recency_boost
                            # (0=off); uses published_ts, else fetch_ts.
                            rows = _rerank(
                                candidates,
                                terms,
                                recency_weight=recency_weight,
                            )
                            used_mode = "fts"
                            facet_ctx = ("fts", base, None, params)
                elif mode == "fts":
                    # FTS explicitly requested but unavailable → degrade to prefix.
                    mode = "prefix"

            # ── 2) prefix / substring (unranked) path ──────────────────────
            snippet_terms = _tokenize_query(query)
            if not rows and (mode in ("prefix", "substring") or (mode == "auto" and total == 0)):
                where, params = base_filters()
                terms = _tokenize_query(query)

                if mode == "substring" or not terms:
                    where.insert(0, "(" + " OR ".join(f"{c} ILIKE $like" for c in cols) + ")")
                    params["like"] = f"%{query}%"
                    used_mode = "substring"
                else:
                    # Match ANY token's stem-root in ANY chosen field (OR-by-default,
                    # matching the FTS path so multi-word recall is consistent).
                    roots = self._stem_roots(conn, terms)
                    snippet_terms = roots or terms
                    or_clauses: list[str] = []
                    for i, root in enumerate(roots):
                        key = f"r{i}"
                        params[key] = f"%{root}%"
                        or_clauses.extend(f"{c} ILIKE ${key}" for c in cols)
                    if or_clauses:
                        where.insert(0, "(" + " OR ".join(or_clauses) + ")")
                    used_mode = "prefix"

                # where_sql interpolates ONLY whitelisted column names (via
                # _clean_fields) and $-bound placeholders; all user values are
                # bound params. No untrusted string reaches the SQL text.
                where_sql = " AND ".join(where) if where else "1=1"
                # Unique-content total: parent group, else content_hash, else title|domain.
                collapse_expr = (
                    "CASE "
                    "WHEN parent_doc_or_dup_group IS NOT NULL "
                    " AND CAST(parent_doc_or_dup_group AS VARCHAR) != '' "
                    "THEN 'p:' || CAST(parent_doc_or_dup_group AS VARCHAR) "
                    "WHEN content_hash IS NOT NULL AND CAST(content_hash AS VARCHAR) != '' "
                    "THEN 'h:' || CAST(content_hash AS VARCHAR) "
                    "ELSE 't:' || lower(trim(coalesce(title, ''))) || '|' || lower(coalesce(domain, '')) "
                    "END"
                )
                total_row = conn.execute(  # nosemgrep
                    f"SELECT COUNT(*) FROM ("  # noqa: S608
                    f"  SELECT 1 FROM captures WHERE {where_sql} "
                    f"  GROUP BY {collapse_expr}"
                    f") u",
                    params,
                ).fetchone()
                total = int(total_row[0]) if total_row else 0
                sql = (
                    "SELECT"  # noqa: S608
                    "  capture_id, doc_id, parent_doc_or_dup_group,"
                    "  source_type, source_name, discovery_channel,"
                    "  url, canonical_url, domain,"
                    "  fetch_ts, observed_ts, published_ts,"
                    "  title, text, language, content_hash,"
                    "  NULL::DOUBLE AS score "
                    "FROM captures "
                    f"WHERE {where_sql} "
                    "ORDER BY fetch_ts DESC, capture_id ASC "
                    f"LIMIT {int(candidate_cap)}"
                )
                rows = self._rows(conn, sql, params)
                if total > 0:
                    facet_ctx = ("where", None, where_sql, params)

            # Quoted whole-query: keep substring matching, surface mode=phrase.
            if is_phrase:
                used_mode = "phrase"

            ranked = used_mode == "fts"

            # Collapse near/exact dups (parent group / hash / title|domain)
            # before pagination so top-K never shows syndicated copies twice.
            # Ranked (post-rerank) lists keep first-seen: re-rank order is the
            # authority, not raw BM25. Unranked lists may still promote a later
            # higher-scored hit when scores exist.
            pre_collapse_n = len(rows)
            rows = _collapse_search_rows(rows, prefer_higher_score=not ranked)
            unique_n = len(rows)
            if used_mode == "fts":
                # FTS COUNT is per capture_id; fold to unique content. When the
                # candidate window held every match, unique_n is exact.
                if total <= candidate_cap and pre_collapse_n >= total:
                    total = unique_n
                elif pre_collapse_n < candidate_cap:
                    # Window not full ⇒ every match was fetched; unique is exact.
                    total = unique_n
                else:
                    # Lower-bound unique total using dups observed in-window.
                    total = max(unique_n, total - (pre_collapse_n - unique_n))
            else:
                # Prefix path already counted unique groups in SQL; still prefer
                # the candidate-window unique count when we saw the full set.
                if pre_collapse_n < candidate_cap:
                    total = unique_n
                else:
                    total = max(unique_n, total)

            # Pageable ceiling: clients (SPA Next, CLI) treat total as the last
            # reachable index+1. Never report more than max_results when set.
            if max_results is not None and max_results > 0:
                total = min(total, int(max_results))

            # Page after collapse (+ re-rank for FTS). Beyond-cap offsets yield [].
            if page_beyond_cap:
                rows = []
            else:
                rows = rows[offset : offset + limit]

            # Augment each row with a snippet + matched terms; strip the heavy
            # full text from the response payload.
            results = []
            for r in rows:
                text = r.pop("text", None) or ""
                title = r.get("title") or ""
                snippet, hits = _snippet_for(text, title, snippet_terms)
                r["snippet"] = snippet
                r["snippet_hits"] = hits
                r["text_len"] = len(text)
                r["terms"] = snippet_terms
                results.append(r)
            payload: dict[str, Any] = {
                "total": total,
                "limit": limit,
                "offset": offset,
                "rows": results,
                "ranked": ranked,
                "mode": used_mode,
                "fields": cols,
                "query": query,
            }
            # Surface active recency re-rank weight when configured (>0).
            # SPA/CLI can show "recency=…" without re-reading settings.
            try:
                from awareness.config import get_settings as _get_settings
                _rw = float(getattr(_get_settings(), "search_recency_boost", 0.0) or 0.0)
            except Exception:
                _rw = 0.0
            if _rw > 0:
                payload["recency_boost"] = _rw
            # Facets over the matching set (not just this page) when total > 0.
            if total > 0 and facet_ctx is not None:
                f_kind, f_base, f_where, f_params = facet_ctx
                payload["facets"] = self._match_facets(
                    conn,
                    kind=f_kind,
                    base=f_base,
                    where_sql=f_where,
                    params=f_params,
                    limit=10,
                )
            # Empty-result diagnostics (corpus_size only when total==0).
            if total == 0:
                corpus_size: int | None = None
                try:
                    crow = conn.execute("SELECT COUNT(*) FROM captures").fetchone()
                    corpus_size = int(crow[0]) if crow else 0
                except duckdb.Error:
                    corpus_size = None
                payload["diagnostics"] = build_search_diagnostics(
                    mode_used=used_mode,
                    fts_available=bool(self._fts_available),
                    # Original tokens (not stem roots) so diagnostics mirror the user query.
                    query_terms=_tokenize_query(query),
                    corpus_size=corpus_size,
                    start=start,
                    end=end,
                    source=source,
                    domain=domain,
                    language=language,
                    requested_mode=requested_mode,
                )
            return payload

    @staticmethod
    def _rows(conn: duckdb.DuckDBPyConnection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if hasattr(self, "_db_path") and self._db_path in self._instances:
                del self._instances[self._db_path]
            self._initialized = False


# ── snippet helpers ────────────────────────────────────────────────────
def _phrase_query(q: str) -> str | None:
    """If *q* is a double-quoted whole-query phrase, return the inner text.

    ``"machine learning"`` → ``machine learning``. Leading/trailing whitespace
    on the outer query is already stripped by the caller; the inner phrase is
    also stripped. Unbalanced quotes, mid-query quotes, or bare text → ``None``
    (caller keeps normal tokenized/FTS behavior). Empty ``""`` → ``""``.
    """
    if len(q) >= 2 and q[0] == '"' and q[-1] == '"':
        return q[1:-1].strip()
    return None


def _tokenize_query(q: str) -> list[str]:
    import re

    return [t for t in re.findall(r"[A-Za-z0-9']+", q.lower()) if len(t) >= 2]


def _title_hit_frac(title: str, terms: list[str]) -> float:
    """Fraction of distinct query terms that occur in the title (case-insensitive)."""
    if not terms:
        return 0.0
    low = (title or "").lower()
    hits = sum(1 for t in terms if t and t in low)
    return hits / len(terms)


def _length_factor(text_len: int, *, pivot: int = _RERANK_LEN_PIVOT, floor: float = _RERANK_LEN_FLOOR) -> float:
    """1.0 for docs up to `pivot` chars, decaying toward `floor` for longer ones.

    Tames over-long blobs the single-field BM25 length-norm under-penalizes,
    while never zeroing a document out (bounded in [floor, 1.0]).
    """
    if pivot <= 0 or text_len <= pivot:
        return 1.0
    over = text_len - pivot
    return floor + (1.0 - floor) * (pivot / (pivot + over))


def _to_epoch(ts: Any) -> float | None:
    """Best-effort epoch-seconds from a datetime / number / ISO-8601 string."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.timestamp()
    try:
        return float(ts)  # already epoch seconds
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _recency_factor(
    doc_epoch: float | None,
    ref_epoch: float | None,
    *,
    halflife_days: float = _RERANK_RECENCY_HALFLIFE_DAYS,
    weight: float = _RERANK_RECENCY_WEIGHT,
) -> float:
    """Bounded recency boost in [1.0, 1.0+weight]: newest≈1+weight, old→1.0.

    Disabled (returns 1.0) when weight<=0, the half-life is non-positive, or a
    timestamp is unavailable.
    """
    if weight <= 0 or halflife_days <= 0 or doc_epoch is None or ref_epoch is None:
        return 1.0
    age_days = max(0.0, (ref_epoch - doc_epoch) / 86400.0)
    decay = 0.5 ** (age_days / halflife_days)
    return 1.0 + weight * decay


def _rerank(
    candidates: list[dict[str, Any]],
    terms: list[str],
    *,
    title_boost: float = _RERANK_TITLE_BOOST,
    len_pivot: int = _RERANK_LEN_PIVOT,
    len_floor: float = _RERANK_LEN_FLOOR,
    recency_weight: float = _RERANK_RECENCY_WEIGHT,
    recency_halflife_days: float = _RERANK_RECENCY_HALFLIFE_DAYS,
    ref_epoch: float | None = None,
) -> list[dict[str, Any]]:
    """Re-rank BM25 candidates (already in raw-score DESC order) by multiplying
    the min-max-normalized BM25 score with independent title / length / recency
    factors. Returns a NEW ordered list; input row dicts are not mutated. Stable:
    equal final scores keep the incoming BM25 order.

    Each candidate dict carries ``score`` (raw BM25), ``title``, ``text`` and a
    timestamp (``published_ts`` or ``fetch_ts``).
    """
    if len(candidates) <= 1:
        return list(candidates)

    scores = [float(c.get("score") or 0.0) for c in candidates]
    max_score = max(scores) if scores else 0.0

    # Recency is off by default; only parse timestamps when it is actually
    # enabled — otherwise we'd _to_epoch every candidate on the search hot path
    # only for _recency_factor to discard it (it short-circuits to 1.0).
    recency_on = recency_weight > 0
    if recency_on and ref_epoch is None:
        epochs = [
            e
            for c in candidates
            for e in (_to_epoch(c.get("published_ts") or c.get("fetch_ts")),)
            if e is not None
        ]
        ref_epoch = max(epochs) if epochs else None

    def final_score(c: dict[str, Any], raw: float) -> float:
        norm = (raw / max_score) if max_score > 0 else 0.0
        title_f = 1.0 + title_boost * _title_hit_frac(c.get("title") or "", terms)
        len_f = _length_factor(len(c.get("text") or ""), pivot=len_pivot, floor=len_floor)
        doc_epoch = _to_epoch(c.get("published_ts") or c.get("fetch_ts")) if recency_on else None
        rec_f = _recency_factor(
            doc_epoch,
            ref_epoch,
            halflife_days=recency_halflife_days,
            weight=recency_weight,
        )
        return norm * title_f * len_f * rec_f

    finals = [final_score(c, raw) for c, raw in zip(candidates, scores, strict=True)]
    # Sort by descending final score; ties keep original BM25 order (lower index
    # first), then capture_id for full determinism across equal-BM25 dups.
    order = sorted(
        range(len(candidates)),
        key=lambda i: (
            -finals[i],
            i,
            str(candidates[i].get("capture_id") or ""),
        ),
    )
    return [candidates[i] for i in order]


def _snippet_for(text: str, title: str, terms: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """Find a snippet of ~200 chars centered on the first match; return
    (snippet, hits) where hits is a list of (start, end) inside the snippet
    for each matched query token, lowercased-case-insensitive.
    """
    import re

    if not text:
        text = title or ""
    if not text:
        return "", []
    if not terms:
        return text[:200].strip(), []

    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)
    m = pattern.search(text)
    if not m:
        # No exact word boundary match — use substring fallback.
        lower = text.lower()
        first_pos = min((p for p in (lower.find(t) for t in terms) if p >= 0), default=-1)
        if first_pos < 0:
            return text[:200].strip(), []
        start = max(0, first_pos - 80)
        end = min(len(text), first_pos + 140)
    else:
        start = max(0, m.start() - 80)
        end = min(len(text), m.end() + 140)

    # Expand to word boundaries for cleaner edges.
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    while end < len(text) and text[end].isalnum():
        end += 1
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "… " + snippet
    if end < len(text):
        snippet = snippet + " …"

    # Compute hit positions inside snippet.
    hits: list[tuple[int, int]] = []
    for mm in pattern.finditer(snippet):
        hits.append((mm.start(), mm.end()))
    return snippet, hits


def count_related_captures(conn: duckdb.DuckDBPyConnection, capture_id: str) -> int:
    """Count sibling captures in the same dup_group (excludes *capture_id*)."""
    row = conn.execute(
        "SELECT doc_id, parent_doc_or_dup_group FROM captures WHERE capture_id = ? LIMIT 1",
        [capture_id],
    ).fetchone()
    if not row:
        return 0
    doc_id, dup_group = row
    group = dup_group or doc_id
    cnt = conn.execute(
        """
        SELECT COUNT(*)
        FROM captures
        WHERE (parent_doc_or_dup_group = ? OR doc_id = ?)
          AND capture_id <> ?
        """,
        [group, group, capture_id],
    ).fetchone()
    return int(cnt[0]) if cnt else 0


def find_related_captures(
    conn: duckdb.DuckDBPyConnection, capture_id: str, *, limit: int = 12
) -> list[dict[str, Any]]:
    """Return sibling captures in the same dup_group as ``capture_id``.

    Order: most recent first; the given capture is excluded.
    """
    row = conn.execute(
        "SELECT doc_id, parent_doc_or_dup_group FROM captures WHERE capture_id = ? LIMIT 1",
        [capture_id],
    ).fetchone()
    if not row:
        return []
    doc_id, dup_group = row
    group = dup_group or doc_id
    cur = conn.execute(
        """
        SELECT
          capture_id, doc_id, source_type, source_name, domain, url,
          fetch_ts, title, length(text) AS text_len
        FROM captures
        WHERE (parent_doc_or_dup_group = ? OR doc_id = ?)
          AND capture_id <> ?
        ORDER BY fetch_ts DESC
        LIMIT ?
        """,
        [group, group, capture_id, limit],
    )
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
