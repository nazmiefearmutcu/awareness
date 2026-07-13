"""URL canonicalization tests."""

import socket

from urllib.parse import urlsplit

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


def test_canonical_url_unwraps_amp_cdn() -> None:
    """Google AMP Cache hosts collapse onto the origin article identity."""
    # Content cache, secure origin (/c/s/).
    assert (
        canonical_url("https://cdn.ampproject.org/c/s/www.news.example/world/story")
        == "https://news.example/world/story"
    )
    # Content cache, insecure origin marker (/c/ → still https identity).
    assert (
        canonical_url("https://cdn.ampproject.org/c/www.news.example/world/story")
        == "https://news.example/world/story"
    )
    # Viewer cache variants (/v/s/, /v/).
    assert (
        canonical_url("https://cdn.ampproject.org/v/s/news.example/world/story")
        == "https://news.example/world/story"
    )
    # Publisher subdomain form on ampproject.org.
    assert (
        canonical_url(
            "https://www-news-example.cdn.ampproject.org/c/s/www.news.example/world/story"
        )
        == "https://news.example/world/story"
    )
    # AMP CDN + AMP path + trackers compose to the same apex article.
    assert (
        canonical_url(
            "https://cdn.ampproject.org/c/s/m.news.example/world/story/amp?utm_source=amp"
        )
        == "https://news.example/world/story"
    )
    # Non-AMP hosts must not be rewritten.
    assert (
        canonical_url("https://cdn.example.org/c/s/news.example/world/story")
        == "https://cdn.example.org/c/s/news.example/world/story"
    )
    # AMP CDN without a recoverable origin path is left as-is (after other rules).
    assert (
        canonical_url("https://cdn.ampproject.org/")
        == "https://cdn.ampproject.org/"
    )


def test_canonical_url_unwraps_bing_and_google_amp_viewers() -> None:
    """Bing / Google AMP viewer hosts collapse onto the origin article identity."""
    # Bing AMP viewer (secure origin /amp/s/).
    assert (
        canonical_url("https://www.bing.com/amp/s/www.news.example/world/story")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://bing.com/amp/s/news.example/world/story")
        == "https://news.example/world/story"
    )
    # Bing without /s/ (http origin marker) still maps to https identity.
    assert (
        canonical_url("https://www.bing.com/amp/www.news.example/world/story")
        == "https://news.example/world/story"
    )
    # Google AMP viewer.
    assert (
        canonical_url("https://www.google.com/amp/s/www.news.example/world/story")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://google.com/amp/s/m.news.example/world/story/amp")
        == "https://news.example/world/story"
    )
    # Viewer + trackers + mobile host compose.
    assert (
        canonical_url(
            "https://www.bing.com/amp/s/www.news.example/world/story?utm_source=bing&srsltid=x"
        )
        == "https://news.example/world/story"
    )
    # Non-viewer host must not be rewritten to a foreign origin host.
    non_viewer = canonical_url("https://cdn.other.test/amp/s/news.example/world/story")
    assert non_viewer is not None
    assert "news.example" not in urlsplit(non_viewer).netloc
    # Bare bing/google roots stay as-is (alias host strip only).
    assert canonical_url("https://www.bing.com/") == "https://bing.com/"
    assert canonical_url("https://www.google.com/") == "https://google.com/"


def test_canonical_url_unwraps_wayback_machine() -> None:
    """web.archive.org /web/<ts>/… wrappers collapse onto the origin article."""
    assert (
        canonical_url(
            "https://web.archive.org/web/20240101120000/https://www.news.example/world/story"
        )
        == "https://news.example/world/story"
    )
    # Timestamp modifier flags (id_, if_) still unwrap.
    assert (
        canonical_url(
            "https://web.archive.org/web/20240101120000id_/http://m.news.example/world/story"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://web.archive.org/web/20240101120000if_/https://news.example/world/story/amp"
        )
        == "https://news.example/world/story"
    )
    # Origin query is kept (minus tracking); wrapper query is dropped.
    assert (
        canonical_url(
            "https://web.archive.org/web/20240101120000/https://news.example/story?id=9&utm_source=wayback"
        )
        == "https://news.example/story?id=9"
    )
    # Nested Wayback must not loop.
    nested = canonical_url(
        "https://web.archive.org/web/20240101120000/https://web.archive.org/web/1/https://news.example/x"
    )
    assert nested is not None
    assert "web.archive.org" in nested  # refused unwrap → stays on archive host
    # Bare archive root is not rewritten.
    assert canonical_url("https://web.archive.org/") == "https://web.archive.org/"
    # Non-archive host with /web/ path is left alone.
    assert (
        canonical_url("https://other.example/web/20240101120000/https://news.example/x")
        == "https://other.example/web/20240101120000/https://news.example/x"
    )


def test_canonical_url_unwraps_google_translate() -> None:
    """translate.google.com?u=… wrappers collapse onto the origin article."""
    assert (
        canonical_url(
            "https://translate.google.com/translate?sl=auto&tl=en&u=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://translate.googleusercontent.com/translate_c?u=http%3A%2F%2Fm.news.example%2Fstory%2Famp&depth=1"
        )
        == "https://news.example/story"
    )
    # Origin query retained; translate noise params dropped with the wrapper.
    assert (
        canonical_url(
            "https://translate.google.com/translate?u=https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dx"
        )
        == "https://news.example/story?id=9"
    )
    # Missing u= → no unwrap (translate host identity only).
    bare = canonical_url("https://translate.google.com/translate?sl=en&tl=tr")
    assert bare is not None
    assert "translate.google.com" in bare


def test_canonical_url_unwraps_facebook_click_redirect() -> None:
    """l(m).facebook.com/l.php?u=… click wrappers collapse onto the origin."""
    assert (
        canonical_url(
            "https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://lm.facebook.com/l.php?u=http%3A%2F%2Fm.news.example%2Fstory%2Famp&h=AT123"
        )
        == "https://news.example/story"
    )
    # Origin query retained; Facebook wrapper params dropped.
    assert (
        canonical_url(
            "https://l.facebook.com/l.php?u=https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dfb"
        )
        == "https://news.example/story?id=9"
    )
    # Missing u= → no unwrap.
    bare = canonical_url("https://l.facebook.com/l.php?h=AT")
    assert bare is not None
    assert "l.facebook.com" in bare
    # Nested facebook redirect must not loop.
    nested = canonical_url(
        "https://l.facebook.com/l.php?u=https%3A%2F%2Fl.facebook.com%2Fl.php%3Fu%3Dx"
    )
    assert nested is not None
    assert "l.facebook.com" in nested


def test_canonical_url_unwraps_outlook_safelinks() -> None:
    """Outlook Safe Links ``?url=…`` wrappers collapse onto the origin."""
    assert (
        canonical_url(
            "https://nam01.safelinks.protection.outlook.com/?url="
            "https%3A%2F%2Fwww.news.example%2Fworld%2Fstory&data=05%7C02"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://safelinks.protection.outlook.com/?url="
            "http%3A%2F%2Fm.news.example%2Fstory%2Famp&data=x"
        )
        == "https://news.example/story"
    )
    # Origin query retained; Safe Links tracking params dropped.
    assert (
        canonical_url(
            "https://nam12.safelinks.protection.outlook.com/?url="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Demail&data=y"
        )
        == "https://news.example/story?id=9"
    )
    # Missing url= → no unwrap.
    bare = canonical_url(
        "https://nam01.safelinks.protection.outlook.com/?data=only"
    )
    assert bare is not None
    assert "safelinks.protection.outlook.com" in bare
    # Nested Safe Links must not loop.
    nested = canonical_url(
        "https://nam01.safelinks.protection.outlook.com/?url="
        "https%3A%2F%2Fnam02.safelinks.protection.outlook.com%2F%3Furl%3Dx"
    )
    assert nested is not None
    assert "safelinks.protection.outlook.com" in nested


def test_canonical_url_unwraps_duckduckgo_redirect() -> None:
    """duckduckgo.com/l/?uddg=… click wrappers collapse onto the origin."""
    assert (
        canonical_url(
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://www.duckduckgo.com/l/?kh=-1&uddg="
            "http%3A%2F%2Fm.news.example%2Fstory%2Famp"
        )
        == "https://news.example/story"
    )
    # Origin query retained; DDG wrapper params dropped.
    assert (
        canonical_url(
            "https://duckduckgo.com/l/?uddg="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dddg"
        )
        == "https://news.example/story?id=9"
    )
    # Missing uddg= → no unwrap.
    bare = canonical_url("https://duckduckgo.com/l/?kh=-1")
    assert bare is not None
    assert "duckduckgo.com" in bare
    # / search path is not a redirect.
    searchish = canonical_url("https://duckduckgo.com/?q=bitcoin")
    assert searchish is not None
    assert "duckduckgo.com" in searchish
    # Nested DDG redirect must not loop.
    nested = canonical_url(
        "https://duckduckgo.com/l/?uddg="
        "https%3A%2F%2Fduckduckgo.com%2Fl%2F%3Fuddg%3Dx"
    )
    assert nested is not None
    assert "duckduckgo.com" in nested


def test_canonical_url_unwraps_instagram_click_redirect() -> None:
    """l.instagram.com/?u=… click wrappers collapse onto the origin."""
    assert (
        canonical_url(
            "https://l.instagram.com/?u=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://www.l.instagram.com/?u="
            "http%3A%2F%2Fm.news.example%2Fstory%2Famp&e=AT123"
        )
        == "https://news.example/story"
    )
    # Origin query retained; Instagram wrapper params dropped.
    assert (
        canonical_url(
            "https://l.instagram.com/?u="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dig"
            "&e=AT&s=1"
        )
        == "https://news.example/story?id=9"
    )
    # Missing u= → no unwrap.
    bare = canonical_url("https://l.instagram.com/?e=AT")
    assert bare is not None
    assert "l.instagram.com" in bare
    # Nested Instagram redirect must not loop.
    nested = canonical_url(
        "https://l.instagram.com/?u="
        "https%3A%2F%2Fl.instagram.com%2F%3Fu%3Dx"
    )
    assert nested is not None
    assert "l.instagram.com" in nested


def test_canonical_url_unwraps_linkedin_safety_redirect() -> None:
    """linkedin.com/safety/go?url=… (and redir/redirect) collapse onto origin."""
    assert (
        canonical_url(
            "https://www.linkedin.com/safety/go?url="
            "https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://linkedin.com/redir/redirect?url="
            "http%3A%2F%2Fm.news.example%2Fstory%2Famp&trk=public"
        )
        == "https://news.example/story"
    )
    # Origin query retained; LinkedIn wrapper params dropped.
    assert (
        canonical_url(
            "https://www.linkedin.com/safety/go?url="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dli"
            "&trk=flagship"
        )
        == "https://news.example/story?id=9"
    )
    # Missing url= → no unwrap.
    bare = canonical_url("https://www.linkedin.com/safety/go?trk=x")
    assert bare is not None
    assert "linkedin.com" in bare
    # Ordinary LinkedIn profile path is not a redirect.
    profile = canonical_url("https://www.linkedin.com/in/someone")
    assert profile is not None
    assert "linkedin.com/in/someone" in profile
    # Nested LinkedIn redirect must not loop.
    nested = canonical_url(
        "https://www.linkedin.com/safety/go?url="
        "https%3A%2F%2Fwww.linkedin.com%2Fsafety%2Fgo%3Furl%3Dx"
    )
    assert nested is not None
    assert "linkedin.com" in nested


def test_canonical_url_unwraps_google_url_redirect() -> None:
    """google.com/url?url=… (or q= absolute URL) collapses onto the origin."""
    assert (
        canonical_url(
            "https://www.google.com/url?url=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://google.com/url?q=https%3A%2F%2Fm.news.example%2Fstory%2Famp&sa=U"
        )
        == "https://news.example/story"
    )
    # Prefer url= over free-text q= when both present.
    assert (
        canonical_url(
            "https://www.google.com/url?q=bitcoin+price&url=https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9"
        )
        == "https://news.example/story?id=9"
    )
    # Free-text q= alone is not an origin → no unwrap.
    searchish = canonical_url("https://www.google.com/url?q=bitcoin+price&sa=U")
    assert searchish is not None
    assert "google.com" in searchish
    # /search?q= is not a redirect path.
    assert (
        canonical_url("https://www.google.com/search?q=https%3A%2F%2Fnews.example%2Fx")
        == "https://google.com/search?q=https%3A%2F%2Fnews.example%2Fx"
    )


def test_canonical_url_unwraps_reddit_outbound() -> None:
    """out.reddit.com/?url=… outbound wrappers collapse onto the origin."""
    assert (
        canonical_url(
            "https://out.reddit.com/123?url=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://www.out.reddit.com/?url="
            "http%3A%2F%2Fm.news.example%2Fstory%2Famp&token=abc"
        )
        == "https://news.example/story"
    )
    # Origin query retained; Reddit wrapper params dropped.
    assert (
        canonical_url(
            "https://out.reddit.com/?url="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dreddit"
            "&token=x&app_name=web"
        )
        == "https://news.example/story?id=9"
    )
    # Missing url= → no unwrap.
    bare = canonical_url("https://out.reddit.com/?token=x")
    assert bare is not None
    assert "out.reddit.com" in bare
    # Ordinary reddit.com post path is not an outbound redirect.
    post = canonical_url("https://www.reddit.com/r/news/comments/abc/story/")
    assert post is not None
    assert "reddit.com/r/news" in post
    # Nested Reddit outbound must not loop.
    nested = canonical_url(
        "https://out.reddit.com/?url="
        "https%3A%2F%2Fout.reddit.com%2F%3Furl%3Dx"
    )
    assert nested is not None
    assert "out.reddit.com" in nested


def test_canonical_url_unwraps_youtube_redirect() -> None:
    """youtube.com/redirect?q=… external-link wrappers collapse onto the origin."""
    assert (
        canonical_url(
            "https://www.youtube.com/redirect?q=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://m.youtube.com/redirect?event=video_description&q="
            "http%3A%2F%2Fm.news.example%2Fstory%2Famp"
        )
        == "https://news.example/story"
    )
    # Origin query retained; YouTube wrapper params dropped.
    assert (
        canonical_url(
            "https://youtube.com/redirect?event=video_description&redir_token=x&q="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dyt"
        )
        == "https://news.example/story?id=9"
    )
    # Free-text q= (not an absolute URL) → no unwrap.
    free = canonical_url("https://www.youtube.com/redirect?q=bitcoin+price")
    assert free is not None
    assert "youtube.com" in free
    # Ordinary watch path is not a redirect.
    watch = canonical_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert watch is not None
    assert "youtube.com/watch" in watch
    # Nested YouTube redirect must not loop.
    nested = canonical_url(
        "https://www.youtube.com/redirect?q="
        "https%3A%2F%2Fwww.youtube.com%2Fredirect%3Fq%3Dx"
    )
    assert nested is not None
    assert "youtube.com" in nested


def test_canonical_url_unwraps_slack_redirect() -> None:
    """slack-redir.net/link?url=… wrappers collapse onto the origin article."""
    assert (
        canonical_url(
            "https://slack-redir.net/link?url=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://www.slack-redir.net/link?url="
            "http%3A%2F%2Fm.news.example%2Fstory%2Famp"
        )
        == "https://news.example/story"
    )
    # Origin query retained; Slack wrapper params dropped.
    assert (
        canonical_url(
            "https://slack-redir.net/link?url="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dslack"
            "&ssb_ver=3"
        )
        == "https://news.example/story?id=9"
    )
    # Missing url= → no unwrap.
    bare = canonical_url("https://slack-redir.net/link?ssb_ver=3")
    assert bare is not None
    assert "slack-redir.net" in bare
    # Non-link path is not a redirector.
    other = canonical_url("https://slack-redir.net/other?url=https%3A%2F%2Fx.test%2Fa")
    assert other is not None
    assert "slack-redir.net" in other
    # Nested Slack redirect must not loop.
    nested = canonical_url(
        "https://slack-redir.net/link?url="
        "https%3A%2F%2Fslack-redir.net%2Flink%3Furl%3Dx"
    )
    assert nested is not None
    assert "slack-redir.net" in nested


def test_canonical_url_unwraps_whatsapp_redirect() -> None:
    """l.wl.co WhatsApp click wrappers collapse onto the origin article."""
    assert (
        canonical_url(
            "https://l.wl.co/l?u=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://l.wl.co/?u=http%3A%2F%2Fm.news.example%2Fstory%2Famp"
        )
        == "https://news.example/story"
    )
    # Origin query retained; WhatsApp wrapper params dropped.
    assert (
        canonical_url(
            "https://l.wl.co/l?u="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dwa"
            "&e=ATxyz"
        )
        == "https://news.example/story?id=9"
    )
    # Missing u= → no unwrap.
    bare = canonical_url("https://l.wl.co/l?e=ATxyz")
    assert bare is not None
    assert "l.wl.co" in bare
    # Nested WhatsApp redirect must not loop.
    nested = canonical_url(
        "https://l.wl.co/l?u="
        "https%3A%2F%2Fl.wl.co%2Fl%3Fu%3Dx"
    )
    assert nested is not None
    assert "l.wl.co" in nested
    # Ordinary chat.whatsapp.com invite is not a click wrapper.
    invite = canonical_url("https://chat.whatsapp.com/AbCdEfGhIjK")
    assert invite is not None
    assert "chat.whatsapp.com" in invite


def test_canonical_url_unwraps_telegram_share() -> None:
    """t.me / telegram.me share and Instant View wrappers collapse to origin."""
    assert (
        canonical_url(
            "https://t.me/share/url?url=https%3A%2F%2Fwww.news.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    assert (
        canonical_url(
            "https://telegram.me/share/url?url=http%3A%2F%2Fm.news.example%2Fstory%2Famp"
        )
        == "https://news.example/story"
    )
    # Instant View path.
    assert (
        canonical_url(
            "https://t.me/iv?url=https%3A%2F%2Fnews.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    # Origin query retained; Telegram wrapper text= dropped.
    assert (
        canonical_url(
            "https://t.me/share/url?url="
            "https%3A%2F%2Fnews.example%2Fstory%3Fid%3D9%26utm_source%3Dtg"
            "&text=Check%20this"
        )
        == "https://news.example/story?id=9"
    )
    # Missing url= → no unwrap.
    bare = canonical_url("https://t.me/share/url?text=hello")
    assert bare is not None
    assert "t.me" in bare
    # Ordinary channel post is not a share wrapper.
    channel = canonical_url("https://t.me/somechannel/12345")
    assert channel is not None
    assert "t.me" in channel
    # Nested Telegram share must not loop.
    nested = canonical_url(
        "https://t.me/share/url?url="
        "https%3A%2F%2Ft.me%2Fshare%2Furl%3Furl%3Dx"
    )
    assert nested is not None
    assert "t.me" in nested


def test_canonical_url_unwraps_href_li() -> None:
    """href.li privacy wrappers collapse onto the origin article."""
    assert (
        canonical_url("https://href.li/?https://www.news.example/world/story")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://href.li/?http://m.news.example/story/amp")
        == "https://news.example/story"
    )
    # Percent-encoded origin in the raw query.
    assert (
        canonical_url(
            "https://href.li/?https%3A%2F%2Fnews.example%2Fworld%2Fstory"
        )
        == "https://news.example/world/story"
    )
    # Origin query retained inside the raw href.li query.
    assert (
        canonical_url(
            "https://href.li/?https://news.example/story?id=9&utm_source=href"
        )
        == "https://news.example/story?id=9"
    )
    # Defensive url= form.
    assert (
        canonical_url(
            "https://href.li/?url=https%3A%2F%2Fnews.example%2Fstory"
        )
        == "https://news.example/story"
    )
    # Non-URL query → no unwrap.
    bare = canonical_url("https://href.li/?not-a-url")
    assert bare is not None
    assert "href.li" in bare
    # Nested href.li must not loop.
    nested = canonical_url("https://href.li/?https://href.li/?https://x.test/a")
    assert nested is not None
    # After refuse_hosts on nested href.li origin, wrapper stays or only first hop
    # peels when outer validates; nested origin host is href.li so refused.
    assert "href.li" in nested or nested == "https://x.test/a"


def test_canonical_url_strips_search_and_cms_noise() -> None:
    """Google/MSN search wrappers and CMS feed/trackback paths are identity-noise."""
    assert (
        canonical_url("https://news.example/world/story?srsltid=Abc123&id=9")
        == "https://news.example/world/story?id=9"
    )
    assert (
        canonical_url("https://news.example/world/story?ocid=msft&id=9")
        == "https://news.example/world/story?id=9"
    )
    assert (
        canonical_url("https://news.example/world/story?replytocom=42")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story?preview=true&id=9")
        == "https://news.example/world/story?id=9"
    )
    # Non-true preview values survive (custom CMS modes).
    assert (
        canonical_url("https://news.example/world/story?preview=draft&id=9")
        == "https://news.example/world/story?id=9&preview=draft"
    )
    assert (
        canonical_url("https://news.example/world/story/trackback")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/feed")
        == "https://news.example/world/story"
    )
    assert (
        canonical_url("https://news.example/world/story/atom")
        == "https://news.example/world/story"
    )
    # Bare root /feed is kept (site feed is a real resource).
    assert canonical_url("https://news.example/feed") == "https://news.example/feed"
    assert (
        canonical_url("https://news.example/trackback")
        == "https://news.example/trackback"
    )


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
