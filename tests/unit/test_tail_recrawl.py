"""Tail recrawl SSRF guard + transient HTTP retry tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from awareness.sources.tail_recrawl import _get_public_url
from awareness.util.http import RetryableHTTPError, get_with_retries, reset_global_fetch_semaphore


class RecordingClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs) -> httpx.Response:
        self.urls.append(url)
        request = httpx.Request("GET", url)
        if url == "https://public.example/news":
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1:8080/admin"},
                request=request,
            )
        return httpx.Response(200, text="internal secret", request=request)


@pytest.fixture(autouse=True)
def _reset_fetch_sem() -> None:
    reset_global_fetch_semaphore()
    yield
    reset_global_fetch_semaphore()


def test_get_public_url_rejects_internal_redirect_before_fetch(monkeypatch) -> None:
    def fake_is_public_http_url(url: str | None) -> bool:
        return url == "https://public.example/news"

    monkeypatch.setattr("awareness.sources.tail_recrawl.is_public_http_url", fake_is_public_http_url)
    client = RecordingClient()

    response = asyncio.run(_get_public_url(client, "https://public.example/news"))  # type: ignore[arg-type]

    assert response is None
    assert client.urls == ["https://public.example/news"]


@pytest.mark.asyncio
async def test_get_public_url_retries_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Article fetch path must retry transient 503 via get_with_retries."""
    monkeypatch.setattr(
        "awareness.sources.tail_recrawl.is_public_http_url",
        lambda url: bool(url and str(url).startswith("https://")),
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="<html>ok</html>", request=request)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 5)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.tail_recrawl.get_with_retries", fast_get)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await _get_public_url(client, "https://news.example/article")

    assert resp is not None
    assert resp.status_code == 200
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_get_public_url_raises_after_exhausted_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "awareness.sources.tail_recrawl.is_public_http_url",
        lambda url: True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    real = get_with_retries

    async def fast_get(client, url, **kwargs):
        kwargs.setdefault("base_delay", 0.0)
        kwargs.setdefault("max_attempts", 3)
        return await real(client, url, **kwargs)

    monkeypatch.setattr("awareness.sources.tail_recrawl.get_with_retries", fast_get)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RetryableHTTPError):
            await _get_public_url(client, "https://news.example/down")
