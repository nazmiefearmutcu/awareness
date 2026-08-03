"""Read/write YAML config overrides used by CLI and Settings UI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from awareness.config import get_settings, reset_settings
from awareness.config.schema import (
    CONFIG_SCHEMA,
    KIND_PATH,
    coerce_and_validate,
    fields_by_section,
    get_field,
    normalize_key,
    value_source,
)
from awareness.config.settings import Settings, _project_root
from awareness.util.urls import is_public_http_url

# Fields whose YAML value is a filesystem path (validated against the project
# root so an unauthenticated config write cannot point the app at arbitrary
# files). ``data_dir`` anchors under <root>/data; everything else path-kind
# anchors under <root>/configs.
_CONFIG_DIR = "configs"
_SECRET_FIELDS = frozenset({"redis_url", "state_db_url"})


def _is_public_seed_url(value: Any) -> bool:
    """http(s) URL that is public AND carries no userinfo (user:pass@).

    ``is_public_http_url`` checks the host, not the userinfo component; a seed
    like ``https://creds@example.com/`` must not be written or fetched.
    """
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.username or parts.password:
        return False
    return is_public_http_url(value)


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
    for key, raw_val in (values or {}).items():
        error = _validate_setting_value(str(key), raw_val)
        if error:
            raise ValueError(f"{normalize_key(str(key))}: {error}")
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


def _redact_url_userinfo(value: Any) -> Any:
    """Strip ``user:pass@`` from URL-shaped values before they leave the API."""
    if not isinstance(value, str) or "://" not in value or "@" not in value:
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not (parts.username or parts.password):
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    redacted = f"{parts.scheme}://***:***@{host}{parts.path or '/'}"
    if parts.query:
        redacted = f"{redacted}?{parts.query}"
    if parts.fragment:
        redacted = f"{redacted}#{parts.fragment}"
    return redacted


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
            secret = fld.key in _SECRET_FIELDS
            items.append(
                {
                    "key": fld.key,
                    "section": fld.section,
                    "kind": fld.kind,
                    "description": fld.description,
                    "default": _jsonable(fld.default),
                    "value": None if secret else _jsonable(_redact_url_userinfo(cur)),
                    "masked": secret,
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


def _validate_setting_value(key: str, raw_val: Any) -> str | None:
    """Return an error message for a path-kind value, or None if acceptable.

    Path confinement: relative values resolve under the project root; absolute
    values must resolve inside it; ``..`` segments are rejected outright.
    ``data_dir`` anchors under ``<root>/data``, other path fields (e.g.
    ``tail_seed_file``) under ``<root>/configs``. ``data_dir`` additionally
    must not point at an existing non-directory (e.g. ``/dev/null``).
    """
    fld = get_field(key)
    if fld is None or fld.kind != KIND_PATH:
        return None
    text = str(raw_val).strip()
    if text == "":
        return None
    root = _project_root()
    raw_path = Path(text)
    error: str | None = None
    if ".." in raw_path.parts:
        error = "must not contain '..' path segments"
    else:
        candidate = raw_path if raw_path.is_absolute() else root / raw_path
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        error = _path_within_root_error(resolved, root)
        if error is None:
            anchor = root / _CONFIG_DIR
            if fld.key == "data_dir":
                anchor = root / "data"
            if not resolved.is_relative_to(anchor):
                error = f"must resolve inside {anchor}"
            elif fld.key == "data_dir" and resolved.exists() and not resolved.is_dir():
                error = f"{resolved} exists and is not a directory"
    return error


def _path_within_root_error(resolved: Path, root: Path) -> str | None:
    """Return an error message when *resolved* escapes the project root."""
    if resolved.is_relative_to(root):
        return None
    return f"must resolve inside the project root ({root})"


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
        path_err = _validate_setting_value(nk, raw_val)
        if path_err:
            errors[nk] = path_err
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
        "applied": {k: (None if k in _SECRET_FIELDS else _jsonable(v)) for k, v in applied.items()},
        "errors": errors,
        "values": {
            f.key: (
                None
                if f.key in _SECRET_FIELDS
                else _jsonable(_redact_url_userinfo(getattr(settings, f.key, None)))
            )
            for f in CONFIG_SCHEMA
        },
    }


def tail_seeds_path() -> Path:
    s = get_settings()
    p = s.tail_seed_file
    if p is None:
        return _project_root() / _CONFIG_DIR / "tail_seeds.yaml"
    if p.is_absolute():
        return Path(p)
    return _project_root() / Path(p)


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
            if not _is_public_seed_url(s):
                raise ValueError(
                    f"seed URL rejected (must be public http(s), no userinfo, no private/internal host): {s}"
                )
            seen.add(s)
            out.append({"url": s})
        return out

    data = {
        "feeds": to_entries(payload.get("feeds")),
        "atom": to_entries(payload.get("atom")),
        "sitemaps": to_entries(payload.get("sitemaps")),
    }
    # Preserve header comment by rewriting full file with a short header
    header = '# Awareness tail seeds — edited via Settings UI\n# Each entry: { url: "https://…" }\n\n'
    body = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)
    return read_tail_seeds()
