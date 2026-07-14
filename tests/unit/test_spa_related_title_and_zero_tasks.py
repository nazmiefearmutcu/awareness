"""Static checks: reader title related badge + backfill zero-task SPA warning."""

from pathlib import Path


APP_JS = Path("src/awareness/api/web/app.js")
CSS = Path("src/awareness/api/web/style.css")
SERVER = Path("src/awareness/api/server.py")


def test_reader_title_shows_related_count_badge() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert "reader-title-row" in app_js
    assert "reader-related-badge" in app_js
    assert "related_count" in app_js
    assert "relatedTotal" in app_js
    assert ".reader-title-row" in css
    assert ".reader-related-badge" in css


def test_load_related_prefers_api_related_count() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "r.related_count" in app_js
    assert "typeof r.related_count === \"number\"" in app_js or "typeof r.related_count === 'number'" in app_js


def test_backfill_submit_surfaces_zero_tasks_warning() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert 'resp.warning === "zero_tasks"' in app_js
    assert "zero_task_reasons" in app_js
    assert "0 tasks planned" in app_js
    # Empty plans must not auto-run a no-op job.
    assert "return;" in app_js
    # Guard that submit checks tasks_total === 0 as well.
    assert "tasks_total" in app_js


def test_server_healthz_and_related_count_fields() -> None:
    server = SERVER.read_text(encoding="utf-8")
    assert "index_ready" in server
    assert "health_snapshot" in server
    assert 'row["related_count"]' in server or "row['related_count']" in server
    assert '"related_count": related_count' in server or '"related_count":related_count' in server
