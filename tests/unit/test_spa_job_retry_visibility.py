"""Static checks: SPA surfaces job dead-letter / retry counts and tail retry list."""

from pathlib import Path


APP_JS = Path("src/awareness/api/web/app.js")
HTML = Path("src/awareness/api/web/index.html")
CSS = Path("src/awareness/api/web/style.css")


def test_spa_job_counters_show_failed_and_dead_lettered() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "function appendJobRetryBits" in app_js
    assert "tasks_failed" in app_js
    assert "tasks_dead_lettered" in app_js
    assert "dead-lettered" in app_js
    assert "appendJobRetryBits(counters, j)" in app_js


def test_spa_tail_shows_retry_scheduled() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")

    assert "retry_scheduled_count" in app_js
    assert "data.retry_scheduled" in app_js
    assert 'id="tail-retry-list"' in html
    assert 'id="tail-retry-meta"' in html
    assert "Retrying" in html
    assert "tn-attempts" in app_js
    assert ".tn-attempts" in css
    assert "ctr-retry" in app_js


def test_server_tail_status_documents_retry_fields() -> None:
    server = Path("src/awareness/api/server.py").read_text(encoding="utf-8")
    assert "retry_scheduled_count" in server
    assert "list_retry_scheduled_tasks" in server
    assert "count_retry_scheduled" in server
