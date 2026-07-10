"""Read/write YAML config overrides used by CLI and Settings UI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from awareness.config import get_settings, reset_settings
from awareness.config.schema import (
    CONFIG_SCHEMA,
    coerce_and_validate,
    fields_by_section,
    get_field,
    normalize_key,
    value_source,
)
from awareness.config.settings import Settings, _project_root


def yaml_config_path() -> Path:
    path = os.environ.get("AW_CONFIG_FILE")
    if path:
        return Path(path)
    return _project_root() / "configs" / "awareness.yaml"


def read_yaml_data() -> dict[str, Any]:
    path = yaml_config_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_yaml_data(data: dict[str, Any]) -> None:
    path = yaml_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=True, allow_unicode=True)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def set_yaml_values(values: dict[str, Any]) -> None:
    data = read_yaml_data()
    data.update(values)
    write_yaml_data(data)
    reset_settings()


def unset_yaml_keys(keys: list[str]) -> list[str]:
    data = read_yaml_data()
    removed: list[str] = []
    for k in keys:
        nk = normalize_key(k)
        if nk in data:
            del data[nk]
            removed.append(nk)
    if removed:
        write_yaml_data(data)
        reset_settings()
    return removed


def _jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def schema_payload() -> dict[str, Any]:
    """Full settings form model for the UI."""
    settings = get_settings()
    yaml_data = read_yaml_data()
    env = os.environ
    sections: list[dict[str, Any]] = []
    for section, fields in fields_by_section().items():
        items = []
        for fld in fields:
            cur = getattr(settings, fld.key, None)
            items.append(
                {
                    "key": fld.key,
                    "section": fld.section,
                    "kind": fld.kind,
                    "description": fld.description,
                    "default": _jsonable(fld.default),
                    "value": _jsonable(cur),
                    "source": value_source(fld.key, yaml_data, env),
                    "env_var": fld.env_var,
                    "choices": list(fld.choices) if fld.choices else None,
                    "minimum": fld.minimum,
                    "maximum": fld.maximum,
                    "example": fld.example,
                    "is_destination": fld.is_destination,
                    "env_locked": value_source(fld.key, yaml_data, env) == "env",
                }
            )
        sections.append({"name": section, "fields": items})
    return {
        "config_path": str(yaml_config_path()),
        "sections": sections,
        "note": "Values saved to YAML. Env vars (AW_*) override YAML and cannot be changed here. Restart API for some knobs.",
    }


def apply_updates(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a partial update map. Returns applied + errors."""
    applied: dict[str, Any] = {}
    errors: dict[str, str] = {}
    yaml_data = read_yaml_data()
    env = os.environ

    for key, raw_val in (raw or {}).items():
        nk = normalize_key(str(key))
        if nk not in Settings.model_fields:
            errors[nk] = "unknown key"
            continue
        if value_source(nk, yaml_data, env) == "env":
            errors[nk] = f"locked by environment variable AW_{nk.upper()}"
            continue
        fld = get_field(nk)
        if fld is None:
            # Allow path-like str for unschematized? stick to schema only
            errors[nk] = "not in user-facing schema"
            continue
        # Empty string for optional → unset
        if raw_val is None or (isinstance(raw_val, str) and raw_val.strip() == "" and fld.default is None):
            if nk in yaml_data:
                del yaml_data[nk]
                applied[nk] = None
            continue
        typed, err = coerce_and_validate(fld, raw_val)
        if err:
            errors[nk] = err
            continue
        yaml_data[nk] = typed
        applied[nk] = typed

    if applied and not errors:
        write_yaml_data(yaml_data)
        reset_settings()
    elif applied and errors:
        # still apply good ones
        write_yaml_data(yaml_data)
        reset_settings()

    settings = get_settings()
    return {
        "ok": len(errors) == 0,
        "applied": {k: _jsonable(v) for k, v in applied.items()},
        "errors": errors,
        "values": {f.key: _jsonable(getattr(settings, f.key, None)) for f in CONFIG_SCHEMA},
    }


def tail_seeds_path() -> Path:
    s = get_settings()
    p = s.tail_seed_file
    if p is None:
        return _project_root() / "configs" / "tail_seeds.yaml"
    return Path(p)


def read_tail_seeds() -> dict[str, Any]:
    path = tail_seeds_path()
    if not path.exists():
        return {"feeds": [], "atom": [], "sitemaps": [], "path": str(path)}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        data = {}
    feeds = data.get("feeds") or []
    atom = data.get("atom") or []
    sitemaps = data.get("sitemaps") or []

    def urls(items: Any) -> list[str]:
        out: list[str] = []
        if not isinstance(items, list):
            return out
        for it in items:
            if isinstance(it, dict) and it.get("url"):
                out.append(str(it["url"]))
            elif isinstance(it, str) and it.strip():
                out.append(it.strip())
        return out

    return {
        "path": str(path),
        "feeds": urls(feeds),
        "atom": urls(atom),
        "sitemaps": urls(sitemaps),
    }


def write_tail_seeds(payload: dict[str, Any]) -> dict[str, Any]:
    path = tail_seeds_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    def to_entries(urls: Any) -> list[dict[str, str]]:
        if not urls:
            return []
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.replace(";", "\n").splitlines() if u.strip()]
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for u in urls:
            s = str(u).strip()
            if not s or s in seen:
                continue
            if not (s.startswith("http://") or s.startswith("https://")):
                continue
            seen.add(s)
            out.append({"url": s})
        return out

    data = {
        "feeds": to_entries(payload.get("feeds")),
        "atom": to_entries(payload.get("atom")),
        "sitemaps": to_entries(payload.get("sitemaps")),
    }
    # Preserve header comment by rewriting full file with a short header
    header = (
        "# Awareness tail seeds — edited via Settings UI\n"
        "# Each entry: { url: \"https://…\" }\n\n"
    )
    body = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)
    return read_tail_seeds()
