"""Tests for the `config` command group and the `configure` command.

All tests run under the `tmp_project` fixture, which points AW_PROJECT_ROOT at
a throwaway dir — so every write lands in an isolated configs/awareness.yaml,
never the real repo file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from typer.testing import CliRunner

from awareness.cli.main import _is_writable_dir, app

runner = CliRunner()


def _yaml_path(root: Path) -> Path:
    return root / "configs" / "awareness.yaml"


def _read_yaml(root: Path) -> dict:
    p = _yaml_path(root)
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


# ── configure: scriptable flag mode ──────────────────────────────────────────
def test_configure_show_default_plan(tmp_project: Path) -> None:
    result = runner.invoke(app, ["configure", "--show"])
    assert result.exit_code == 0
    assert "Where TAIL writes captures" in result.output
    assert "Local JSONL" in result.output
    assert "Google Drive" in result.output


def test_configure_flags_persist_and_route(tmp_project: Path) -> None:
    result = runner.invoke(
        app, ["configure", "--non-interactive", "--local", "--no-s3", "--no-gdrive"]
    )
    assert result.exit_code == 0
    data = _read_yaml(tmp_project)
    assert data["enable_jsonl_staging"] is True
    assert data["enable_iceberg"] is False
    assert data["enable_gdrive"] is False


def test_configure_terminal_only(tmp_project: Path) -> None:
    result = runner.invoke(app, ["configure", "--non-interactive", "--terminal-only", "--yes"])
    assert result.exit_code == 0
    data = _read_yaml(tmp_project)
    assert data["enable_jsonl_staging"] is False
    assert data["enable_iceberg"] is False
    assert data["enable_gdrive"] is False
    show = runner.invoke(app, ["configure", "--show"])
    assert "TERMINAL-ONLY" in show.output


def test_configure_warehouse_cloud_uri(tmp_project: Path) -> None:
    result = runner.invoke(
        app, ["configure", "--non-interactive", "--s3", "--warehouse", "s3://bucket/wh"]
    )
    assert result.exit_code == 0
    data = _read_yaml(tmp_project)
    assert data["iceberg_warehouse"] == "s3://bucket/wh"
    assert data["enable_iceberg"] is True
    # cloud URI → no "local path" warning in the plan
    assert "warehouse is a local path" not in result.output


def test_configure_rejects_out_of_range(tmp_project: Path) -> None:
    result = runner.invoke(app, ["configure", "--non-interactive", "--poll-seconds", "0.1"])
    assert result.exit_code == 1
    assert "tail_poll_seconds" in result.output
    # nothing should have been written
    assert not _yaml_path(tmp_project).exists() or "tail_poll_seconds" not in _read_yaml(tmp_project)


def test_configure_non_interactive_no_flags_changes_nothing(tmp_project: Path) -> None:
    result = runner.invoke(app, ["configure", "--non-interactive"])
    assert result.exit_code == 0
    assert "No flags given" in result.output
    assert _read_yaml(tmp_project) == {}


def test_configure_reset(tmp_project: Path) -> None:
    runner.invoke(app, ["configure", "--non-interactive", "--no-local", "--no-s3"])
    assert _read_yaml(tmp_project)  # something written
    result = runner.invoke(app, ["configure", "--reset", "--yes"])
    assert result.exit_code == 0
    data = _read_yaml(tmp_project)
    for k in ("enable_jsonl_staging", "enable_iceberg", "enable_gdrive"):
        assert k not in data


# ── configure: interactive wizard ────────────────────────────────────────────
def test_configure_wizard_local_only(tmp_project: Path) -> None:
    # local? y · data dir (default) · gzip? n · s3? n · gdrive? n ·
    # poll (default) · gdelt? n · show captures? y · save? y
    wiz = "y\n\nn\nn\nn\n\nn\ny\ny\n"
    result = runner.invoke(app, ["configure"], input=wiz)
    assert result.exit_code == 0
    assert "Saved" in result.output
    data = _read_yaml(tmp_project)
    assert data["enable_jsonl_staging"] is True
    assert data["enable_iceberg"] is False
    assert data["enable_gdrive"] is False


def test_configure_wizard_abort_saves_nothing(tmp_project: Path) -> None:
    wiz = "y\n\nn\nn\nn\n\nn\ny\nn\n"  # final "save?" → n
    result = runner.invoke(app, ["configure"], input=wiz)
    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert _read_yaml(tmp_project) == {}


# ── config show / get ────────────────────────────────────────────────────────
def test_config_show_is_sectioned(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "Storage routing" in result.output
    assert "Destination targets" in result.output
    assert "enable_gdrive" in result.output


def test_config_show_json(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["enable_gdrive"]["value"] is False
    assert payload["enable_gdrive"]["source"] == "default"


def test_config_get_known(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "get", "enable-gdrive"])
    assert result.exit_code == 0
    assert "enable_gdrive" in result.output
    assert "default" in result.output


def test_config_get_unknown_suggests(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "get", "enable_gdrve"])
    assert result.exit_code == 1
    assert "Did you mean" in result.output
    assert "enable_gdrive" in result.output


# ── config set / unset / reset ───────────────────────────────────────────────
def test_config_set_valid(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "set", "enable-gdrive", "true"])
    assert result.exit_code == 0
    assert _read_yaml(tmp_project)["enable_gdrive"] is True
    got = runner.invoke(app, ["config", "get", "enable_gdrive"])
    assert "True" in got.output and "yaml" in got.output


def test_config_set_unknown_key(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "set", "not_a_setting", "1"])
    assert result.exit_code == 1
    assert "not a valid configuration setting" in result.output


def test_config_set_out_of_range(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "set", "tail-poll-seconds", "0.1"])
    assert result.exit_code == 1
    assert "tail_poll_seconds" in result.output


def test_config_set_bad_bool(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "set", "enable_iceberg", "maybe"])
    assert result.exit_code == 1
    assert "boolean" in result.output


def test_config_unset_reverts(tmp_project: Path) -> None:
    runner.invoke(app, ["config", "set", "enable_gdrive", "true"])
    result = runner.invoke(app, ["config", "unset", "enable_gdrive"])
    assert result.exit_code == 0
    assert "enable_gdrive" not in _read_yaml(tmp_project)


def test_config_unset_missing(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "unset", "enable_gdrive"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.output


def test_config_reset(tmp_project: Path) -> None:
    runner.invoke(app, ["config", "set", "enable_gdrive", "true"])
    result = runner.invoke(app, ["config", "reset", "--yes"])
    assert result.exit_code == 0
    assert _read_yaml(tmp_project) == {}


# ── config validate / path / doctor ──────────────────────────────────────────
def test_config_validate_clean(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "valid" in result.output.lower()


def test_config_validate_detects_problems(tmp_project: Path) -> None:
    p = _yaml_path(tmp_project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("enable_iceberg: notabool\nbogus_key: 1\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "bogus_key" in result.output  # unknown-key warning
    assert "enable_iceberg" in result.output  # invalid value


def test_config_path(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    # The long temp path may word-wrap, so assert on stable, unwrapped tokens.
    assert "Config file" in result.output
    assert "exists:" in result.output


def test_config_doctor_default_ok(tmp_project: Path) -> None:
    # defaults: local + iceberg(local path) enabled, both under the writable tmp root
    result = runner.invoke(app, ["config", "doctor"])
    assert result.exit_code == 0
    assert "Destination health" in result.output


def test_config_doctor_flags_unauthorized_gdrive(tmp_project: Path) -> None:
    runner.invoke(app, ["config", "set", "enable_gdrive", "true"])
    result = runner.invoke(app, ["config", "doctor"])
    assert result.exit_code == 1
    assert "not authorized" in result.output.lower() or "NOT authorized" in result.output


# ── tail honours persisted destinations ──────────────────────────────────────
def test_tail_start_honours_configured_terminal_only(tmp_project: Path) -> None:
    runner.invoke(app, ["configure", "--non-interactive", "--terminal-only", "--yes"])
    result = runner.invoke(app, ["tail", "start", "--no-interactive", "--duration", "1"])
    assert result.exit_code == 0
    assert "terminal only (NOT saved)" in result.output
    assert "from `awareness configure`" in result.output
    assert "Tail stopped cleanly" in result.output


def test_tail_start_explicit_flag_overrides_config(tmp_project: Path) -> None:
    runner.invoke(app, ["configure", "--non-interactive", "--terminal-only", "--yes"])
    result = runner.invoke(
        app, ["tail", "start", "--no-interactive", "--to-local", "--duration", "1"]
    )
    assert result.exit_code == 0
    assert "local" in result.output  # explicit --to-local won over the persisted off


# ── review-driven coverage additions ─────────────────────────────────────────
def test_configure_terminal_only_warns_on_conflicting_flags(tmp_project: Path) -> None:
    result = runner.invoke(
        app, ["configure", "--non-interactive", "--terminal-only", "--local", "--yes"]
    )
    assert result.exit_code == 0
    assert "overrides" in result.output  # warns it ignored --local
    # --terminal-only still wins: local ends up disabled
    assert _read_yaml(tmp_project)["enable_jsonl_staging"] is False


def test_config_show_section_filter(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "show", "--section", "Destination"])
    assert result.exit_code == 0
    assert "Destination targets" in result.output
    assert "iceberg_warehouse" in result.output
    assert "Politeness" not in result.output  # other sections filtered out


def test_config_show_all_includes_derived(tmp_project: Path) -> None:
    result = runner.invoke(app, ["config", "show", "--all"])
    assert result.exit_code == 0
    # a derived/advanced field that is NOT in the documented schema
    assert "iceberg_catalog_db" in result.output or "Derived / advanced" in result.output


def test_config_get_reports_env_source(tmp_project: Path, monkeypatch) -> None:
    monkeypatch.setenv("AW_ENABLE_GDRIVE", "true")
    result = runner.invoke(app, ["config", "get", "enable_gdrive"])
    assert result.exit_code == 0
    assert "env" in result.output  # env override beats yaml/default


def test_config_edit_creates_file_with_header(tmp_project: Path, monkeypatch) -> None:
    monkeypatch.setenv("EDITOR", "true")  # the no-op `true` command stands in for an editor
    monkeypatch.delenv("VISUAL", raising=False)
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 0
    p = _yaml_path(tmp_project)
    assert p.exists()
    assert "Awareness configuration" in p.read_text(encoding="utf-8")


def test_configure_wizard_s3_local_path_warns(tmp_project: Path) -> None:
    wh = str(tmp_project / "wh")
    # local? n · s3? y · warehouse=<local path> · gdrive? n · poll(default) · gdelt? n · show? y · save? y
    wiz = f"n\ny\n{wh}\nn\n\nn\ny\ny\n"
    result = runner.invoke(app, ["configure"], input=wiz)
    assert result.exit_code == 0
    assert "local path" in result.output  # warns the warehouse isn't a cloud URI


def test_configure_wizard_gdrive_unauthorized_warns(tmp_project: Path) -> None:
    # local? n · s3? n · gdrive? y · folder(default) · poll(default) · gdelt? n · show? y · save? y
    wiz = "n\nn\ny\n\n\nn\ny\ny\n"
    result = runner.invoke(app, ["configure"], input=wiz)
    assert result.exit_code == 0
    assert "authoriz" in result.output.lower()  # warns Drive isn't authorized


def test_is_writable_dir_detects_readonly(tmp_project: Path) -> None:
    good = tmp_project / "writable"
    assert _is_writable_dir(good) is True

    ro = tmp_project / "readonly"
    ro.mkdir(parents=True, exist_ok=True)
    os.chmod(ro, 0o500)  # r-x, no write
    try:
        # Skip if running as root (root bypasses permission bits).
        if os.geteuid() != 0:
            assert _is_writable_dir(ro) is False
    finally:
        # Owner-only rwx — the minimum that lets pytest traverse + delete the dir
        # during teardown. nosemgrep: 0o700 is restrictive, not "widely permissive".
        os.chmod(ro, 0o700)  # nosemgrep


def test_configure_reset_preserves_non_destination_keys(tmp_project: Path) -> None:
    runner.invoke(app, ["config", "set", "per_domain_concurrency", "5"])
    runner.invoke(app, ["configure", "--non-interactive", "--no-local", "--no-s3"])
    result = runner.invoke(app, ["configure", "--reset", "--yes"])
    assert result.exit_code == 0
    data = _read_yaml(tmp_project)
    assert data.get("per_domain_concurrency") == 5  # non-destination key untouched
    assert "enable_jsonl_staging" not in data  # destination key cleared
