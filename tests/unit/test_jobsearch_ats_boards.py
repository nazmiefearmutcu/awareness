"""ATS board config loading — YAML + embedded defaults."""

from __future__ import annotations

from pathlib import Path

import yaml

from awareness.jobsearch import ats


def test_load_boards_returns_non_empty_lists():
    ats.load_boards.cache_clear()
    boards = ats.load_boards()
    assert isinstance(boards, dict)
    for key in ("greenhouse", "lever", "ashby"):
        assert key in boards
        assert isinstance(boards[key], list)
        assert len(boards[key]) > 0, f"{key} board list should be non-empty"
        assert all(isinstance(x, str) and x.strip() for x in boards[key])


def test_load_boards_from_repo_yaml():
    """Repo configs/jobsearch_boards.yaml should be picked up and non-empty."""
    root = Path(__file__).resolve().parents[2]
    yaml_path = root / "configs" / "jobsearch_boards.yaml"
    assert yaml_path.is_file(), f"expected {yaml_path}"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert len(data.get("greenhouse") or []) >= 30
    assert len(data.get("lever") or []) >= 10
    assert len(data.get("ashby") or []) >= 5

    ats.load_boards.cache_clear()
    boards = ats.load_boards()
    # Should match YAML when load path resolves to repo config
    assert len(boards["greenhouse"]) >= 30
    assert len(boards["lever"]) >= 10
    assert len(boards["ashby"]) >= 5


def test_defaults_are_sane_sizes():
    assert len(ats.DEFAULT_GREENHOUSE_BOARDS) >= 30
    assert len(ats.DEFAULT_LEVER_COMPANIES) >= 10
    assert len(ats.DEFAULT_ASHBY_BOARDS) >= 5


def test_match_query_and_tokens():
    assert ats._match_query("Senior Backend Engineer Python", "backend python")
    assert not ats._match_query("Frontend Designer", "backend python")
    assert ats._match_query("anything", "")
