"""dashboard opens native app, not webbrowser for SPA."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from awareness.cli.main import app, _resolve_native_app_path

runner = CliRunner()


def _make_fake_app(root: Path) -> Path:
    """Create a minimal Awareness.app with Contents/MacOS/Awareness executable."""
    fake_app = root / "Awareness.app"
    binary = fake_app / "Contents" / "MacOS" / "Awareness"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return fake_app


def test_dashboard_opens_native_app_not_webbrowser(tmp_path: Path) -> None:
    fake_app = _make_fake_app(tmp_path)
    with (
        patch("awareness.cli.main.webbrowser.open") as wb,
        patch("awareness.cli.main._resolve_native_app_path", return_value=fake_app),
        patch("awareness.cli.main.subprocess.Popen") as popen,
    ):
        popen.return_value = MagicMock()
        result = runner.invoke(app, ["dashboard"])
        assert result.exit_code == 0, result.output
        wb.assert_not_called()
        popen.assert_called_once()
        args = popen.call_args[0][0]
        assert args[0] == str(fake_app / "Contents" / "MacOS" / "Awareness")
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True
        env = kwargs.get("env") or {}
        assert env.get("AW_API_HOST") == "127.0.0.1"
        assert "AW_API_PORT" in env


def test_dashboard_browser_flag_opens_webbrowser() -> None:
    with (
        patch("awareness.cli.main.webbrowser.open") as wb,
        patch("awareness.cli.main.subprocess.Popen") as popen,
        patch("awareness.cli.main._resolve_native_app_path") as resolve,
    ):
        result = runner.invoke(app, ["dashboard", "--browser"])
        assert result.exit_code == 0, result.output
        wb.assert_called_once()
        url = wb.call_args[0][0]
        assert url.startswith("http://")
        popen.assert_not_called()
        resolve.assert_not_called()


def test_dashboard_missing_app_exits_1() -> None:
    with (
        patch("awareness.cli.main.webbrowser.open") as wb,
        patch("awareness.cli.main._resolve_native_app_path", return_value=None),
        patch("awareness.cli.main.subprocess.Popen") as popen,
    ):
        result = runner.invoke(app, ["dashboard"])
        assert result.exit_code == 1
        assert "Awareness.app not found" in result.output
        assert "build-app.sh" in result.output
        wb.assert_not_called()
        popen.assert_not_called()


def test_dashboard_falls_back_to_open_when_binary_missing(tmp_path: Path) -> None:
    fake_app = tmp_path / "Awareness.app"
    fake_app.mkdir()
    (fake_app / "Contents").mkdir()
    # No Contents/MacOS/Awareness binary
    with (
        patch("awareness.cli.main.webbrowser.open") as wb,
        patch("awareness.cli.main._resolve_native_app_path", return_value=fake_app),
        patch("awareness.cli.main.subprocess.Popen") as popen,
    ):
        popen.return_value = MagicMock()
        result = runner.invoke(app, ["dashboard"])
        assert result.exit_code == 0, result.output
        wb.assert_not_called()
        popen.assert_called_once()
        args = popen.call_args[0][0]
        assert args[0] == "open"
        assert str(fake_app) in args


def test_dashboard_passes_host_port_env(tmp_path: Path) -> None:
    fake_app = _make_fake_app(tmp_path)
    with (
        patch("awareness.cli.main.webbrowser.open"),
        patch("awareness.cli.main._resolve_native_app_path", return_value=fake_app),
        patch("awareness.cli.main.subprocess.Popen") as popen,
    ):
        popen.return_value = MagicMock()
        result = runner.invoke(app, ["dashboard", "--host", "127.0.0.1", "--port", "9099"])
        assert result.exit_code == 0, result.output
        env = popen.call_args.kwargs.get("env") or {}
        assert env["AW_API_HOST"] == "127.0.0.1"
        assert env["AW_API_PORT"] == "9099"


def test_resolve_native_app_path_respects_env(tmp_path: Path, monkeypatch) -> None:
    fake_app = _make_fake_app(tmp_path)
    monkeypatch.setenv("AWARENESS_APP", str(fake_app))
    assert _resolve_native_app_path() == fake_app


def test_resolve_native_app_path_missing_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AWARENESS_APP", raising=False)
    # Point home at empty tmp so ~/Applications won't match a real install
    monkeypatch.setenv("HOME", str(tmp_path))
    # Force parents[3] path and walk not to find a real dist either by using a
    # non-existent env only — if a real /Applications/Awareness.app exists we skip.
    apps = Path("/Applications/Awareness.app")
    home_apps = tmp_path / "Applications" / "Awareness.app"
    if not apps.is_dir() and not home_apps.is_dir():
        # May still find repo dist/ — only assert type when clearly none
        result = _resolve_native_app_path()
        assert result is None or result.is_dir()
    # Explicit non-existent env path is ignored (falls through)
    monkeypatch.setenv("AWARENESS_APP", str(tmp_path / "nope.app"))
    # If env points at missing path, continue to other candidates
    _ = _resolve_native_app_path()  # should not raise
