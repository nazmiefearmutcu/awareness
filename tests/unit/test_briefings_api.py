"""API tests for the /briefings router (list + single-briefing read).

Mounts :func:`~awareness.briefings.router.create_briefings_router` on a bare
FastAPI app wired to a tmp briefings directory (filesystem-backed — no index
or DuckDB involved) and drives it with FastAPI's TestClient.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from awareness.briefings.router import create_briefings_router


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _valid_payload() -> dict:
    return {
        "generated_at": "2026-08-05T06:00:00+00:00",
        "days": 1,
        "total_captures": 120,
        "movers": [
            {"term": "bitcoin", "count": 9, "zscore": 3.2},
            {"term": "ethereum", "count": 5, "zscore": 2.1},
        ],
        "top_terms": [
            {"term": "bitcoin", "count": 18},
            {"term": "ethereum", "count": 11},
        ],
        "new_domains": [{"domain": "spike.news", "count": 10}],
    }


def _seed_dir(tmp_path: Path) -> Path:
    """Three files: a valid briefing, a legacy one without top_terms, and a
    corrupt one (non-JSON text)."""
    briefings = tmp_path / "briefings"
    briefings.mkdir()
    _write(briefings / "2026-08-05.json", json.dumps(_valid_payload(), indent=2))
    _write(
        briefings / "2026-08-04.json",
        json.dumps(
            {
                "generated_at": "2026-08-04T06:00:00+00:00",
                "movers": [],
                "new_domains": [],
            }
        ),
    )
    _write(briefings / "2026-08-03.json", "not json {{{")
    return briefings


def _client(briefings_dir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_briefings_router(lambda: briefings_dir))
    return TestClient(app)


def test_list_returns_all_files_newest_first(tmp_path: Path) -> None:
    briefings = _seed_dir(tmp_path)
    with _client(briefings) as client:
        res = client.get("/briefings")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 3
    # Newest first by filename.
    assert [b["date"] for b in body] == ["2026-08-05", "2026-08-04", "2026-08-03"]
    valid, legacy, corrupt = body

    assert valid["name"] is None
    assert valid["generated_at"] == "2026-08-05T06:00:00+00:00"
    assert valid["movers_count"] == 2
    assert valid["top_terms"] == ["bitcoin", "ethereum"]
    assert valid["size_bytes"] > 0
    assert valid["path"].endswith("2026-08-05.json")

    # Legacy file without top_terms: metadata intact, terms stay null.
    assert legacy["generated_at"] == "2026-08-04T06:00:00+00:00"
    assert legacy["movers_count"] == 0
    assert legacy["top_terms"] is None

    # Corrupt file: nulls everywhere but still listed with a size.
    assert corrupt["generated_at"] is None
    assert corrupt["movers_count"] is None
    assert corrupt["top_terms"] is None
    assert corrupt["size_bytes"] > 0
    assert corrupt["path"].endswith("2026-08-03.json")


def test_get_briefing_by_date_200_with_content(tmp_path: Path) -> None:
    briefings = _seed_dir(tmp_path)
    with _client(briefings) as client:
        res = client.get("/briefings/2026-08-05")
    assert res.status_code == 200
    body = res.json()
    assert body["briefing"]["date"] == "2026-08-05"
    assert body["briefing"]["movers_count"] == 2
    # Content is the full parsed JSON object.
    assert body["content"]["generated_at"] == "2026-08-05T06:00:00+00:00"
    assert body["content"]["movers"][0]["term"] == "bitcoin"
    assert body["content"]["top_terms"][1]["count"] == 11
    assert body["content"]["new_domains"] == [{"domain": "spike.news", "count": 10}]


def test_get_briefing_named_suffix(tmp_path: Path) -> None:
    briefings = tmp_path / "briefings"
    briefings.mkdir()
    _write(briefings / "2026-08-05-weekly.json", json.dumps(_valid_payload()))
    with _client(briefings) as client:
        res = client.get("/briefings/2026-08-05-weekly")
    assert res.status_code == 200
    body = res.json()
    assert body["briefing"]["date"] == "2026-08-05"
    assert body["briefing"]["name"] == "weekly"


def test_get_briefing_404_unknown(tmp_path: Path) -> None:
    briefings = _seed_dir(tmp_path)
    with _client(briefings) as client:
        assert client.get("/briefings/2026-01-01").status_code == 404


def test_get_briefing_400_bad_date_format(tmp_path: Path) -> None:
    briefings = _seed_dir(tmp_path)
    with _client(briefings) as client:
        assert client.get("/briefings/2026-8-5").status_code == 400
        assert client.get("/briefings/not-a-date").status_code == 400
        assert client.get("/briefings/2026-08-05-").status_code == 400
        assert client.get("/briefings/2026-08-05 extra").status_code == 400


def test_list_empty_dir_and_missing_dir(tmp_path: Path) -> None:
    # Missing directory (nothing saved yet) → empty list.
    briefings = tmp_path / "briefings"
    with _client(briefings) as client:
        assert client.get("/briefings").json() == []
    # Existing but empty directory → empty list too.
    briefings.mkdir()
    with _client(briefings) as client:
        res = client.get("/briefings")
    assert res.status_code == 200
    assert res.json() == []
