# Awareness Cycle 1 — Plan 4: Schema & Storage Correctness (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close three concrete correctness bugs at the storage edges: a 64-bit simhash that overflows a 32-bit column on Postgres, a schema migration that silently leaves dedup writes broken, and a Google Drive upload that crashes on compressed chunks and mislabels content type.

**Architecture:** Small, self-contained fixes in `storage/state.py` (column type + a loud post-migration schema check) and `storage/gdrive.py` (binary-safe multipart upload).

**Tech Stack:** Python 3.13, SQLAlchemy 2.x, httpx, pytest.

**Standard test command:** `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`
**Baseline at plan start:** 221 passing after Plan 3.

**Scope source:** spec workstream **G** (bug-level subset). Audit: `docs/superpowers/audit/2026-06-08-awareness-audit.json`.
**Deferred to Plan 4b (noted):** `tail_recrawl`/`warc_repair` honoring `text_min_chars`/`text_max_chars` + charset decode; one-command `quickstart`; zero-task backfill warning; search empty-state "why"; source-aware `text_min_chars`.

---

### Task 1: `near_dup_hash` is a 64-bit value — declare it `BigInteger`

**Why:** `DedupNearRow.near_dup_hash` is declared `Integer` (32-bit on Postgres) but stores a 64-bit signed simhash, overflowing on Postgres and reading back NULL on DuckDB's signed BIGINT path. Declare it `BigInteger`. (Audit: `bug:near-dup-hash-int32-overflow-on-postgres`.)

**Files:**
- Modify: `src/awareness/storage/state.py` (sqlalchemy import; `DedupNearRow.near_dup_hash` ~line 125)
- Test: `tests/unit/test_dedup_near_column.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_dedup_near_column.py`:

```python
from __future__ import annotations

from sqlalchemy import BigInteger

from awareness.storage.state import DedupNearRow


def test_near_dup_hash_is_bigint() -> None:
    col = DedupNearRow.__table__.c.near_dup_hash
    assert isinstance(col.type, BigInteger), (
        "near_dup_hash stores a 64-bit simhash; a 32-bit Integer overflows on Postgres"
    )
```

- [ ] **Step 2: Run, confirm FAIL** (it's `Integer`, not `BigInteger`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_near_column.py -q`

- [ ] **Step 3: Implement.** In `src/awareness/storage/state.py`:
  - Add `BigInteger` to the `from sqlalchemy import (...)` block (alphabetical position, before `DateTime`):
    ```python
    from sqlalchemy import (
        BigInteger,
        DateTime,
        Integer,
        ...
    )
    ```
  - Change the `near_dup_hash` column (~line 125) from:
    ```python
        near_dup_hash: Mapped[int | None] = mapped_column(Integer, nullable=True)  # legacy 64-bit signed
    ```
    to:
    ```python
        near_dup_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 64-bit signed simhash
    ```

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_dedup_near_column.py -q`
- [ ] **Step 5: Full-suite gate.**
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/storage/state.py tests/unit/test_dedup_near_column.py
git commit -m "fix(state): declare near_dup_hash BigInteger (64-bit simhash overflowed Integer)"
```

---

### Task 2: Fail loudly if the dedup schema migration left `sig_hex` missing

**Why:** `init()` runs `ALTER TABLE dedup_near ADD COLUMN sig_hex` inside a broad `try/except` that only logs a warning. If the ALTER fails (locked/partial/read-only DB), the column is absent but the ORM still writes/reads it, so every dedup write fails later behind a single easily-missed warning. Re-inspect after the migration and raise a clear error if `sig_hex` is still missing. (Audit: `bug:swallowed-sig-hex-migration-leaves-writes-broken`.)

**Files:**
- Modify: `src/awareness/storage/state.py` (`init()` migration block ~lines 200-213)
- Test: `tests/unit/test_state_migration_check.py` (create)

- [ ] **Step 1: Read** `init()` (~lines 194-213) to confirm the current migration block and the `next_attempt_at` migration added in Plan 1 (both live in the same try/except).

- [ ] **Step 2: Write the failing test** — create `tests/unit/test_state_migration_check.py`:

```python
from __future__ import annotations

import pytest

from awareness.storage.state import _verify_dedup_schema


class _FakeInspector:
    def __init__(self, columns: list[str]) -> None:
        self._columns = columns

    def get_columns(self, table: str):
        return [{"name": c} for c in self._columns]


def test_missing_sig_hex_raises() -> None:
    insp = _FakeInspector(["id", "doc_id", "seg", "seg_value"])  # no sig_hex
    with pytest.raises(RuntimeError):
        _verify_dedup_schema(insp)


def test_present_sig_hex_passes() -> None:
    insp = _FakeInspector(["id", "doc_id", "sig_hex", "seg", "seg_value"])
    _verify_dedup_schema(insp)  # must not raise


def test_no_table_yet_passes() -> None:
    # A brand-new DB before create_all reports no columns — must not raise.
    _verify_dedup_schema(_FakeInspector([]))
```

- [ ] **Step 3: Run, confirm FAIL** (`ImportError: cannot import name '_verify_dedup_schema'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_migration_check.py -q`

- [ ] **Step 4: Implement.** In `src/awareness/storage/state.py`:
  - Add a module-level function near the other helpers (e.g. just after `_utcnow`):
    ```python
    def _verify_dedup_schema(inspector: Any) -> None:
        """Raise if the dedup_near table exists but lacks the sig_hex column.

        The migration in init() adds sig_hex to legacy tables; if that ALTER
        silently failed (locked/partial/read-only DB), surface it loudly here
        instead of deferring to a confusing 'no such column: sig_hex' on every
        later dedup write.
        """
        cols = [c["name"] for c in inspector.get_columns("dedup_near")]
        if cols and "sig_hex" not in cols:
            raise RuntimeError(
                "dedup_near.sig_hex is missing after migration — the DB may be "
                "read-only, locked, or partially migrated; near-dup indexing "
                "would fail. Fix or recreate the state DB."
            )
    ```
    (`Any` is already imported at the top of the file.)
  - In `init()`, AFTER the migration `try/except` block and BEFORE `self._initialized = True`, add:
    ```python
            from sqlalchemy import inspect as _sa_inspect

            _verify_dedup_schema(_sa_inspect(self._engine))
    ```
    (Read the exact current end of the migration block; the `inspect` import already appears inside the try — re-importing under an alias here keeps the call outside the swallowing try so its RuntimeError propagates.)

- [ ] **Step 5: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_state_migration_check.py -q`
- [ ] **Step 6: Full-suite gate** (a normal fresh DB has sig_hex via create_all, so init() must NOT raise — verify the broad suite still constructs StateDB fine).
- [ ] **Step 7: Commit:**
```bash
git add src/awareness/storage/state.py tests/unit/test_state_migration_check.py
git commit -m "fix(state): raise loudly if dedup_near.sig_hex missing after migration"
```

---

### Task 3: Binary-safe Google Drive upload (read bytes; correct content type)

**Why:** `upload_file` does `file_path.read_text(encoding="utf-8")` and embeds the content in a string multipart body with `Content-Type: application/json` — so it crashes on `.jsonl.gz` (binary) and mislabels JSONL. Read bytes, build the multipart body as bytes, and set a correct content type. (Audit: `bug:gdrive-upload-readtext-fails-on-gzip-and-sends-invalid-json`.)

**Files:**
- Modify: `src/awareness/storage/gdrive.py` (`upload_file` ~lines 146-178)
- Test: `tests/unit/test_gdrive_multipart.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_gdrive_multipart.py`:

```python
from __future__ import annotations

from awareness.storage.gdrive import _build_multipart_body, _file_mime


def test_multipart_body_preserves_raw_bytes_and_mime() -> None:
    raw = b"\x1f\x8b\x08\x00rawgzipbytes"  # gzip magic + payload
    body = _build_multipart_body(
        {"name": "c.jsonl.gz", "parents": ["folder1"]}, raw, "application/gzip", "BOUND"
    )
    assert isinstance(body, bytes)
    assert raw in body  # binary content preserved verbatim
    assert b"Content-Type: application/gzip" in body
    assert b'"name": "c.jsonl.gz"' in body
    assert body.startswith(b"--BOUND\r\n")
    assert body.rstrip().endswith(b"--BOUND--")


def test_file_mime_by_extension() -> None:
    from pathlib import Path
    assert _file_mime(Path("c.jsonl.gz")) == "application/gzip"
    assert _file_mime(Path("c.jsonl")) == "application/x-ndjson"
```

- [ ] **Step 2: Run, confirm FAIL** (`ImportError: cannot import name '_build_multipart_body'`):
`PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_gdrive_multipart.py -q`

- [ ] **Step 3: Implement** in `src/awareness/storage/gdrive.py`:

(a) Add two module-level helpers (e.g. just above `upload_file`):
```python
def _file_mime(file_path: Path) -> str:
    """Content type for an uploaded corpus chunk."""
    return "application/gzip" if file_path.suffix == ".gz" else "application/x-ndjson"


def _build_multipart_body(
    metadata: dict[str, Any], file_bytes: bytes, file_mime: str, boundary: str
) -> bytes:
    """Assemble a multipart/related body as bytes so binary (gzip) chunks
    upload intact and carry the correct content type."""
    return b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            json.dumps(metadata).encode("utf-8"),
            f"\r\n--{boundary}\r\n".encode(),
            f"Content-Type: {file_mime}\r\n\r\n".encode(),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
```

(b) In `upload_file`, replace the read + body-assembly section. Change the read (~line 147-151):
```python
    try:
        file_content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.exception("gdrive_upload_read_failed", path=str(file_path), error=str(e))
        return None
```
to:
```python
    try:
        file_bytes = file_path.read_bytes()
    except Exception as e:
        logger.exception("gdrive_upload_read_failed", path=str(file_path), error=str(e))
        return None
```
And replace the multipart-body string assembly (the `multipart_body = (...)` block ~lines 166-174) and the post call's `content=multipart_body` with the bytes builder:
```python
    multipart_body = _build_multipart_body(metadata, file_bytes, _file_mime(file_path), boundary)
```
Leave the `headers`, `metadata`, `upload_url`, and `httpx.post(..., content=multipart_body, ...)` lines intact (the post already passes `content=multipart_body`, which now carries bytes).

- [ ] **Step 4: Confirm PASS:** `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_gdrive_multipart.py -q`
- [ ] **Step 5: Full-suite gate** (existing gdrive tests in `tests/unit/test_gdrive.py` must still pass; if one asserted the old string body, READ it and update only the bug-encoding assertion, noting under Deviations).
- [ ] **Step 6: Commit:**
```bash
git add src/awareness/storage/gdrive.py tests/unit/test_gdrive_multipart.py
git commit -m "fix(gdrive): binary-safe multipart upload (read bytes, correct content type)"
```

---

## Plan-level self-review checklist

- [ ] Full suite green after all tasks.
- [ ] `ruff check` introduces no NEW errors in `state.py` / `gdrive.py`.
- [ ] Deferred to Plan 4b recorded: tail_recrawl/warc_repair min/max chars + charset; quickstart; zero-task warning; search empty-state; source-aware text_min_chars.

## Spec coverage map (workstream G subset)

| Item | Task |
|---|---|
| near_dup_hash → BigInteger | 1 |
| sig_hex migration loud | 2 |
| gdrive read_bytes + content-type | 3 |
| tail_recrawl/warc_repair min/max+charset, quickstart, empty-state, zero-task warn | Deferred → Plan 4b |
