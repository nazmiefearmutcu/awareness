"""LLM-ready dataset export over the DuckDB captures lake.

Each exported row is shaped for instruction-tuning / fine-tuning ingestion::

    {
        "instruction": null,
        "input": null,
        "output": "<title>\\n\\n<text>",
        "metadata": {
            "capture_id": ...,
            "domain": ...,
            "url": ...,
            "observed_ts": ...,
            "language": ...,
        }
    }

Guarantees:

* **Bounded memory** — rows are streamed from DuckDB with ``fetchmany``
  batches and written incrementally; the full dataset is never materialized
  in Python (except Parquet, which buffers the bounded row set for Arrow).
* **Atomic writes** — the export is written to a ``*.tmp`` sibling and
  ``os.replace``-renamed into place, so a crash never leaves a truncated file
  under the final name.
* **Hard limit** — ``limit`` is clamped to ``[1, HARD_MAX_LIMIT]``
  (default 1000, max 100000).
* **Deterministic order** — rows are ordered by capture timestamp ascending
  with ``capture_id`` as a stable tie-breaker.

Timestamps: windows and ordering use ``COALESCE(observed_ts, fetch_ts)`` —
the semantic observation time, with a fallback for corpora that predate
``observed_ts`` being populated.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from awareness.obs.logging import get_logger
from awareness.storage.duckdb_index import DuckDbIndex
from awareness.util.timeutil import to_utc, utcnow

logger = get_logger("consume.llm_export")

DEFAULT_EXPORT_LIMIT = 1000
HARD_MAX_LIMIT = 100_000
EXPORT_FORMATS: tuple[str, ...] = ("jsonl", "parquet")

# Semantic capture timestamp used for window filters + ordering. Falls back to
# the fetch time when a capture predates observed_ts being populated.
_CAPTURE_TS_SQL = "COALESCE(observed_ts, fetch_ts)"

# Fold key for dedupe mode: one row per parent_doc_or_dup_group, falling back
# to content_hash then capture_id (mirrors the API /captures?unique=group key).
_FOLD_KEY_SQL = (
    "COALESCE("
    "NULLIF(TRIM(CAST(parent_doc_or_dup_group AS VARCHAR)), ''), "
    "NULLIF(TRIM(CAST(content_hash AS VARCHAR)), ''), "
    "capture_id)"
)

_EXPORT_SELECT = f"capture_id, domain, url, title, text, language, {_CAPTURE_TS_SQL} AS observed_ts"

_STREAM_BATCH = 512

# SQL templates — interpolate ONLY code-owned constants; every user-supplied
# value flows through bound $params (see the S608 noqas below).
_EXPORT_SQL_PLAIN = (
    f"SELECT {_EXPORT_SELECT} "  # noqa: S608 -- code-owned constants only
    "FROM captures "
    "ORDER BY observed_ts ASC, capture_id ASC"
)
_EXPORT_SQL_FOLDED = (
    "SELECT * EXCLUDE (_fold_key) FROM ( "  # noqa: S608 -- code-owned constants only
    f"SELECT DISTINCT ON ({_FOLD_KEY_SQL}) {_EXPORT_SELECT}, {_FOLD_KEY_SQL} AS _fold_key "
    "FROM captures "
    f"ORDER BY {_FOLD_KEY_SQL}, observed_ts ASC, capture_id ASC "
    ") _folded "
    "ORDER BY observed_ts ASC, capture_id ASC"
)


class ExportResult(BaseModel):
    """Outcome of an :func:`export_llm_dataset` call."""

    count: int = 0
    path: str = ""
    files: list[str] = Field(default_factory=list)
    format: str = "jsonl"
    limit: int = DEFAULT_EXPORT_LIMIT
    dedupe: bool = True
    start: datetime | None = None
    end: datetime | None = None


def _clamp_limit(limit: int) -> int:
    """Clamp *limit* to ``[1, HARD_MAX_LIMIT]`` (hard overload guard)."""
    return max(1, min(int(limit), HARD_MAX_LIMIT))


def _window_params(start: Any, end: Any) -> tuple[list[str], dict[str, Any]]:
    """Build WHERE clauses + bind params for a capture-timestamp window."""
    where: list[str] = []
    params: dict[str, Any] = {}
    start_dt = to_utc(start)
    end_dt = to_utc(end)
    if start_dt is not None:
        where.append(f"{_CAPTURE_TS_SQL} >= $start")
        params["start"] = start_dt
    if end_dt is not None:
        where.append(f"{_CAPTURE_TS_SQL} <= $end")
        params["end"] = end_dt
    return where, params


def _domain_params(domains: list[str] | None) -> tuple[list[str], dict[str, Any]]:
    """Build WHERE clauses + bind params for a domain list filter.

    Case-insensitive: stored domains are lower eTLD+1, but callers may pass
    mixed case. All values are bound parameters (no interpolation).
    """
    clean = [str(d).strip().lower() for d in (domains or []) if d is not None and str(d).strip()]
    if not clean:
        return [], {}
    where = [f"lower(domain) IN ({', '.join(f'$d{i}' for i in range(len(clean)))})"]
    params = {f"d{i}": d for i, d in enumerate(clean)}
    return where, params


def _export_sql(*, dedupe: bool) -> str:
    """SELECT for an export (no WHERE / LIMIT yet).

    In dedupe mode the fold keeps the FIRST capture per group ordered by
    capture timestamp ascending (earliest observation wins). The outer query
    then re-orders the folded set deterministically.
    """
    return _EXPORT_SQL_FOLDED if dedupe else _EXPORT_SQL_PLAIN


def _with_where(sql: str, where_sql: str) -> str:
    """Insert *where_sql* right after ``FROM captures`` in the SELECT.

    Both the plain and the dedupe-fold template read from ``captures`` exactly
    once (inside the subquery for the fold), so a single marker works for both.
    """
    if not where_sql:
        return sql
    marker = "FROM captures"
    idx = sql.index(marker) + len(marker)
    return f"{sql[:idx]}{where_sql}{sql[idx:]}"


def _stream_rows(
    index: DuckDbIndex,
    sql: str,
    params: dict[str, Any],
    *,
    batch: int = _STREAM_BATCH,
) -> Iterator[tuple[tuple[Any, ...], list[str]]]:
    """Yield ``(row_tuple, columns)`` in bounded batches from the live view.

    Uses the process-wide connection directly with ``fetchmany`` so a large
    export never materializes more than *batch* rows in memory. The view
    refresh is triggered by the caller via ``health_snapshot()``; we re-enter
    the index lock so the stream is serialized against concurrent writers the
    same way ``execute``/``related`` are.
    """
    conn = index.connect()
    with index._lock:  # guard raw connection access like related() does
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for row in rows:
                yield row, cols


def _iso_or(value: Any) -> Any:
    """ISO-8601 string for datetimes, passthrough for everything else."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _iter_records(rows: Iterator[tuple[tuple[Any, ...], list[str]]]) -> Iterator[dict[str, Any]]:
    """Yield LLM-ready row dicts from the streamed database rows."""
    for row, cols in rows:
        rec = dict(zip(cols, row, strict=True))
        metadata = {
            "capture_id": rec.get("capture_id"),
            "domain": rec.get("domain"),
            "url": rec.get("url"),
            "observed_ts": _iso_or(rec.get("observed_ts")),
            "language": rec.get("language"),
        }
        title = str(rec["title"]) if rec.get("title") is not None else ""
        text = str(rec["text"]) if rec.get("text") is not None else ""
        output = f"{title}\n\n{text}".strip("\n") if (title or text) else ""
        yield {
            "instruction": None,
            "input": None,
            "output": output,
            "metadata": metadata,
        }


def _write_jsonl_atomic(out_dir: Path, name: str, records: Iterator[dict[str, Any]]) -> int:
    """Stream records to a temp JSONL file, then atomically rename it.

    Returns the number of records written. The temp file is removed on error.
    """
    tmp_path = out_dir / f"{name}.tmp"
    count = 0
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        os.replace(tmp_path, out_dir / name)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    return count


def _write_parquet_atomic(out_dir: Path, name: str, records: Iterator[dict[str, Any]]) -> int:
    """Materialize records into a Parquet table and atomically rename it.

    ``pyarrow`` is imported lazily so the rest of the module works without it.
    """
    try:
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError("parquet export requires pyarrow; install it or use format='jsonl'") from exc

    meta_type = pa.struct(
        [
            ("capture_id", pa.string()),
            ("domain", pa.string()),
            ("url", pa.string()),
            ("observed_ts", pa.string()),
            ("language", pa.string()),
        ]
    )
    outputs: list[str | None] = []
    metadatas: list[dict[str, Any] | None] = []
    count = 0
    for record in records:
        outputs.append(record["output"] or None)
        meta = record["metadata"]
        metadatas.append(
            {
                "capture_id": meta.get("capture_id"),
                "domain": meta.get("domain"),
                "url": meta.get("url"),
                "observed_ts": meta.get("observed_ts"),
                "language": meta.get("language"),
            }
        )
        count += 1

    table = pa.table(
        {
            "instruction": pa.array([None] * count, type=pa.null()),
            "input": pa.array([None] * count, type=pa.null()),
            "output": pa.array(outputs, type=pa.string()),
            "metadata": pa.array(metadatas, type=meta_type),
        }
    )
    tmp_path = out_dir / f"{name}.tmp"
    try:
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, out_dir / name)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    return count


def _export_filename(format: str) -> str:
    """Collision-safe, sortable export file name (timestamp + uuid suffix)."""
    stamp = utcnow().strftime("%Y%m%d_%H%M%S")
    return f"llm_export_{stamp}_{uuid.uuid4().hex[:8]}.{format}"


def export_llm_dataset(  # noqa: PLR0917 -- spec'd signature; all-but-two args are optional
    index: DuckDbIndex,
    out_dir: Path,
    format: str = "jsonl",
    limit: int = DEFAULT_EXPORT_LIMIT,
    start: Any = None,
    end: Any = None,
    domains: list[str] | None = None,
    dedupe: bool = True,
) -> ExportResult:
    """Export captures to an LLM-ready dataset file in *out_dir*.

    Args:
        index: The :class:`DuckDbIndex` to read captures from.
        out_dir: Destination directory (created if missing).
        format: ``"jsonl"`` (default) or ``"parquet"`` (requires pyarrow).
        limit: Max rows to write; clamped to ``[1, HARD_MAX_LIMIT]``.
        start/end: Optional capture-timestamp window (observed_ts, falling
            back to fetch_ts); anything convertible via ``to_utc``.
        domains: Optional domain allow-list (case-insensitive).
        dedupe: Fold to one row per ``parent_doc_or_dup_group`` (earliest
            capture wins) when True.

    The write is atomic (temp file + rename). An empty corpus yields a valid
    file with zero records.
    """
    fmt = str(format).strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format {format!r}; expected one of {EXPORT_FORMATS}")
    bounded = _clamp_limit(limit)

    where_sql = ""
    params: dict[str, Any] = {}
    window_where, window_params = _window_params(start, end)
    domain_where, domain_params = _domain_params(domains)
    params.update(window_params)
    params.update(domain_params)
    if window_where or domain_where:
        where_sql = f" WHERE {' AND '.join(window_where + domain_where)}"
    params["limit"] = bounded

    # health_snapshot() forces a view refresh against the current corpus, so
    # the streaming SELECT below always reads fresh data.
    snap = index.health_snapshot()
    if not bool(snap.get("ready")):
        raise RuntimeError(f"duckdb index not ready: {snap.get('error')}")

    sql = _with_where(_export_sql(dedupe=dedupe), where_sql) + "\n        LIMIT $limit"

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = _export_filename(fmt)
    records = _iter_records(_stream_rows(index, sql, params))

    if fmt == "jsonl":
        count = _write_jsonl_atomic(out_dir, name, records)
    else:
        count = _write_parquet_atomic(out_dir, name, records)

    final_path = out_dir / name
    logger.info(
        "llm_export_written",
        format=fmt,
        rows=count,
        limit=bounded,
        dedupe=bool(dedupe),
        path=str(final_path),
    )
    return ExportResult(
        count=count,
        path=str(final_path),
        files=[str(final_path)],
        format=fmt,
        limit=bounded,
        dedupe=bool(dedupe),
        start=to_utc(start),
        end=to_utc(end),
    )


def sample_corpus(
    index: DuckDbIndex,
    n: int = 5,
    start: Any = None,
    end: Any = None,
) -> list[dict[str, Any]]:
    """Return a bounded random sample of captures for human inspection.

    ``ORDER BY random() LIMIT n`` over the ``captures`` view — *n* is clamped
    to ``[1, 1000]`` so memory stays bounded. Each item carries the full
    capture row plus a serialized ``observed_ts``.
    """
    bounded = max(1, min(int(n), 1000))
    where, params = _window_params(start, end)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    params["n"] = bounded
    sql = (
        f"SELECT capture_id, doc_id, domain, url, title, text, language, {_CAPTURE_TS_SQL} AS observed_ts "  # noqa: S608 -- code-owned constants only
        "FROM captures "
        f"{where_sql} "
        "ORDER BY random() "
        "LIMIT $n"
    )
    rows = index.execute(sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        rec["observed_ts"] = _iso_or(rec.get("observed_ts"))
        out.append(rec)
    return out
