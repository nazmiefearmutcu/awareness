"""Short-TTL disk (+ memory) cache for job-search HTTP payloads.

Used by LinkedIn guest search / detail enrichment to cut rate risk and latency.
Keys are sha256 of kind+params; values are JSON-serializable (usually HTML text).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

# Search result pages go stale quickly; job details are more stable.
SEARCH_TTL_SEC = 20 * 60  # 20 minutes
DETAIL_TTL_SEC = 2 * 60 * 60  # 2 hours


def _stable_key(kind: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]


class JobSearchCache:
    """Disk cache under ``{data_dir}/jobsearch_cache/`` with a small memory layer."""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "jobsearch_cache"
        self.root.mkdir(parents=True, exist_ok=True)
        # key -> (expires_at, value)
        self._mem: dict[str, tuple[float, Any]] = {}

    def get(self, kind: str, params: dict[str, Any]) -> Any | None:
        key = _stable_key(kind, params)
        now = time.time()

        hit = self._mem.get(key)
        if hit is not None:
            exp, val = hit
            if now < exp:
                return val
            self._mem.pop(key, None)

        path = self.root / f"{key}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        exp = float(data.get("expires_at") or 0)
        if now >= exp:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        val = data.get("value")
        self._mem[key] = (exp, val)
        return val

    def set(self, kind: str, params: dict[str, Any], value: Any, ttl_sec: int) -> None:
        key = _stable_key(kind, params)
        expires_at = time.time() + max(1, int(ttl_sec))
        self._mem[key] = (expires_at, value)
        path = self.root / f"{key}.json"
        payload = {
            "kind": kind,
            "params": params,
            "expires_at": expires_at,
            "value": value,
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # Memory still holds the entry for this process.
            pass

    def clear_memory(self) -> None:
        self._mem.clear()
