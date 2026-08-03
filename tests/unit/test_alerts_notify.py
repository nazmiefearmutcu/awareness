"""Unit tests for webhook delivery (httpx mocked)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from awareness.alerts import notify
from awareness.alerts.models import AlertFiring


@pytest.fixture(autouse=True)
def _allow_public_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the public-host DNS gate for unit tests (httpx is mocked)."""
    monkeypatch.setattr(
        "awareness.alerts.notify.is_public_http_url",
        lambda url: True,
    )


def _firing() -> AlertFiring:
    return AlertFiring(
        id=7,
        rule_id="rule-1",
        rule_name="bitcoin watch",
        kind="term_count",
        term="bitcoin",
        count=5,
        threshold=3.0,
        fired_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        detail="5 docs matched 'bitcoin'",
    )


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeClient:
    """AsyncClient stand-in; records posts and mimics a behavior."""

    def __init__(self, *, timeout: float, behavior: str = "ok") -> None:
        self.timeout = timeout
        self.behavior = behavior
        self.posts: list[tuple[str, dict]] = []
        self.entered = 0

    async def __aenter__(self) -> _FakeClient:
        self.entered += 1
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, url: str, json: dict) -> _FakeResponse:
        self.posts.append((url, json))
        if self.behavior == "ok":
            return _FakeResponse(200)
        if self.behavior == "status":
            return _FakeResponse(500)
        if self.behavior == "raise":
            raise httpx.ConnectError("connection refused")
        raise AssertionError(f"unknown behavior {self.behavior!r}")


async def test_deliver_success_posts_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(timeout=0.0, behavior="ok")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)

    ok = await notify.deliver_webhook("https://hooks.example/alert", _firing())
    assert ok is True
    assert fake.entered == 1
    assert len(fake.posts) == 1
    url, payload = fake.posts[0]
    assert url == "https://hooks.example/alert"
    assert payload["event"] == "alert"
    assert payload["firing"]["id"] == 7
    assert payload["firing"]["term"] == "bitcoin"
    assert payload["firing"]["fired_at"] == "2026-06-01T12:00:00Z"


async def test_deliver_retries_once_then_fails_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(timeout=0.0, behavior="raise")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)
    monkeypatch.setattr(notify, "RETRY_DELAY_SECONDS", 0.0)

    ok = await notify.deliver_webhook("https://hooks.example/alert", _firing())
    assert ok is False  # never raises
    assert len(fake.posts) == 2  # original + one retry


async def test_deliver_retries_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(timeout=0.0, behavior="status")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake)
    monkeypatch.setattr(notify, "RETRY_DELAY_SECONDS", 0.0)

    ok = await notify.deliver_webhook("https://hooks.example/alert", _firing())
    assert ok is False
    assert len(fake.posts) == 2


async def test_deliver_sets_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    class _Client(_FakeClient):
        def __init__(self, **kw: object) -> None:  # type: ignore[no-untyped-def]
            seen.append(float(kw["timeout"]))
            super().__init__(timeout=0.0, behavior="ok")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ok = await notify.deliver_webhook("https://hooks.example/alert", _firing())
    assert ok is True
    assert seen == [notify.TIMEOUT_SECONDS]


def test_validate_webhook_rejects_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSRF gate active (autouse public-gate bypass removed for this test)."""
    import awareness.alerts.notify as notify_mod
    from awareness.alerts.notify import validate_webhook_url

    # Undo the autouse bypass so the real DNS/IP gate runs.
    monkeypatch.undo()

    def _fake_public(url: str) -> bool:
        return url.startswith("https://hooks.example")

    monkeypatch.setattr(notify_mod, "is_public_http_url", _fake_public)
    with pytest.raises(ValueError):
        validate_webhook_url("http://127.0.0.1:9000/hook")
    with pytest.raises(ValueError):
        validate_webhook_url("ftp://example.com/hook")
    with pytest.raises(ValueError):
        validate_webhook_url("http://user:pass@example.com/hook")
    assert validate_webhook_url("https://hooks.example/alert") == "https://hooks.example/alert"
