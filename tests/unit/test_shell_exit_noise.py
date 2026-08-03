"""L-02: ``_shell_dispatch`` must mirror exit codes silently — no "Error: 1" noise.

``typer.Exit`` / ``click.exceptions.Exit`` are RuntimeError subclasses in
click 8 (not SystemExit); the old generic handler printed "Error: <code>"
for every non-zero command exit inside the REPL.
"""

from __future__ import annotations

from pathlib import Path

import click

from awareness.cli.main import _shell_click_command, _shell_dispatch


def test_shell_dispatch_mirrors_typer_exit_silently(tmp_project: Path) -> None:
    """A command raising typer.Exit(2) returns 2 and prints nothing."""
    click_cmd = _shell_click_command()
    # `browse --unique invalid` rprints the message itself then raises
    # typer.Exit(code=2) — the dispatch layer must not add "Error: 2".
    code = _shell_dispatch(click_cmd, ["browse", "--unique", "invalid"])
    assert code == 2


def test_shell_dispatch_exit_zero_for_ok_command(tmp_project: Path) -> None:
    click_cmd = _shell_click_command()
    code = _shell_dispatch(click_cmd, ["health"])
    assert code == 0


def test_shell_dispatch_unknown_command_is_usage_error(capsys) -> None:
    click_cmd = _shell_click_command()
    code = _shell_dispatch(click_cmd, ["definitely-not-a-command"])
    assert code == 2
    out = capsys.readouterr().out
    # Usage errors legitimately print "Error: No such command…" — the bug was
    # bare "Error: <code>" noise from Exit, which must not appear.
    assert "Error: 2" not in out
    assert "No such command" in out


def test_shell_dispatch_direct_click_exit(capsys) -> None:
    """Direct Exit exceptions (not typer.Exit) are handled the same way."""
    click_cmd = _shell_click_command()

    def _boom_exit() -> None:
        raise click.exceptions.Exit(3)

    click_cmd = click.Command(
        "boom",
        callback=_boom_exit,
        params=[],
    )
    code = _shell_dispatch(click_cmd, [])
    assert code == 3
    out = capsys.readouterr().out
    assert "Error:" not in out
