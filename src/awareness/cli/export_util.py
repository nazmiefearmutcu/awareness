"""Helpers for ``awareness export`` — query captures and write JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from awareness.storage.duckdb_index import DuckDbIndex

# Match API /captures?unique= fold keys (newest fetch_ts per key).
_UNIQUE_FOLD_KEY_SQL: dict[str, str] = {
    "content": (
        "COALESCE(NULLIF(TRIM(CAST(content_hash AS VARCHAR)), ''), capture_id)"
    ),
    "group": (
        "COALESCE("
        "NULLIF(TRIM(CAST(parent_doc_or_dup_group AS VARCHAR)), ''), "
        "NULLIF(TRIM(CAST(content_hash AS VARCHAR)), ''), "
        "capture_id)"
    ),
}

_EXPORT_SELECT = (
    "doc_id, capture_id, source_type, source_name, canonical_url, "
    "fetch_ts, domain, title, text, language, "
    "content_hash, parent_doc_or_dup_group"
)


def export_fold_key_sql(unique: str) -> str | None:
    """Return SQL fold-key expression for unique mode, or None for no fold."""
    if unique in (None, "", "none"):
        return None
    expr = _UNIQUE_FOLD_KEY_SQL.get(unique)
    if expr is None:
        raise ValueError(f"invalid unique mode: {unique!r}")
    return expr


def query_export_captures(
    idx: DuckDbIndex,
    *,
    limit: int = 1000,
    unique: str = "none",
    domain: str = "",
    source: str = "",
) -> list[dict[str, Any]]:
    """Fetch captures for export via :meth:`DuckDbIndex.execute`.

    ``unique``:
      * ``none``    — all rows (default)
      * ``content`` — one row per content_hash (newest fetch_ts)
      * ``group``   — one row per parent_doc_or_dup_group / content_hash / capture_id

    ``limit`` 0 means no SQL LIMIT (export all matching rows).
    """
    where: list[str] = []
    params: dict[str, Any] = {}
    if domain:
        where.append("domain = $dom")
        params["dom"] = domain
    if source:
        where.append("source_type = $src")
        params["src"] = source
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    fold_key = export_fold_key_sql(unique)
    limit_sql = f" LIMIT {int(limit)}" if int(limit) > 0 else ""

    if fold_key is None:
        sql = f"""
            SELECT {_EXPORT_SELECT}
            FROM captures
            {where_sql}
            ORDER BY fetch_ts DESC
            {limit_sql}
        """
        return idx.execute(sql, params)

    sql = f"""
        SELECT * EXCLUDE (_fold_key) FROM (
          SELECT DISTINCT ON ({fold_key})
            {_EXPORT_SELECT},
            {fold_key} AS _fold_key
          FROM captures
          {where_sql}
          ORDER BY {fold_key}, fetch_ts DESC
        ) _folded
        ORDER BY fetch_ts DESC
        {limit_sql}
    """
    return idx.execute(sql, params)


def write_export_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    """Write rows as JSONL; return number of lines written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
    return len(rows)
