"""Settings redaction + env-over-YAML precedence for the config API."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from awareness.config import get_settings, reset_settings
from awareness.config.persist import _redact_url_userinfo, schema_payload
from awareness.config.schema import value_source


def _yaml(tmp_path: Path, **values) -> Path:
    cfg = tmp_path / "configs" / "awareness.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(yaml.safe_dump(values), encoding="utf-8")
    return cfg


# ── C-06: env beats YAML ─────────────────────────────────────────────────────
def test_env_beats_yaml(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    monkeypatch.delenv("AW_DATA_DIR", raising=False)
    _yaml(tmp_path, data_dir=str(tmp_path / "yaml-data"), log_json=True)
    reset_settings()
    monkeypatch.setenv("AW_DATA_DIR", str(tmp_path / "env-data"))
    reset_settings()
    s = get_settings()
    assert s.data_dir == tmp_path / "env-data"  # env wins over YAML
    assert s.log_json is True  # no env for this key → YAML applies
    assert (
        value_source("data_dir", {"data_dir": str(tmp_path / "yaml-data")}, os.environ) == "env"
    )
    reset_settings()
    monkeypatch.delenv("AW_DATA_DIR")
    reset_settings()
    assert get_settings().data_dir == tmp_path / "yaml-data"


def test_env_beats_yaml_in_schema_payload(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    _yaml(tmp_path, tail_poll_seconds=5.0)
    monkeypatch.setenv("AW_TAIL_POLL_SECONDS", "77")
    reset_settings()
    fields = {
        f["key"]: f
        for section in schema_payload()["sections"]
        for f in section["fields"]
    }
    assert fields["tail_poll_seconds"]["value"] == 77.0
    assert fields["tail_poll_seconds"]["source"] == "env"
    assert fields["tail_poll_seconds"]["env_locked"] is True
    reset_settings()


# ── H-21: secret masking in schema payload ───────────────────────────────────
def test_schema_masks_redis_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AW_CONFIG_FILE", raising=False)
    monkeypatch.setenv("AW_REDIS_URL", "redis://default:supersecret@redis.internal:6379/0")
    reset_settings()
    fields = {
        f["key"]: f
        for section in schema_payload()["sections"]
        for f in section["fields"]
    }
    redis = fields["redis_url"]
    assert redis["masked"] is True
    assert redis["value"] is None
    assert "supersecret" not in str(schema_payload())
    reset_settings()


def test_redact_url_userinfo_keeps_creds_out() -> None:
    assert _redact_url_userinfo("redis://user:pass@host:6379/0") == "redis://***:***@host:6379/0"
    assert _redact_url_userinfo("https://user:pass@example.com/a?b=1") == "https://***:***@example.com/a?b=1"
    assert _redact_url_userinfo("https://example.com/plain") == "https://example.com/plain"
    assert _redact_url_userinfo("/var/local/path") == "/var/local/path"
    assert _redact_url_userinfo(None) is None
