"""Tail recrawl SSRF guard tests."""

import asyncio

import httpx

from awareness.sources.tail_recrawl import _get_public_url


class RecordingClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        request = httpx.Request("GET", url)
        if url == "https://public.example/news":
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1:8080/admin"},
                request=request,
            )
        return httpx.Response(200, text="internal secret", request=request)


def test_get_public_url_rejects_internal_redirect_before_fetch(monkeypatch) -> None:
    def fake_is_public_http_url(url: str | None) -> bool:
        return url == "https://public.example/news"

    monkeypatch.setattr("awareness.sources.tail_recrawl.is_public_http_url", fake_is_public_http_url)
    client = RecordingClient()

    response = asyncio.run(_get_public_url(client, "https://public.example/news"))  # type: ignore[arg-type]

    assert response is None
    assert client.urls == ["https://public.example/news"]
