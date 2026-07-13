"""URL canonicalization tests."""

import socket

from awareness.util.urls import (
    canonical_url,
    domain_of,
    is_homepage_url,
    is_http_url,
    is_public_http_url,
)


def test_canonical_url_lowercases_scheme_host_and_strips_default_port() -> None:
    assert canonical_url("HTTPS://Example.COM:443/foo") == "https://example.com/foo"
    # http upgrades to https for identity; :80 dropped with the upgrade.
    assert canonical_url("http://Example.COM:80/foo") == "https://example.com/foo"
    # Non-default ports survive the scheme upgrade.
    assert canonical_url("http://Example.COM:8080/foo") == "https://example.com:8080/foo"


def test_canonical_url_http_https_same_identity() -> None:
    """Same article via http vs https must share one fetch-gate key."""
    http_u = canonical_url("http://news.example/story/1")
    https_u = canonical_url("https://news.example/story/1")
    assert http_u == https_u == "https://news.example/story/1"


def test_canonical_url_drops_tracking_params() -> None:
    raw = "https://news.example/article?id=42&utm_source=tw&utm_medium=organic&fbclid=xyz"
    out = canonical_url(raw)
    assert out is not None
    assert "utm_source" not in out
    assert "utm_medium" not in out
    assert "fbclid" not in out
    assert "id=42" in out


def test_canonical_url_strips_any_utm_prefix_and_extra_trackers() -> None:
    raw = (
        "https://news.example/article?id=7"
        "&utm_custom_channel=newsletter"
        "&gbraid=abc&wbraid=def&igshid=1&mkt_tok=x"
    )
    out = canonical_url(raw)
    assert out == "https://news.example/article?id=7"


def test_canonical_url_drops_fragment_and_sorts_query() -> None:
    a = canonical_url("https://x.test/p?b=2&a=1#section")
    b = canonical_url("https://x.test/p?a=1&b=2")
    assert a == b


def test_canonical_url_handles_garbage() -> None:
    assert canonical_url("") is None
    assert canonical_url(None) is None
    assert canonical_url("not a url") is None


def test_canonical_url_strips_www_for_news_identity() -> None:
    """Same article via www vs apex must share one canonical identity."""
    with_www = canonical_url("https://www.bbc.co.uk/news/world-123")
    without = canonical_url("https://bbc.co.uk/news/world-123")
    assert with_www == without == "https://bbc.co.uk/news/world-123"
    # Case-insensitive www. after host lowercasing.
    assert canonical_url("https://WWW.Example.COM/story") == "https://example.com/story"
    # Do not strip non-alias labels (www2 is a real host).
    assert canonical_url("https://www2.example.com/x") == "https://www2.example.com/x"


def test_canonical_url_strips_mobile_and_amp_hosts() -> None:
    """m./mobile./amp. subdomains are news aliases of the apex article."""
    assert canonical_url("https://m.example.com/x") == "https://example.com/x"
    assert canonical_url("https://mobile.example.com/x") == "https://example.com/x"
    assert canonical_url("https://amp.example.com/x") == "https://example.com/x"
    # Stacked aliases: www + mobile.
    assert canonical_url("https://www.m.bbc.co.uk/news/1") == "https://bbc.co.uk/news/1"
    # Non-prefix m. mid-label must not be stripped.
    assert canonical_url("https://forum.example.com/x") == "https://forum.example.com/x"


def test_canonical_url_strips_amp_path_and_query() -> None:
    """Publisher AMP mirrors collapse onto the non-AMP article path."""
    assert (
        canonical_url("https://news.example/world/story/amp")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/amp.html")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/amp/world/story")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/story?amp=1&id=9")
        == "https://news.example/story?id=9"
    )
    assert (
        canonical_url("https://news.example/story?output=amp&id=9")
        == "https://news.example/story?id=9"
    )
    # Non-AMP output= values must survive.
    assert (
        canonical_url("https://news.example/story?output=json&id=9")
        == "https://news.example/story?id=9&output=json"
    )


def test_canonical_url_normalizes_trailing_slash() -> None:
    """Trailing slash is identity-noise for article paths; root stays ``/``."""
    assert canonical_url("https://news.example/world/1/") == "https://news.example/world/1"
    assert canonical_url("https://news.example/world/1") == "https://news.example/world/1"
    a = canonical_url("https://news.example/a/b/")
    b = canonical_url("https://news.example/a/b")
    assert a == b == "https://news.example/a/b"
    # Empty / root path stays a single slash.
    assert canonical_url("https://news.example") == "https://news.example/"
    assert canonical_url("https://news.example/") == "https://news.example/"


def test_canonical_url_www_and_trailing_slash_compose() -> None:
    """Fetch-gate identity: www + slash + trackers + scheme collapse together."""
    variants = [
        "https://www.reuters.com/world/article-9/",
        "https://reuters.com/world/article-9",
        "http://WWW.reuters.com/world/article-9/?utm_source=rss&fbclid=1",
        "https://m.reuters.com/world/article-9/amp?amp=1",
    ]
    canonicals = {canonical_url(u) for u in variants}
    assert canonicals == {"https://reuters.com/world/article-9"}


def test_canonical_url_strips_print_path_and_query() -> None:
    """Print-view mirrors collapse onto the non-print article path."""
    assert (
        canonical_url("https://news.example/world/story/print")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/print.html")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story?print=1&id=9")
        == "https://news.example/world/story?id=9"
    )
    assert (
        canonical_url("https://news.example/world/story?view=print&id=9")
        == "https://news.example/world/story?id=9"
    )
    assert (
        canonical_url("https://news.example/world/story?output=print&id=9")
        == "https://news.example/world/story?id=9"
    )
    # Non-print view= values must survive.
    assert (
        canonical_url("https://news.example/world/story?view=desktop&id=9")
        == "https://news.example/world/story?id=9&view=desktop"
    )


def test_canonical_url_strips_share_trackers() -> None:
    """YouTube/Twitter/share wrappers must not create a second fetch-gate key."""
    raw = (
        "https://news.example/clip?v=abc"
        "&si=sharetoken&feature=share&via=someone&xtor=RSS-1"
    )
    out = canonical_url(raw)
    assert out == "https://news.example/clip?v=abc"


def test_canonical_url_strips_index_basenames() -> None:
    """CMS default documents are identity-noise for the same article."""
    assert (
        canonical_url("https://news.example/world/story/index.html")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/index.php")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/default.aspx")
        == "https://news.example/world/story"
    )
    # Bare site root index collapses to ``/``.
    assert canonical_url("https://news.example/index.html") == "https://news.example/"


def test_canonical_url_strips_trailing_html_extension() -> None:
    """``.html`` / ``.htm`` article paths collapse onto the extensionless form."""
    assert (
        canonical_url("https://news.example/world/story.html")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story.htm")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story.HTML")
        == "https://news.example/world/story"
    )
    # Extensionless path is already identity.
    assert (
        canonical_url("https://news.example/world/story")
        == "https://news.example/world/story"
    )
    # Composes with host alias + trackers.
    assert (
        canonical_url("https://www.news.example/world/story.html?utm_source=rss")
        == "https://news.example/world/story"
    )
    # Non-html extensions survive (not identity-noise for us).
    assert (
        canonical_url("https://news.example/world/story.json")
        == "https://news.example/world/story.json"
    )


def test_canonical_url_print_share_index_compose() -> None:
    """Print path + index basename + share trackers collapse with host aliases."""
    variants = [
        "https://www.news.example/world/story/index.html",
        "https://m.news.example/world/story/print?print=1&si=x",
        "http://news.example/world/story/?view=print&utm_source=share",
        "https://news.example/world/story",
    ]
    assert {canonical_url(u) for u in variants} == {"https://news.example/world/story"}


def test_canonical_url_strips_embed_comments_share_paths() -> None:
    """CMS embed/comments/share path mirrors collapse onto the article path."""
    assert (
        canonical_url("https://news.example/world/story/embed")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/embed.html")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/comments")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/comment")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/comments/")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/share")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/shared")
        == "https://news.example/world/story"
    )
    # Mid-path token names must not be stripped (``/comments-section/…`` stays).
    assert (
        canonical_url("https://news.example/comments-section/a")
        == "https://news.example/comments-section/a"
    )
    # Bare root embed is not stripped to empty (keep path identity for /embed).
    assert canonical_url("https://news.example/embed") == "https://news.example/embed"


def test_canonical_url_embed_comments_compose_with_aliases() -> None:
    """Embed/comments/share paths compose with host aliases and trackers."""
    variants = [
        "https://www.news.example/world/story/embed?utm_source=rss",
        "https://m.news.example/world/story/comments/",
        "http://news.example/world/story/share?si=x",
        "https://news.example/world/story",
    ]
    assert {canonical_url(u) for u in variants} == {"https://news.example/world/story"}


def test_canonical_url_strips_mobile_lite_app_paths() -> None:
    """Mobile/lite/app path mirrors collapse onto the desktop article path."""
    assert (
        canonical_url("https://news.example/world/story/mobile")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/lite")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/app")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/touch")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/mobile.html")
        == "https://news.example/world/story"
    )
    # Leading mobile segments (``/m/…``, ``/mobile/…``).
    assert (
        canonical_url("https://news.example/m/world/story")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/mobile/world/story")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/lite/world/story")
        == "https://news.example/world/story"
    )
    # Bare root markers are kept (not rewritten to empty).
    assert canonical_url("https://news.example/mobile") == "https://news.example/mobile"
    assert canonical_url("https://news.example/m") == "https://news.example/m"
    # Mid-path token names must not be stripped.
    assert (
        canonical_url("https://news.example/mobile-apps/a")
        == "https://news.example/mobile-apps/a"
    )


def test_canonical_url_drops_first_page_query() -> None:
    """``page=1`` / ``p=1`` are identity-noise; later pages stay distinct."""
    assert (
        canonical_url("https://news.example/world/story?page=1")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story?p=1&id=9")
        == "https://news.example/world/story?id=9"
    )
    assert (
        canonical_url("https://news.example/world/story?pg=01&id=9")
        == "https://news.example/world/story?id=9"
    )
    assert (
        canonical_url("https://news.example/world/story?paged=1")
        == "https://news.example/world/story"
    )
    # page=2+ is a different document slice — keep it.
    assert (
        canonical_url("https://news.example/world/story?page=2")
        == "https://news.example/world/story?page=2"
    )
    assert (
        canonical_url("https://news.example/world/story?p=3&id=9")
        == "https://news.example/world/story?id=9&p=3"
    )


def test_canonical_url_mobile_page_compose_with_aliases() -> None:
    """Mobile path + first-page query compose with host aliases and trackers."""
    variants = [
        "https://www.news.example/m/world/story?page=1&utm_source=rss",
        "https://m.news.example/world/story/mobile?p=1",
        "http://news.example/mobile/world/story/?pg=1&si=x",
        "https://news.example/world/story",
    ]
    assert {canonical_url(u) for u in variants} == {"https://news.example/world/story"}


def test_domain_of_returns_etld_plus_one() -> None:
    assert domain_of("https://news.bbc.co.uk/x") == "bbc.co.uk"
    assert domain_of("https://example.com/y") == "example.com"
    assert domain_of("ftp://anything") is None or domain_of("ftp://anything") == "anything"


def test_is_http_url() -> None:
    assert is_http_url("https://x.test")
    assert is_http_url("http://x.test")
    assert not is_http_url("ftp://x.test")
    assert not is_http_url("")


def test_is_public_http_url_rejects_internal_ip_literals() -> None:
    assert not is_public_http_url("http://127.0.0.1:8000/admin")
    assert not is_public_http_url("http://[::1]:8000/admin")
    assert not is_public_http_url("http://169.254.169.254/latest/meta-data/")
    assert not is_public_http_url("http://10.0.0.5/admin")
    assert not is_public_http_url("http://192.168.1.10/admin")


def test_is_public_http_url_rejects_dns_names_that_resolve_private(monkeypatch) -> None:
    def fake_getaddrinfo(host: str, port: int | None, *, type: socket.SocketKind) -> list[tuple]:
        assert host == "attacker.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert not is_public_http_url("https://attacker.example/news")


def test_is_public_http_url_allows_dns_names_that_resolve_public(monkeypatch) -> None:
    def fake_getaddrinfo(host: str, port: int | None, *, type: socket.SocketKind) -> list[tuple]:
        assert host == "news.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert is_public_http_url("https://news.example/article")


def test_is_homepage_url() -> None:
    assert is_homepage_url("https://example.com")
    assert is_homepage_url("https://example.com/")
    assert is_homepage_url("http://news.example.org/")
    assert not is_homepage_url("https://example.com/feed.xml")
    assert not is_homepage_url("https://example.com/?utm=1")
    assert not is_homepage_url("https://example.com/#top")
    assert not is_homepage_url("ftp://example.com/")
    assert not is_homepage_url("")
    assert not is_homepage_url(None)
