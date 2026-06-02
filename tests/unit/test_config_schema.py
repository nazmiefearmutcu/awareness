"""Tests for the pure configuration schema registry (config/schema.py).

These pin the registry against the runtime Settings model and exercise every
coercion/validation branch — the backbone the `config` and `configure` CLI
commands are built on.
"""

from __future__ import annotations

import os

import pytest

from awareness.config import schema as sc
from awareness.config.settings import Settings


# ── registry integrity ───────────────────────────────────────────────────────
def test_every_schema_key_exists_on_settings() -> None:
    model_fields = set(Settings.model_fields)
    for fld in sc.CONFIG_SCHEMA:
        assert fld.key in model_fields, f"{fld.key} is not a real Settings field"


def test_no_duplicate_keys() -> None:
    keys = [f.key for f in sc.CONFIG_SCHEMA]
    assert len(keys) == len(set(keys))


def test_every_field_has_section_and_description() -> None:
    for fld in sc.CONFIG_SCHEMA:
        assert fld.section.strip()
        assert fld.description.strip()
        assert fld.section in sc.SECTION_ORDER, f"{fld.section} missing from SECTION_ORDER"


def test_defaults_match_settings() -> None:
    # A fresh Settings with no env/yaml overrides must agree with the schema
    # defaults for every non-path field (paths are derived in post-init).
    with pytest.MonkeyPatch.context() as mp:
        for k in list(os.environ):
            if k.startswith("AW_"):
                mp.delenv(k, raising=False)
        s = Settings()
    for fld in sc.CONFIG_SCHEMA:
        if fld.kind == sc.KIND_PATH or fld.key in {"data_dir", "iceberg_warehouse", "tail_seed_file"}:
            continue  # derived in model_post_init, not a static default
        assert getattr(s, fld.key) == fld.default, f"default drift on {fld.key}"


def test_destination_fields_are_the_three_toggles() -> None:
    keys = {f.key for f in sc.destination_fields()}
    assert keys == {"enable_jsonl_staging", "enable_iceberg", "enable_gdrive"}


def test_env_var_naming() -> None:
    assert sc.get_field("data_dir").env_var == "AW_DATA_DIR"
    assert sc.get_field("enable_gdrive").env_var == "AW_ENABLE_GDRIVE"


def test_fields_by_section_is_ordered() -> None:
    sections = list(sc.fields_by_section().keys())
    # Storage routing must come first (it is the destinations).
    assert sections[0] == "Storage routing"
    assert "Destination targets" in sections


# ── key normalisation + suggestions ──────────────────────────────────────────
def test_normalize_and_lookup_accepts_dashes() -> None:
    assert sc.normalize_key("data-dir") == "data_dir"
    assert sc.get_field("data-dir") is sc.get_field("data_dir")
    assert sc.get_field("totally_unknown") is None


def test_suggest_keys_finds_near_miss() -> None:
    assert "enable_gdrive" in sc.suggest_keys("enable_gdrve")
    assert "data_dir" in sc.suggest_keys("datadir")


# ── coercion: bool ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
    (True, True), (False, False),
])
def test_coerce_bool_ok(raw: object, expected: bool) -> None:
    fld = sc.get_field("enable_gdrive")
    value, err = fld.coerce(raw)
    assert err is None
    assert value is expected


def test_coerce_bool_bad() -> None:
    value, err = sc.get_field("enable_gdrive").coerce("maybe")
    assert value is None
    assert "boolean" in err


# ── coercion: int / float with range ─────────────────────────────────────────
def test_coerce_int_ok_and_range() -> None:
    fld = sc.get_field("tail_gdelt_max_urls")
    assert fld.coerce("500") == (500, None)
    value, err = fld.coerce("0")  # below minimum=1
    assert value is None and ">=" in err
    value, err = fld.coerce("not-a-number")
    assert value is None and "integer" in err


def test_coerce_float_ok_and_range() -> None:
    fld = sc.get_field("tail_poll_seconds")
    assert fld.coerce("30") == (30.0, None)
    value, err = fld.coerce("0.1")  # below minimum 1.0
    assert value is None and ">=" in err


def test_coerce_max_enforced() -> None:
    fld = sc.get_field("per_domain_concurrency")  # max 1000
    value, err = fld.coerce("5000")
    assert value is None and "<=" in err


# ── coercion: choice ─────────────────────────────────────────────────────────
def test_coerce_choice_normalises_case() -> None:
    fld = sc.get_field("log_level")
    assert fld.coerce("debug") == ("DEBUG", None)
    value, err = fld.coerce("LOUD")
    assert value is None and "one of" in err


# ── coercion: str / path ─────────────────────────────────────────────────────
def test_coerce_str_rejects_empty() -> None:
    value, err = sc.get_field("gdrive_folder_name").coerce("   ")
    assert value is None and "empty" in err


def test_coerce_path_keeps_uri() -> None:
    value, err = sc.get_field("iceberg_warehouse").coerce("s3://bucket/path")
    assert err is None and value == "s3://bucket/path"


# ── value source resolution ──────────────────────────────────────────────────
def test_value_source_precedence() -> None:
    yaml_data = {"enable_iceberg": True, "data_dir": "/data/x"}
    env = {"AW_ENABLE_ICEBERG": "false"}
    assert sc.value_source("enable_iceberg", yaml_data, env) == sc.SOURCE_ENV
    assert sc.value_source("data_dir", yaml_data, env) == sc.SOURCE_YAML
    assert sc.value_source("log_level", yaml_data, env) == sc.SOURCE_DEFAULT


# ── destination plan ─────────────────────────────────────────────────────────
def test_describe_destinations_local_only() -> None:
    plan = sc.describe_destinations(
        local=True, s3=False, gdrive=False,
        data_dir="/data", warehouse=None,
        gdrive_folder="Awareness Captures", gdrive_authorized=False,
    )
    assert not plan.terminal_only
    by_key = {d.key: d for d in plan.destinations}
    assert by_key["local"].enabled and not by_key["s3"].enabled
    assert plan.warnings == []  # nothing enabled that needs a warning


def test_describe_destinations_terminal_only() -> None:
    plan = sc.describe_destinations(
        local=False, s3=False, gdrive=False,
        data_dir="/data", warehouse=None,
        gdrive_folder="X", gdrive_authorized=False,
    )
    assert plan.terminal_only is True


def test_describe_destinations_warns_unauthorized_gdrive() -> None:
    plan = sc.describe_destinations(
        local=False, s3=False, gdrive=True,
        data_dir="/data", warehouse=None,
        gdrive_folder="X", gdrive_authorized=False,
    )
    assert any("authorize" in w.lower() for w in plan.warnings)


def test_describe_destinations_warns_local_warehouse_for_s3() -> None:
    plan = sc.describe_destinations(
        local=False, s3=True, gdrive=False,
        data_dir="/data", warehouse="/some/local/dir",
        gdrive_folder="X", gdrive_authorized=False,
    )
    assert any("cloud URI" in w for w in plan.warnings)


def test_describe_destinations_cloud_warehouse_no_warning() -> None:
    plan = sc.describe_destinations(
        local=False, s3=True, gdrive=False,
        data_dir="/data", warehouse="s3://bucket/wh",
        gdrive_folder="X", gdrive_authorized=False,
    )
    assert plan.warnings == []
