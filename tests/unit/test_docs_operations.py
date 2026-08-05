"""Static doc-contract checks: docs/operations.md vs the actual CLI.

Extracts every ``awareness <cmd>`` invocation from the operations runbook,
verifies each command is registered (grep of the typer decorators in
``cli/main.py`` + ``alerts/cli.py``, plus a ``--help`` smoke via CliRunner),
checks that every documented env var is read where it matters, and
cross-checks the launchd plist keys in the docs against the plist builders
in ``cli/main.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from awareness.cli.main import app

REPO = Path(__file__).resolve().parents[2]
DOCS = (REPO / "docs" / "operations.md").read_text(encoding="utf-8")
MAIN_PY = (REPO / "src" / "awareness" / "cli" / "main.py").read_text(encoding="utf-8")
ALERTS_CLI_PY = (REPO / "src" / "awareness" / "alerts" / "cli.py").read_text(
    encoding="utf-8"
)
SETTINGS_PY = (REPO / "src" / "awareness" / "config" / "settings.py").read_text(
    encoding="utf-8"
)
SERVER_PY = (REPO / "src" / "awareness" / "api" / "server.py").read_text(
    encoding="utf-8"
)

runner = CliRunner()


# ── documented commands exist ────────────────────────────────────────────────


def _documented_invocations() -> list[str]:
    """``awareness <cmd> [<sub>]`` chains inside the docs' code fences
    (cron/bash/xml blocks) — prose mentions like "the awareness engine"
    are not invocations and are excluded."""
    chains: list[str] = []
    for block in re.findall(r"```[a-z]+\n(.*?)```", DOCS, flags=re.S):
        chains += re.findall(
            r"awareness\s+((?:[a-z][a-z0-9-]*\s+)*[a-z][a-z0-9-]*)", block
        )
    return list(dict.fromkeys(chains))


def _is_registered(chain: str) -> bool:
    """Grep the source for the decorator/typer registration of *chain*."""
    parts = chain.split()
    cmd = parts[0]
    group_pattern = rf'app\.add_typer\([a-z_]+_app, name="{cmd}"\)'
    main_pattern = rf'@app\.command\(name="{cmd}"\)'
    main_fn_pattern = rf"^def {cmd}\("
    if re.search(group_pattern, MAIN_PY):
        return True
    if len(parts) == 1:
        return bool(
            re.search(main_pattern, MAIN_PY) or re.search(main_fn_pattern, MAIN_PY, re.MULTILINE)
        )
    sub = parts[1]
    return any(
        re.search(p, src)
        for p, src in (
            (rf'@alerts_app\.command\(name="{sub}"\)', MAIN_PY),
            (rf'@service_app\.command\("{sub}"\)', MAIN_PY),
            (rf'@app\.command\(name="{sub}"\)', ALERTS_CLI_PY),
        )
    )


def test_operations_documented_commands_are_registered() -> None:
    chains = _documented_invocations()
    assert chains, "docs/operations.md should document at least one CLI invocation"
    # The operations runbook documents the full set of automation entry points.
    assert "briefing" in chains
    assert "report" in chains
    assert "quality" in chains
    for chain in chains:
        assert _is_registered(chain), f"docs document `awareness {chain}` but it is not registered"


def test_operations_documented_commands_help_smoke() -> None:
    for chain in _documented_invocations():
        result = runner.invoke(app, [*chain.split(), "--help"])
        assert result.exit_code == 0, f"`awareness {chain} --help` failed: {result.output}"


def test_quality_record_is_registered() -> None:
    # The cron hook `awareness quality --record` exists (appends a snapshot
    # to <data_dir>/quality_history.jsonl); the operations doc's "does not
    # exist" note predates it and is now stale.
    quality_body = _slice_command(
        MAIN_PY, '@app.command(name="quality")', '@app.command(name="report")'
    )
    assert '"--record"' in quality_body
    assert '"--recorded"' in quality_body
    assert "quality --record" in DOCS
    assert "briefing --save" in DOCS  # the daily snapshot hook is briefing, not quality


# ── documented env vars are real ─────────────────────────────────────────────

# Env var → the file that actually reads it. SMTP_* / EMAIL_FROM are raw
# os.environ reads in cli/main.py's `_email_digest`; AW_ALERTS_AUTOSTART is
# read in api/server.py; only AW_PROJECT_ROOT lives in config/settings.py
# (the pydantic Settings surface uses AW_* knobs, these three are consumed
# directly by their feature code).
ENV_READERS = {
    "AW_PROJECT_ROOT": SETTINGS_PY,
    "SMTP_HOST": MAIN_PY,
    "SMTP_PORT": MAIN_PY,
    "SMTP_USER": MAIN_PY,
    "SMTP_PASSWORD": MAIN_PY,
    "EMAIL_FROM": MAIN_PY,
    "AW_ALERTS_AUTOSTART": SERVER_PY,
}


def test_operations_documented_env_vars_are_read_in_source() -> None:
    for env, source in ENV_READERS.items():
        assert env in DOCS, f"operations.md should document {env}"
        assert env in source, f"{env} documented but not referenced in {source.name}"


def test_operations_env_var_facts() -> None:
    # AW_PROJECT_ROOT is the cron-critical root resolver in settings.py.
    assert 'os.environ.get("AW_PROJECT_ROOT")' in SETTINGS_PY
    # SMTP default port matches _email_digest (587, SSL only on 465).
    assert 'os.environ.get("SMTP_PORT") or "587"' in MAIN_PY
    # The alerts runner is gated on the exact "1" value.
    assert 'os.environ.get("AW_ALERTS_AUTOSTART") == "1"' in SERVER_PY


# ── launchd plist keys ───────────────────────────────────────────────────────


def _plist_block() -> str:
    blocks = re.findall(r"```xml\n(.*?)```", DOCS, flags=re.S)
    assert len(blocks) == 1, "operations.md should contain exactly one plist block"
    return blocks[0]


def _doc_plist_keys() -> set[str]:
    """Top-level <key> entries of the documented plist (nested keys like
    Hour/Minute belong to StartCalendarInterval and are not plist-level)."""
    return set(re.findall(r"^  <key>([A-Za-z]+)</key>$", _plist_block(), flags=re.MULTILINE))


def _slice_command(src: str, start: str, end: str) -> str:
    return src[src.index(start) : src.index(end)]


def _plist_builder_keys(body: str) -> set[str]:
    return set(re.findall(r'"([A-Za-z]+)":', body))


def test_operations_launchd_keys_mirror_service_install() -> None:
    install_body = _slice_command(
        MAIN_PY, '@service_app.command("install")', '@service_app.command("uninstall")'
    )
    install_keys = _plist_builder_keys(install_body)
    # Sanity: the extractor found the real service-install vocabulary.
    assert {"Label", "WorkingDirectory", "ProgramArguments", "StandardOutPath"} <= install_keys
    assert "StartCalendarInterval" not in install_keys

    doc_keys = _doc_plist_keys()
    assert doc_keys, "plist block should carry <key> entries"
    assert "StartCalendarInterval" in doc_keys  # the daily schedule

    # Everything except the launchd-native calendar schedule mirrors the
    # keys `service install` generates (service install runs at load via
    # RunAtLoad/KeepAlive; a scheduled job needs a calendar key instead).
    assert doc_keys - {"StartCalendarInterval"} <= install_keys

    # The full documented key set stays inside the CLI's launchd vocabulary
    # plus StartCalendarInterval (the CLI's own schedule builder uses
    # StartInterval — the same family of launchd schedule keys).
    compact_body = _slice_command(
        MAIN_PY,
        '@service_app.command("schedule-compaction")',
        '@service_app.command("unschedule-compaction")',
    )
    cli_launchd_keys = install_keys | _plist_builder_keys(compact_body)
    assert doc_keys <= cli_launchd_keys | {"StartCalendarInterval"}


# ── retention guidance ───────────────────────────────────────────────────────


def test_operations_retention_guidance() -> None:
    assert "-mtime +30 -delete" in DOCS  # briefings dir prune
    assert "data/alerts.db" in DOCS
    assert "firings" in DOCS  # alerts.db growth note names the growing table
    # The daily snapshot hook is documented as briefing --save, not a
    # nonexistent `quality --record`.
    assert "briefing --save" in DOCS
