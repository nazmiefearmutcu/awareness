from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import click.testing
import pytest
import typer.testing
from typer.testing import CliRunner

from awareness.cli.main import _setup_shell_readline, _shell_click_command, app, highlight_tokens

runner = CliRunner()

_FULL_KEYS = (
    "doc_id", "capture_id", "parent_doc_or_dup_group", "source_type",
    "source_name", "source_locator", "source_shard",
    "source_offset_or_record_id", "discovery_channel", "job_id", "batch_id",
    "ingest_version", "url", "canonical_url", "domain", "fetch_ts",
    "observed_ts", "published_ts", "last_modified", "content_type",
    "http_status", "etag", "title", "text", "language", "content_hash",
    "near_dup_hash", "robots_decision", "terms_note_if_relevant",
)

def _write_doc(root: Path, idx: int, *, title: str, text: str, domain: str = "example.com") -> None:
    day = root / "data" / "jsonl" / "captures" / "2026" / "06" / "01"
    day.mkdir(parents=True, exist_ok=True)
    rec: dict[str, object] = {k: None for k in _FULL_KEYS}
    rec.update(
        doc_id=f"doc-{idx}",
        capture_id=f"cap-{idx}",
        source_type="rss",
        domain=domain,
        url=f"https://{domain}/{idx}",
        fetch_ts="2026-06-01T12:00:00+00:00",
        title=title,
        text=text,
    )
    (day / f"chunk-{idx}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


class MockStdin:
    def __init__(self, real_stdin: Any) -> None:
        self._real_stdin = real_stdin

    def isatty(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_stdin, name)

    def __iter__(self) -> Any:
        return iter(self._real_stdin)


class MockSys:
    def __init__(self, real_sys: Any) -> None:
        self._real_sys = real_sys

    @property
    def stdin(self) -> MockStdin:
        return MockStdin(self._real_sys.stdin)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_sys, name)


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    if hasattr(typer.testing, "_NamedTextIOWrapper"):
        monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True, raising=False)
    if hasattr(click.testing, "_NamedTextIOWrapper"):
        monkeypatch.setattr(click.testing._NamedTextIOWrapper, "isatty", lambda self: True, raising=False)
    if hasattr(click.testing, "EchoingStdin"):
        monkeypatch.setattr(click.testing.EchoingStdin, "isatty", lambda self: True, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def _collect_completions(completer_fn: Any, mock_rl: Any, line_buffer: str, begidx: int, prefix: str) -> list[str]:
    mock_rl.get_line_buffer.return_value = line_buffer
    mock_rl.get_begidx.return_value = begidx
    completions = []
    state = 0
    while True:
        res = completer_fn(prefix, state)
        if res is None:
            break
        completions.append(res)
        state += 1
    return completions


def test_highlight_tokens_helper() -> None:
    # 1. Empty query
    assert highlight_tokens("hello world", "") == "hello world"
    # 2. Token too short
    assert highlight_tokens("hello world", "a") == "hello world"
    # 3. Simple matching
    assert highlight_tokens("The sports news was great.", "sports") == "The [bold yellow]sports[/bold yellow] news was great."
    # 4. Prefix matching
    assert highlight_tokens("The financial report.", "financ") == "The [bold yellow]financial[/bold yellow] report."
    # 5. Case insensitivity
    assert highlight_tokens("The SPORTS news.", "sports") == "The [bold yellow]SPORTS[/bold yellow] news."
    # 6. HTML/Rich tag escaping
    assert highlight_tokens("An [awesome] link.", "awesome") == "An \\[[bold yellow]awesome[/bold yellow]] link."


def test_search_non_interactive_highlighting(tmp_project: Path) -> None:
    _write_doc(tmp_project, 1, title="Breaking sports news", text="Football match ended today.")
    
    result = runner.invoke(app, ["search", "sports", "--no-interactive"])
    assert result.exit_code == 0
    assert "• Breaking sports news" in result.output


def test_search_interactive_table_highlighting(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch)
    _write_doc(tmp_project, 1, title="Breaking sports news", text="Football match ended today.")
    
    # We pass "q\n" to quit the search results screen
    result = runner.invoke(app, ["search", "sports"], input="q\n")
    assert result.exit_code == 0
    assert "Search Results for" in result.output and "sports" in result.output


def test_browse_query_highlighting_list_and_read(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch)
    _write_doc(tmp_project, 1, title="Breaking sports news", text="Football match ended today.")

    # Run browse with query: should only show matching doc
    result = runner.invoke(app, ["browse", "--query", "sports"], input="1\n\nq\n")
    assert result.exit_code == 0
    assert "sports" in result.output
    assert "DOCUMENT READ VIEW" in result.output
    assert "Title:       Breaking sports news" in result.output


def test_shell_history_persistence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _force_tty(monkeypatch)

    # Mock readline
    mock_rl = MagicMock()
    mock_rl.__doc__ = "GNU Readline support"
    monkeypatch.setitem(sys.modules, "readline", mock_rl)

    # Mock ~/.awareness_history path
    mock_history_path = tmp_path / ".awareness_history"
    mock_history_path.touch()

    original_expanduser = Path.expanduser
    def mock_expanduser(self: Path) -> Path:
        if str(self) == "~/.awareness_history":
            return mock_history_path
        return original_expanduser(self)
    monkeypatch.setattr(Path, "expanduser", mock_expanduser)

    # Invoke interactive shell and exit
    result = runner.invoke(app, ["shell"], input="exit\n")
    assert result.exit_code == 0

    # Verify history was read and written
    mock_rl.read_history_file.assert_called_once_with(str(mock_history_path))
    mock_rl.write_history_file.assert_called_with(str(mock_history_path))


def test_shell_autocomplete_top_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mock readline
    mock_rl = MagicMock()
    mock_rl.__doc__ = "GNU Readline support"
    monkeypatch.setitem(sys.modules, "readline", mock_rl)

    # Get click command tree
    click_cmd = _shell_click_command()
    
    # Initialize shell readline setup
    success = _setup_shell_readline(click_cmd, None)
    assert success is True

    # Get the completer function registered to readline
    completer_fn = mock_rl.set_completer.call_args[0][0]

    # Test top-level command completions (no slash)
    # Cursor is at "se"
    completions = _collect_completions(completer_fn, mock_rl, "se", 0, "se")
    assert "search" in completions
    assert "service" in completions

    # Test top-level command completions with slash prefix
    # Cursor is at "/se"
    completions_slash = _collect_completions(completer_fn, mock_rl, "/se", 0, "/se")
    assert "/search" in completions_slash
    assert "/service" in completions_slash

    # Test top-level command completions starting with prefix like "/c"
    # should suggest "/config", "/cloud", etc.
    completions_c = _collect_completions(completer_fn, mock_rl, "/c", 0, "/c")
    assert "/cloud" in completions_c
    assert "/compact" in completions_c
    # Every completion should start with a slash
    assert all(c.startswith("/") for c in completions_c)

    # Test top-level command completions (no slash)
    # Cursor is at "sea"
    mock_rl.get_line_buffer.return_value = "sea"
    mock_rl.get_begidx.return_value = 0
    assert completer_fn("sea", 0) == "search"
    assert completer_fn("sea", 1) is None

    # Test top-level command completions with slash prefix
    # Cursor is at "/sea"
    mock_rl.get_line_buffer.return_value = "/sea"
    mock_rl.get_begidx.return_value = 0
    assert completer_fn("/sea", 0) == "/search"
    assert completer_fn("/sea", 1) is None


def test_shell_autocomplete_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mock readline
    mock_rl = MagicMock()
    mock_rl.__doc__ = "GNU Readline support"
    monkeypatch.setitem(sys.modules, "readline", mock_rl)

    # Get click command tree
    click_cmd = _shell_click_command()
    
    # Initialize shell readline setup
    success = _setup_shell_readline(click_cmd, None)
    assert success is True

    # Get the completer function registered to readline
    completer_fn = mock_rl.set_completer.call_args[0][0]

    # Test subcommand completions (no slash)
    # Cursor is at "cloud st"
    mock_rl.get_line_buffer.return_value = "cloud st"
    mock_rl.get_begidx.return_value = 6
    assert completer_fn("st", 0) == "status"
    assert completer_fn("st", 1) is None

    # Test subcommand completions with slash prefix
    # Cursor is at "/cloud /st"
    mock_rl.get_line_buffer.return_value = "/cloud /st"
    mock_rl.get_begidx.return_value = 7
    assert completer_fn("/st", 0) == "/status"
    assert completer_fn("/st", 1) is None
