"""URL canonicalization and identity helpers."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

# Tracking params that should be stripped during canonicalization.
# Any query key matching these names (case-insensitive) or starting with
# ``utm_`` is removed so the same article under different campaign wrappers
# collapses to one identity for the fetch gate / dedup keys.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "gclid",
        "gclsrc",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "twclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "_hsenc",
        "_hsmi",
        "_openstat",
        "li_fat_id",
        "s_kwcid",
        "ncid",
        "icid",
        "pk_campaign",
        "pk_kwd",
        "pk_source",
        "pk_medium",
        "mtm_campaign",
        "mtm_kwd",
        "mtm_source",
        "mtm_medium",
        "ref",
        "ref_src",
        "ref_url",
        "referrer",
        "share",
        "trk",
        "spm",
        # AMP / mobile-share wrappers that do not change article identity.
        "amp",
        "amp_js_v",
        "amp_gsa",
        "usqp",
        "output",  # often output=amp
        # Share / social / newsletter click wrappers (YouTube, Twitter, etc.).
        "si",  # YouTube share identity
        "feature",  # often feature=share on YT / CMS
        "via",  # Twitter via=
        "sref",
        "sr_share",
        "cmpid",
        "s_cid",
        "xtor",
        # Print-view wrappers (value-gated below when ambiguous).
        "print",
        "printable",
        "printview",
        # Google organic / SERP and MSN wrappers that do not change article identity.
        "srsltid",
        "ocid",
        "replytocom",
    }
)

# Host labels that are aliases of the apex host for the same article.
# Applied repeatedly so ``www.m.example.com`` → ``example.com``.
_ALIAS_HOST_PREFIXES: tuple[str, ...] = ("www.", "m.", "mobile.", "amp.")

# Path suffixes that mark Google AMP / publisher AMP mirrors of the same doc.
_AMP_PATH_SUFFIXES: tuple[str, ...] = (
    "/amp",
    "/amp.html",
    "/amp.htm",
    "/index.amp",
    "/index.amp.html",
)

# Path suffixes that mark print-view mirrors of the same article.
_PRINT_PATH_SUFFIXES: tuple[str, ...] = (
    "/print",
    "/print.html",
    "/print.htm",
)

# Path suffixes that mark embed / comments / share CMS mirrors of the same article.
# Applied after print so ``/story/print`` still wins when both appear; only
# trailing whole-segment markers are stripped (not mid-path tokens like
# ``/comments-section/…``). Bare ``/embed`` (root-only) is kept so a site
# whose homepage is literally ``/embed`` is not rewritten to empty.
_EMBED_COMMENTS_SHARE_PATH_SUFFIXES: tuple[str, ...] = (
    "/embed",
    "/embed.html",
    "/embed.htm",
    "/comments",
    "/comment",
    "/share",
    "/shared",
)

# WordPress / CMS article appendages that are not the article body itself.
# Require a parent segment so bare ``/feed`` (site RSS) stays distinct.
_CMS_FEED_TRACKBACK_PATH_SUFFIXES: tuple[str, ...] = (
    "/trackback",
    "/feed",
    "/atom",
    "/rdf",
    "/feed.xml",
    "/atom.xml",
    "/index.rdf",
)

# Search-engine AMP *viewer* hosts (not publisher AMP paths). Path embeds the
# origin as ``/amp/s/<host>/<path>`` (https) or ``/amp/<host>/<path>`` (http).
_VIEWER_AMP_HOSTS: frozenset[str] = frozenset(
    {
        "bing.com",
        "google.com",
    }
)

# Wayback Machine hosts that wrap an origin URL in ``/web/<timestamp…>/<origin>``.
_WAYBACK_HOSTS: frozenset[str] = frozenset(
    {
        "web.archive.org",
        "wayback.archive.org",
    }
)

# Google Translate wrapper hosts that put the origin in a ``u=`` query param.
_TRANSLATE_HOSTS: frozenset[str] = frozenset(
    {
        "translate.google.com",
        "translate.googleusercontent.com",
    }
)

# Facebook click-through hosts that embed the origin in a ``u=`` query param
# (``l.facebook.com/l.php?u=…``, ``lm.facebook.com/l.php?u=…``).
_FACEBOOK_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "l.facebook.com",
        "lm.facebook.com",
        "l.facebook.net",
        "lm.facebook.net",
    }
)

# Google web-search redirect hosts (``google.com/url?url=…`` / ``?q=…``).
# Matched after stripping a single leading ``www.`` / country TLD is NOT
# expanded here — only the common ``google.com`` apex (plus www).
_GOOGLE_URL_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "google.com",
    }
)

# DuckDuckGo click-through host (``duckduckgo.com/l/?uddg=…``).
_DUCKDUCKGO_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "duckduckgo.com",
    }
)

# Instagram click-through host (``l.instagram.com/?u=…``).
_INSTAGRAM_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "l.instagram.com",
    }
)

# LinkedIn external-link warning / redir hosts
# (``linkedin.com/safety/go?url=…``, ``linkedin.com/redir/redirect?url=…``).
_LINKEDIN_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com",
    }
)
_LINKEDIN_REDIRECT_PATHS: frozenset[str] = frozenset(
    {
        "/safety/go",
        "/redir/redirect",
    }
)

# Reddit outbound click-through hosts (``out.reddit.com/…?url=…``).
_REDDIT_OUTBOUND_HOSTS: frozenset[str] = frozenset(
    {
        "out.reddit.com",
    }
)

# YouTube external-link redirect hosts (``youtube.com/redirect?q=…``).
# ``m.`` kept explicitly — unwrap runs before alias-host stripping.
_YOUTUBE_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "m.youtube.com",
        "youtube-nocookie.com",
    }
)

# Slack outbound click-through hosts (``slack-redir.net/link?url=…``).
_SLACK_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "slack-redir.net",
    }
)
_SLACK_REDIRECT_PATHS: frozenset[str] = frozenset(
    {
        "/link",
    }
)

# WhatsApp click-through hosts (``l.wl.co/?u=…`` / ``l.wl.co/l?u=…``).
_WHATSAPP_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "l.wl.co",
    }
)

# Telegram share / Instant View hosts that embed the origin in a ``url=`` param
# (``t.me/share/url?url=…``, ``t.me/iv?url=…``, ``telegram.me/…``).
_TELEGRAM_SHARE_HOSTS: frozenset[str] = frozenset(
    {
        "t.me",
        "telegram.me",
        "telegram.dog",
    }
)
_TELEGRAM_SHARE_PATHS: frozenset[str] = frozenset(
    {
        "/share/url",
        "/iv",
    }
)

# href.li privacy/outbound wrapper (``href.li/?https://origin…`` — origin is the
# raw query string, not a key=value pair).
_HREFLI_HOSTS: frozenset[str] = frozenset(
    {
        "href.li",
    }
)

# Tumblr outbound click-through hosts (``t.umblr.com/redirect?z=…``).
_TUMBLR_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "t.umblr.com",
    }
)
_TUMBLR_REDIRECT_PATHS: frozenset[str] = frozenset(
    {
        "/redirect",
    }
)

# Pocket save/share redirect hosts (``getpocket.com/redirect?url=…``).
_POCKET_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "getpocket.com",
    }
)
_POCKET_REDIRECT_PATHS: frozenset[str] = frozenset(
    {
        "/redirect",
    }
)

# Pinterest pin-create / offsite share hosts that embed origin in ``url=``.
_PINTEREST_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "pinterest.com",
        "pinterest.co.uk",
        "pinterest.de",
        "pinterest.fr",
        "pinterest.ca",
        "pinterest.jp",
        "pinterest.com.au",
    }
)
_PINTEREST_REDIRECT_PATH_PREFIXES: tuple[str, ...] = (
    "/pin/create/",
    "/offsite",
)

# Flipboard share bookmarklet hosts (``share.flipboard.com/…?url=…``).
_FLIPBOARD_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "share.flipboard.com",
        "flipboard.com",
    }
)
_FLIPBOARD_REDIRECT_PATH_PREFIXES: tuple[str, ...] = (
    "/bookmarklet/",
    "/share",
)

# Buffer compose / add share hosts that embed origin in ``url=``.
_BUFFER_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "buffer.com",
        "bufferapp.com",
        "publish.buffer.com",
    }
)
_BUFFER_REDIRECT_PATHS: frozenset[str] = frozenset(
    {
        "/add",
        "/compose",
    }
)

# Medium external-link interstitial hosts (``medium.com/m/global/external-link?url=…``).
_MEDIUM_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "medium.com",
        "link.medium.com",
    }
)
_MEDIUM_REDIRECT_PATHS: frozenset[str] = frozenset(
    {
        "/m/global/external-link",
        "/global/external-link",
        "/external-link",
        "/redirect",
    }
)

# Suffix for Microsoft Outlook Safe Links rewrite hosts
# (``nam01.safelinks.protection.outlook.com``, ``*.safelinks.protection.outlook.com``).
_OUTLOOK_SAFELINKS_SUFFIX = "safelinks.protection.outlook.com"

# Path suffixes that mark mobile / lite / app CMS mirrors of the same article.
# Applied after embed/comments so those wrappers still win when stacked;
# only trailing whole-segment markers are stripped. Bare ``/mobile`` (root-only)
# is kept so a site whose homepage is literally ``/mobile`` is not emptied.
_MOBILE_LITE_APP_PATH_SUFFIXES: tuple[str, ...] = (
    "/mobile",
    "/mobile.html",
    "/mobile.htm",
    "/lite",
    "/lite.html",
    "/lite.htm",
    "/app",
    "/app.html",
    "/app.htm",
    "/touch",
    "/touch.html",
)

# Leading path segments used by mobile/lite CMS mirrors (like ``/amp/…``).
# ``/m/`` is the common single-letter mobile prefix; require a following
# segment so bare ``/m`` is not rewritten to empty.
_MOBILE_LITE_LEADING_SEGMENTS: tuple[str, ...] = (
    "/m/",
    "/mobile/",
    "/lite/",
    "/app/",
    "/touch/",
)

# First-page pagination query keys: ``page=1`` / ``p=1`` are identity-noise
# (same document as omitting the param). Higher pages (page=2+) stay.
_FIRST_PAGE_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "page",
        "p",
        "pg",
        "paged",
        "pagina",
        "page_num",
        "pagenum",
    }
)

# Default document names that are identity-noise for CMS article paths.
_INDEX_BASENAMES: tuple[str, ...] = (
    "/index.html",
    "/index.htm",
    "/index.php",
    "/index.aspx",
    "/default.html",
    "/default.htm",
    "/default.aspx",
)


_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _is_tracking_param(key: str) -> bool:
    kl = key.lower()
    return kl in _TRACKING_PARAMS or kl.startswith("utm_")


def _is_noise_query_pair(key: str, value: str) -> bool:
    """True for tracking keys or AMP/print-only query values.

    Value-gated keys keep non-wrapper uses of the same name (e.g. ``output=json``,
    ``feature=embed`` when it is not a pure share/print flag is still stripped
    for ``feature`` because CMS share wrappers dominate in news crawls).
    """
    kl = key.lower()
    val = value.strip().lower()
    # First-page pagination is identity-noise (``?page=1`` ≡ no page param).
    # Keep page=2+ so multi-page articles retain distinct fetch-gate keys.
    if kl in _FIRST_PAGE_QUERY_KEYS:
        return val in ("", "1", "01")
    if not _is_tracking_param(key):
        # ``view=print`` / ``display=print`` appear without being in the key set.
        if kl in ("view", "display", "mode", "format"):
            return val in (
                "print",
                "printable",
                "printview",
                "amp",
                "amphtml",
            )
        # ``preview=true`` is CMS identity-noise; ``preview=draft`` may be real.
        if kl == "preview":
            return val in ("1", "true", "yes", "on")
        return False
    # ``output`` is only noise when it marks AMP/print; keep other values.
    if kl == "output":
        return val in (
            "amp",
            "amphtml",
            "htmlamp",
            "print",
            "printable",
        )
    # Boolean-ish print flags: print=1 / printable=true — always strip the key
    # when listed in _TRACKING_PARAMS (print / printable / printview).
    return True


def _strip_alias_host(netloc: str) -> str:
    """Strip leading www/m/mobile/amp labels from host (preserve userinfo / port).

    News publishers re-host the same article under mobile and AMP subdomains;
    the fetch gate and doc identity must collapse those to the apex host.

    Only the first label is stripped when the *remainder* is still a real
    multi-label host (≥2 labels — a dot in non-TLD position), so real domains
    like ``www.com`` / ``m.me`` are never collapsed to ``com`` / ``me`` (M-27).
    """
    if "@" in netloc:
        userinfo, _, hostport = netloc.rpartition("@")
        return f"{userinfo}@{_strip_alias_host(hostport)}"
    # IPv6 literals are bracketed; never treat them as alias hosts.
    if netloc.startswith("["):
        return netloc
    # Split port so we only rewrite the hostname labels.
    host, sep, port = netloc.partition(":")
    changed = True
    while changed:
        changed = False
        for prefix in _ALIAS_HOST_PREFIXES:
            if host.startswith(prefix) and len(host) > len(prefix):
                rest = host[len(prefix) :]
                # Require ≥2 labels in the remainder (a dot in non-TLD
                # position) so real domains like ``www.com`` / ``m.me`` are
                # never collapsed to ``com`` / ``me`` (M-27).
                if "." in rest and len(rest) > 1 and "." in rest.rstrip("."):
                    host = rest
                    changed = True
                break
    return f"{host}{sep}{port}" if sep else host


# Back-compat alias used by older call sites / tests that imported the name.
_strip_www_host = _strip_alias_host


def _host_without_port_or_userinfo(netloc: str) -> str | None:
    """Lowercased hostname from a netloc, or ``None`` for IPv6 literals."""
    host = netloc.lower()
    if "@" in host:
        _, _, host = host.rpartition("@")
    if host.startswith("["):
        return None
    if ":" in host:
        host, _, _ = host.partition(":")
    return host


def _parse_embedded_origin(rest: str) -> tuple[str, str] | None:
    """Parse ``host`` or ``host/path…`` into ``(netloc, path)`` for AMP viewers."""
    origin_host, sep, origin_path = rest.partition("/")
    if not origin_host or "." not in origin_host:
        return None
    if any(ch in origin_host for ch in (" ", "?", "#", "@")):
        return None
    origin_netloc = origin_host.lower()
    origin_path_out = f"/{origin_path}" if sep else "/"
    return origin_netloc, origin_path_out


def _unwrap_amp_cdn(netloc: str, path: str) -> tuple[str, str] | None:
    """Rewrite Google AMP Cache hosts to the origin article (netloc, path).

    AMP Cache URL forms (https://developers.google.com/amp/cache/overview):

    * ``https://cdn.ampproject.org/c/s/www.example.com/story``
    * ``https://cdn.ampproject.org/c/www.example.com/story`` (http origin)
    * ``https://www-example-com.cdn.ampproject.org/c/s/www.example.com/story``
    * Viewer variants use ``/v/`` or ``/v/s/`` instead of ``/c/`` / ``/c/s/``.

    Returns ``(origin_netloc, origin_path)`` when the input is an AMP Cache
    URL with a recoverable origin; ``None`` otherwise so callers keep the
    original parts. Identity-only — does not change what is fetched.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None  # IPv6 never hosts ampproject CDN
    if not (host == "cdn.ampproject.org" or host.endswith(".cdn.ampproject.org")):
        return None
    # Path prefixes: /c/s/, /c/, /v/s/, /v/ then origin host + origin path.
    lower = (path or "").lower()
    rest: str | None = None
    for prefix in ("/c/s/", "/v/s/", "/c/", "/v/"):
        if lower.startswith(prefix) and len(path) > len(prefix):
            rest = path[len(prefix) :]
            break
    if not rest:
        return None
    return _parse_embedded_origin(rest)


def _unwrap_viewer_amp(netloc: str, path: str) -> tuple[str, str] | None:
    """Rewrite Bing / Google AMP *viewer* URLs to the origin article.

    Forms:

    * ``https://www.bing.com/amp/s/www.example.com/story``
    * ``https://www.bing.com/amp/www.example.com/story`` (http origin marker)
    * ``https://www.google.com/amp/s/www.example.com/story``

    Host matching ignores a single leading ``www.``. Returns ``None`` when the
    host is not a known viewer or the path does not embed a recoverable origin.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    if host.startswith("www.") and len(host) > 4:
        host = host[4:]
    if host not in _VIEWER_AMP_HOSTS:
        return None
    lower = (path or "").lower()
    rest: str | None = None
    # Prefer /amp/s/ (https origin) before bare /amp/ so we do not leave a
    # leading ``s/`` segment as a fake host.
    for prefix in ("/amp/s/", "/amp/"):
        if lower.startswith(prefix) and len(path) > len(prefix):
            rest = path[len(prefix) :]
            break
    if not rest:
        return None
    parsed = _parse_embedded_origin(rest)
    if parsed is None:
        return None
    origin_netloc, origin_path = parsed
    # Refuse to "unwrap" into another viewer host (loop / nonsense).
    origin_host = origin_netloc
    if origin_host.startswith("www.") and len(origin_host) > 4:
        origin_host = origin_host[4:]
    if origin_host in _VIEWER_AMP_HOSTS:
        return None
    return origin_netloc, origin_path


def _strip_www_label(host: str) -> str:
    """Drop a single leading ``www.`` label when present."""
    if host.startswith("www.") and len(host) > 4:
        return host[4:]
    return host


def _unwrap_wayback(netloc: str, path: str, outer_query: str = "") -> tuple[str, str, str] | None:
    """Rewrite Wayback Machine URLs to origin (netloc, path, query).

    Forms (https://archive.org/help/wayback_api.php):

    * ``https://web.archive.org/web/20240101120000/https://www.example.com/story``
    * ``https://web.archive.org/web/20240101120000id_/http://example.com/story``
    * ``https://web.archive.org/web/20240101120000if_/https://example.com/story``

    Timestamp flags (``id_``, ``if_``, ``js_``, ``cs_``, ``im_``, ``oe_``) may
    trail the 14-digit capture id. ``urlsplit`` peels the origin's query off
    the *wrapper* URL (first ``?``), so ``outer_query`` is reattached to the
    embedded origin before parsing. Returns ``None`` when the host is not a
    Wayback host or the path does not embed a recoverable absolute origin URL.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _WAYBACK_HOSTS:
        return None
    # Path: /web/<timestamp[flags]>/<origin-url>
    raw = path or ""
    if not raw.lower().startswith("/web/"):
        return None
    rest = raw[5:]  # after "/web/"
    if not rest:
        return None
    # First segment is the timestamp (+ optional modifier suffix).
    stamp, sep, origin = rest.partition("/")
    if not sep or not origin:
        return None
    # Timestamp is digits, optionally followed by a short alpha flag + underscore.
    if not stamp or not stamp[0].isdigit():
        return None
    # Origin may be scheme-relative or absolute; require an absolute http(s) URL.
    origin = origin.strip()
    if origin.startswith("//"):
        origin = "https:" + origin
    if not origin.lower().startswith(("http://", "https://")):
        return None
    # Reattach outer query: urlsplit took origin's ?… off the full wrapper URL.
    if outer_query:
        origin = f"{origin}&{outer_query}" if "?" in origin else f"{origin}?{outer_query}"
    try:
        op = urlsplit(origin)
    except (ValueError, AttributeError):
        return None
    if not op.scheme or not op.netloc:
        return None
    if op.scheme.lower() not in ("http", "https"):
        return None
    origin_netloc = op.netloc.lower()
    origin_host = _host_without_port_or_userinfo(origin_netloc)
    if origin_host is None:
        return None
    # Refuse to unwrap into another Wayback host (loop).
    if _strip_www_label(origin_host) in _WAYBACK_HOSTS:
        return None
    return origin_netloc, (op.path or "/"), (op.query or "")


def _validate_embedded_origin_url(origin: str, *, refuse_hosts: frozenset[str]) -> str | None:
    """Normalize and validate an embedded origin URL string.

    Accepts absolute http(s) or scheme-relative (``//host/…``) origins.
    Returns the cleaned origin string, or ``None`` when unusable / looping
    into a host listed in ``refuse_hosts`` (after stripping ``www.``).
    """
    origin = (origin or "").strip()
    if not origin:
        return None
    if origin.startswith("//"):
        origin = "https:" + origin
    try:
        op = urlsplit(origin)
    except (ValueError, AttributeError):
        return None
    if op.scheme.lower() not in ("http", "https") or not op.netloc:
        return None
    origin_host = _host_without_port_or_userinfo(op.netloc.lower())
    if origin_host is None:
        return None
    if _strip_www_label(origin_host) in refuse_hosts:
        return None
    return origin


def _query_param(query: str, *keys: str) -> str | None:
    """First non-empty value for any of ``keys`` (case-insensitive key match)."""
    want = {k.lower() for k in keys}
    for k, v in parse_qsl(query or "", keep_blank_values=True):
        if k.lower() in want and v.strip():
            return v.strip()
    return None


def _unwrap_translate(netloc: str, query: str) -> str | None:
    """Extract the origin URL from a Google Translate wrapper ``u=`` param.

    Forms:

    * ``https://translate.google.com/translate?u=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://translate.googleusercontent.com/translate_c?u=…``

    Returns the raw origin URL string when recoverable, else ``None``.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _TRANSLATE_HOSTS:
        return None
    origin = _query_param(query, "u")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_TRANSLATE_HOSTS)


def _unwrap_facebook_redirect(netloc: str, query: str) -> str | None:
    """Extract the origin URL from a Facebook ``l.php?u=…`` click wrapper.

    Forms:

    * ``https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://lm.facebook.com/l.php?u=http%3A%2F%2Fm.example.com%2Fstory``

    Returns the raw origin URL string when recoverable, else ``None``.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _FACEBOOK_REDIRECT_HOSTS:
        return None
    origin = _query_param(query, "u")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_FACEBOOK_REDIRECT_HOSTS)


def _unwrap_google_url_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Google ``/url?url=…`` (or ``q=``) redirect.

    Forms:

    * ``https://www.google.com/url?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://google.com/url?q=https%3A%2F%2Fexample.com%2Fstory&sa=U``

    Only the ``/url`` path is treated as a redirector so ordinary search
    result pages (``/search?q=…``) are not rewritten. Prefer ``url=`` over
    ``q=`` when both are present (``q`` may be a free-text query).
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _GOOGLE_URL_REDIRECT_HOSTS:
        return None
    # Path must be exactly /url (optional trailing slash); ignore /url/other.
    p = (path or "").rstrip("/") or "/"
    if p.lower() != "/url":
        return None
    # Prefer explicit url=; fall back to q= only when it looks like an absolute URL.
    origin = _query_param(query, "url")
    if not origin:
        candidate = _query_param(query, "q")
        if candidate and (
            candidate.lower().startswith(("http://", "https://")) or candidate.startswith("//")
        ):
            origin = candidate
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_GOOGLE_URL_REDIRECT_HOSTS)


def _is_outlook_safelinks_host(host: str) -> bool:
    """True for ``*.safelinks.protection.outlook.com`` (incl. bare suffix)."""
    h = (host or "").lower()
    if not h:
        return False
    return h == _OUTLOOK_SAFELINKS_SUFFIX or h.endswith("." + _OUTLOOK_SAFELINKS_SUFFIX)


def _unwrap_outlook_safelinks(netloc: str, query: str) -> str | None:
    """Extract the origin URL from an Outlook Safe Links ``url=`` wrapper.

    Forms:

    * ``https://nam01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://safelinks.protection.outlook.com/?url=http%3A%2F%2Fm.example.com%2Fx&data=…``

    Nested Safe Links hosts refuse to loop. Returns the raw origin string or
    ``None`` when not a Safe Links host / missing ``url=``.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None or not _is_outlook_safelinks_host(host):
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    # Refuse nested Safe Links hosts (cannot put suffix set in frozenset of
    # exact hosts; validate host after parse).
    origin = (origin or "").strip()
    if not origin:
        return None
    if origin.startswith("//"):
        origin = "https:" + origin
    try:
        op = urlsplit(origin)
    except (ValueError, AttributeError):
        return None
    if op.scheme.lower() not in ("http", "https") or not op.netloc:
        return None
    origin_host = _host_without_port_or_userinfo(op.netloc.lower())
    if origin_host is None or _is_outlook_safelinks_host(origin_host):
        return None
    return origin


def _unwrap_duckduckgo_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a DuckDuckGo ``/l/?uddg=…`` click wrapper.

    Forms:

    * ``https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://www.duckduckgo.com/l/?kh=-1&uddg=http%3A%2F%2Fm.example.com%2Fx``

    Only the ``/l`` path is treated as a redirector so ordinary search pages
    are not rewritten.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _DUCKDUCKGO_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() != "/l":
        return None
    origin = _query_param(query, "uddg")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_DUCKDUCKGO_REDIRECT_HOSTS)


def _unwrap_instagram_redirect(netloc: str, query: str) -> str | None:
    """Extract the origin URL from an Instagram ``l.instagram.com/?u=…`` wrapper.

    Forms:

    * ``https://l.instagram.com/?u=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://l.instagram.com/?u=http%3A%2F%2Fm.example.com%2Fx&e=AT…``

    Any path on the click-through host is treated as a redirector when ``u=``
    is present (IG puts the origin in the query, not a fixed path).
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _INSTAGRAM_REDIRECT_HOSTS:
        return None
    origin = _query_param(query, "u")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_INSTAGRAM_REDIRECT_HOSTS)


def _unwrap_linkedin_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a LinkedIn external-link redirect.

    Forms:

    * ``https://www.linkedin.com/safety/go?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://linkedin.com/redir/redirect?url=http%3A%2F%2Fm.example.com%2Fx``

    Only known redirect paths are rewritten so profiles (``/in/…``), posts,
    and feed URLs stay on LinkedIn identity.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _LINKEDIN_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() not in _LINKEDIN_REDIRECT_PATHS:
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_LINKEDIN_REDIRECT_HOSTS)


def _unwrap_reddit_outbound(netloc: str, query: str) -> str | None:
    """Extract the origin URL from a Reddit ``out.reddit.com/?url=…`` wrapper.

    Forms:

    * ``https://out.reddit.com/…?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://out.reddit.com/?url=http%3A%2F%2Fm.example.com%2Fx&token=…``

    Any path on the outbound host is treated as a redirector when ``url=`` is
    present. Ordinary reddit.com post/comment URLs are not rewritten.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _REDDIT_OUTBOUND_HOSTS:
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_REDDIT_OUTBOUND_HOSTS)


def _unwrap_youtube_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a YouTube ``/redirect?q=…`` external link.

    Forms:

    * ``https://www.youtube.com/redirect?q=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://m.youtube.com/redirect?event=video_description&q=http%3A%2F%2Fm.example.com%2Fx``

    Only the ``/redirect`` path is rewritten so ordinary watch/search URLs stay
    on YouTube identity. Prefer ``q=`` (YouTube's origin param); fall back to
    ``url=`` when present.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _YOUTUBE_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() != "/redirect":
        return None
    # YouTube uses q= for the destination; accept url= as a defensive alias.
    origin = _query_param(query, "q", "url")
    if not origin:
        return None
    # q= may be free text on non-redirect paths; we already gated on /redirect.
    # Still require absolute / scheme-relative so garbage q= values stay put.
    if not (origin.lower().startswith(("http://", "https://")) or origin.startswith("//")):
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_YOUTUBE_REDIRECT_HOSTS)


def _unwrap_slack_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Slack ``slack-redir.net/link?url=…`` wrapper.

    Forms:

    * ``https://slack-redir.net/link?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://www.slack-redir.net/link?url=http%3A%2F%2Fm.example.com%2Fx``

    Only the ``/link`` path is rewritten so other slack-redir routes stay put.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _SLACK_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() not in _SLACK_REDIRECT_PATHS:
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_SLACK_REDIRECT_HOSTS)


def _unwrap_whatsapp_redirect(netloc: str, query: str) -> str | None:
    """Extract the origin URL from a WhatsApp ``l.wl.co/?u=…`` click wrapper.

    Forms:

    * ``https://l.wl.co/l?u=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://l.wl.co/?u=http%3A%2F%2Fm.example.com%2Fx&e=AT…``

    Any path on the click-through host is treated as a redirector when ``u=``
    is present (WhatsApp puts the origin in the query, not a fixed path).
    Ordinary ``chat.whatsapp.com`` invites are not rewritten.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _WHATSAPP_REDIRECT_HOSTS:
        return None
    origin = _query_param(query, "u")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_WHATSAPP_REDIRECT_HOSTS)


def _unwrap_telegram_share(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Telegram share / Instant View wrapper.

    Forms:

    * ``https://t.me/share/url?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://telegram.me/share/url?url=http%3A%2F%2Fm.example.com%2Fx``
    * ``https://t.me/iv?url=https%3A%2F%2Fexample.com%2Fstory`` (Instant View)

    Only share/url and Instant View paths are rewritten so ordinary channel
    posts (``t.me/channel/123``) stay on Telegram identity.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _TELEGRAM_SHARE_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() not in _TELEGRAM_SHARE_PATHS:
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_TELEGRAM_SHARE_HOSTS)


def _unwrap_href_li(netloc: str, query: str) -> str | None:
    """Extract the origin URL from an ``href.li/?<origin>`` privacy wrapper.

    Forms:

    * ``https://href.li/?https://www.example.com/story``
    * ``https://href.li/?https%3A%2F%2Fm.example.com%2Fstory``
    * ``https://href.li/?url=https%3A%2F%2Fexample.com%2Fstory`` (defensive)

    The classic form puts the absolute origin in the *raw* query string (not a
    key=value pair). ``urlsplit`` keeps nested ``?`` of the origin inside the
    query, so article query params survive. Ordinary non-URL queries stay put.
    """
    from urllib.parse import unquote

    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _HREFLI_HOSTS:
        return None
    raw = (query or "").strip()
    if not raw:
        return None
    candidates: list[str] = []
    # Prefer explicit url= when present (defensive alias).
    via_param = _query_param(raw, "url")
    if via_param:
        candidates.append(via_param)
    # Classic form: entire query is the origin (optionally percent-encoded).
    candidates.append(raw)
    if "%" in raw:
        candidates.append(unquote(raw))
    for origin in candidates:
        if not (origin.lower().startswith(("http://", "https://")) or origin.startswith("//")):
            continue
        validated = _validate_embedded_origin_url(origin, refuse_hosts=_HREFLI_HOSTS)
        if validated is not None:
            return validated
    return None


def _unwrap_tumblr_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Tumblr ``t.umblr.com/redirect?z=…`` wrapper.

    Forms:

    * ``https://t.umblr.com/redirect?z=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://t.umblr.com/redirect?z=http%3A%2F%2Fm.example.com%2Fx&t=…``

    Only the ``/redirect`` path is rewritten so ordinary Tumblr blog hosts
    (``blog.tumblr.com``) stay on Tumblr identity. The origin lives in ``z=``.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _TUMBLR_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() not in _TUMBLR_REDIRECT_PATHS:
        return None
    origin = _query_param(query, "z")
    if not origin:
        # Defensive alias used by some clients / older bookmarks.
        origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_TUMBLR_REDIRECT_HOSTS)


def _unwrap_pocket_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Pocket ``getpocket.com/redirect?url=…`` wrapper.

    Forms:

    * ``https://getpocket.com/redirect?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://www.getpocket.com/redirect?url=http%3A%2F%2Fm.example.com%2Fx``

    Only the ``/redirect`` path is rewritten so ordinary Pocket UI paths
    (``/home``, ``/read/…``) stay on Pocket identity.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _POCKET_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() not in _POCKET_REDIRECT_PATHS:
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_POCKET_REDIRECT_HOSTS)


def _unwrap_pinterest_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Pinterest pin-create / offsite ``url=`` wrapper.

    Forms:

    * ``https://www.pinterest.com/pin/create/button/?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://pinterest.com/pin/create/link/?url=http%3A%2F%2Fm.example.com%2Fx``
    * ``https://www.pinterest.com/offsite/?url=https%3A%2F%2Fexample.com%2Fstory``

    Only pin-create and offsite paths are rewritten so ordinary pin/board URLs
    stay on Pinterest identity. The origin lives in ``url=``.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _PINTEREST_REDIRECT_HOSTS:
        return None
    p = (path or "/").lower()
    if not any(p == pref.rstrip("/") or p.startswith(pref) for pref in _PINTEREST_REDIRECT_PATH_PREFIXES):
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_PINTEREST_REDIRECT_HOSTS)


def _unwrap_flipboard_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Flipboard share ``url=`` wrapper.

    Forms:

    * ``https://share.flipboard.com/bookmarklet/popout?v=2&url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://flipboard.com/share?url=http%3A%2F%2Fm.example.com%2Fx``

    Only bookmarklet/share paths are rewritten so magazine/profile pages stay
    on Flipboard identity.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _FLIPBOARD_REDIRECT_HOSTS:
        return None
    p = (path or "/").lower()
    if not any(p == pref.rstrip("/") or p.startswith(pref) for pref in _FLIPBOARD_REDIRECT_PATH_PREFIXES):
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_FLIPBOARD_REDIRECT_HOSTS)


def _unwrap_buffer_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Buffer compose / add ``url=`` share wrapper.

    Forms:

    * ``https://buffer.com/add?url=https%3A%2F%2Fexample.com%2Fstory&text=…``
    * ``https://bufferapp.com/add?url=http%3A%2F%2Fm.example.com%2Fx``
    * ``https://publish.buffer.com/compose?url=https%3A%2F%2Fexample.com%2Fstory``

    Only ``/add`` and ``/compose`` paths are rewritten so ordinary Buffer app
    pages stay on Buffer identity. The origin lives in ``url=``.
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _BUFFER_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() not in _BUFFER_REDIRECT_PATHS:
        return None
    origin = _query_param(query, "url")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_BUFFER_REDIRECT_HOSTS)


def _unwrap_medium_redirect(netloc: str, path: str, query: str) -> str | None:
    """Extract the origin URL from a Medium external-link interstitial.

    Forms:

    * ``https://medium.com/m/global/external-link?url=https%3A%2F%2Fexample.com%2Fstory``
    * ``https://link.medium.com/redirect?url=http%3A%2F%2Fm.example.com%2Fx``
    * ``https://link.medium.com/external-link?url=https%3A%2F%2Fexample.com%2Fstory``

    Only known external-link / redirect paths are rewritten so ordinary Medium
    posts (``/@author/slug``) stay on Medium identity. The origin lives in
    ``url=`` (defensive ``sourceLink=`` alias also accepted).
    """
    host = _host_without_port_or_userinfo(netloc)
    if host is None:
        return None
    host = _strip_www_label(host)
    if host not in _MEDIUM_REDIRECT_HOSTS:
        return None
    p = (path or "").rstrip("/") or "/"
    if p.lower() not in _MEDIUM_REDIRECT_PATHS:
        return None
    origin = _query_param(query, "url", "sourceLink")
    if not origin:
        return None
    return _validate_embedded_origin_url(origin, refuse_hosts=_MEDIUM_REDIRECT_HOSTS)


def _normalize_path(path: str) -> str:
    """Normalize path for identity: empty → ``/``; strip AMP/print/index noise + slash."""
    if not path:
        return "/"
    # Lowercase only for suffix detection; rebuild with original casing
    # collapsed via the trailing-slash rule. Markers are ASCII.
    lower = path.lower()
    for suffix in _AMP_PATH_SUFFIXES:
        if lower.endswith(suffix) and len(path) > len(suffix):
            path = path[: -len(suffix)]
            lower = path.lower()
            break
    # Leading ``/amp/`` segment (``/amp/world/story`` → ``/world/story``).
    if lower.startswith("/amp/") and len(path) > 5:
        path = path[4:]  # drop leading "/amp"
        lower = path.lower()
    # Print-view path mirrors (``/world/story/print`` → ``/world/story``).
    # Also handle trailing slash forms (``/print/`` already in suffix set).
    for suffix in _PRINT_PATH_SUFFIXES:
        s = suffix.rstrip("/")
        if not s:
            continue
        if lower.endswith(s) and len(path) > len(s):
            path = path[: -len(s)]
            lower = path.lower()
            break
        if lower.endswith(s + "/") and len(path) > len(s) + 1:
            path = path[: -(len(s) + 1)]
            lower = path.lower()
            break
    # Embed / comments / share CMS mirrors (``/world/story/embed`` →
    # ``/world/story``). Require a parent segment so bare ``/embed`` is kept.
    for suffix in _EMBED_COMMENTS_SHARE_PATH_SUFFIXES:
        s = suffix.rstrip("/")
        if not s:
            continue
        # Path must be longer than the suffix itself (not root-only marker).
        if lower.endswith(s) and len(path) > len(s):
            path = path[: -len(s)]
            lower = path.lower()
            break
        if lower.endswith(s + "/") and len(path) > len(s) + 1:
            path = path[: -(len(s) + 1)]
            lower = path.lower()
            break
    # WordPress feed/trackback/atom appendages (``/world/story/feed`` →
    # ``/world/story``). Bare ``/feed`` is kept (site RSS is a real resource).
    for suffix in _CMS_FEED_TRACKBACK_PATH_SUFFIXES:
        s = suffix.rstrip("/")
        if not s:
            continue
        if lower.endswith(s) and len(path) > len(s):
            path = path[: -len(s)]
            lower = path.lower()
            break
        if lower.endswith(s + "/") and len(path) > len(s) + 1:
            path = path[: -(len(s) + 1)]
            lower = path.lower()
            break
    # Leading mobile/lite/app segment (``/m/world/story`` → ``/world/story``,
    # ``/mobile/world/story`` → ``/world/story``). Segment must be followed by
    # more path so bare ``/m`` is not emptied.
    for lead in _MOBILE_LITE_LEADING_SEGMENTS:
        if lower.startswith(lead) and len(path) > len(lead):
            path = path[len(lead) - 1 :]  # keep leading ``/`` of remainder
            lower = path.lower()
            break
    # Trailing mobile/lite/app CMS mirrors (``/world/story/mobile`` →
    # ``/world/story``). Require a parent segment so bare ``/mobile`` is kept.
    for suffix in _MOBILE_LITE_APP_PATH_SUFFIXES:
        s = suffix.rstrip("/")
        if not s:
            continue
        if lower.endswith(s) and len(path) > len(s):
            path = path[: -len(s)]
            lower = path.lower()
            break
        if lower.endswith(s + "/") and len(path) > len(s) + 1:
            path = path[: -(len(s) + 1)]
            lower = path.lower()
            break
    # CMS default document names: ``/world/story/index.html`` → ``/world/story``
    # and ``/index.html`` → ``/``. Basename always starts with ``/`` so
    # mid-segment names like ``/myindex.html`` are never stripped.
    for basename in _INDEX_BASENAMES:
        if lower.endswith(basename):
            path = path[: -len(basename)] if len(path) > len(basename) else "/"
            if not path:
                path = "/"
            lower = path.lower()
            break
    # Trailing ``.html`` / ``.htm`` is identity-noise for many news CMS paths
    # (``/world/story.html`` ≡ ``/world/story``). Applied after AMP/print/index
    # markers so ``/story/amp.html`` already collapsed before this runs.
    for ext in (".html", ".htm"):
        if lower.endswith(ext) and len(path) > len(ext):
            stripped = path[: -len(ext)]
            if stripped:
                path = stripped
                lower = path.lower()
            break
    # Keep bare root as ``/``; collapse ``/foo/`` → ``/foo`` (and multi-segment).
    if not path:
        return "/"
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def canonical_url(url: str | None) -> str | None:
    """Canonicalize a URL for dedup/identity purposes.

    Operations:
      - scheme/host lowercased
      - ``http`` upgraded to ``https`` for identity (same doc, different scheme)
      - default ports dropped
      - Google AMP Cache hosts rewritten to the origin article URL
      - Bing / Google AMP viewer hosts rewritten to the origin article URL
      - Wayback Machine (``web.archive.org/web/…``) rewritten to the origin article URL
      - Google Translate ``u=`` wrappers rewritten to the origin article URL
      - Facebook ``l(m).facebook.com/l.php?u=…`` click redirects rewritten to origin
      - Google ``/url?url=…`` (or ``q=``) click redirects rewritten to origin
      - Outlook Safe Links (``*.safelinks.protection.outlook.com/?url=…``) rewritten
      - DuckDuckGo ``/l/?uddg=…`` click redirects rewritten to origin
      - Instagram ``l.instagram.com/?u=…`` click redirects rewritten to origin
      - LinkedIn ``/safety/go`` and ``/redir/redirect`` ``url=`` wrappers rewritten
      - Reddit ``out.reddit.com/?url=…`` outbound wrappers rewritten to origin
      - YouTube ``/redirect?q=…`` external-link redirects rewritten to origin
      - Slack ``slack-redir.net/link?url=…`` outbound wrappers rewritten to origin
      - WhatsApp ``l.wl.co/?u=…`` click wrappers rewritten to origin
      - Telegram ``t.me/share/url`` / ``t.me/iv`` ``url=`` wrappers rewritten to origin
      - href.li privacy wrappers (``href.li/?https://…``) rewritten to origin
      - Tumblr ``t.umblr.com/redirect?z=…`` outbound wrappers rewritten to origin
      - Pocket ``getpocket.com/redirect?url=…`` save wrappers rewritten to origin
      - Pinterest pin-create / offsite ``url=`` share wrappers rewritten to origin
      - Flipboard share/bookmarklet ``url=`` wrappers rewritten to origin
      - Buffer compose/add ``url=`` share wrappers rewritten to origin
      - Medium external-link / redirect ``url=`` interstitials rewritten to origin
      - leading ``www.`` / ``m.`` / ``mobile.`` / ``amp.`` stripped from host
      - AMP path suffixes stripped (``/amp``, ``/amp.html``, leading ``/amp/``)
      - print-view path suffixes stripped (``/print``, ``/print.html``)
      - embed/comments/share path suffixes stripped (``/embed``, ``/comments``, …)
      - CMS feed/trackback/atom path suffixes stripped (``/feed``, ``/trackback``, …)
      - mobile/lite/app path prefixes and suffixes stripped (``/m/…``, ``/mobile``, …)
      - CMS default basenames stripped (``/index.html``, ``/index.php``, …)
      - trailing ``.html`` / ``.htm`` stripped (``/story.html`` → ``/story``)
      - path trailing slash normalized (``/`` kept; ``/foo/`` → ``/foo``)
      - tracking / AMP / print / share / SERP query parameters stripped (incl. ``utm_*``)
      - first-page pagination query params stripped (``page=1``, ``p=1``, …)
      - remaining query keys sorted
      - fragment dropped

    Identity only — callers still fetch the original URL; this value keys the
    URL fetch gate, ``doc_id``, and feed seen-cursors.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except (ValueError, AttributeError):
        return None
    if not parts.scheme or not parts.netloc:
        return None

    scheme = parts.scheme.lower()
    # Prefer https identity so http/https mirrors of the same article share a key.
    if scheme == "http":
        scheme = "https"
    netloc = parts.netloc.lower()
    # Never let userinfo (potentially credentials) leak into identity keys —
    # feed URLs may carry ``user:pass@``; canonical_url feeds doc_id and the
    # fetch gate (M-01 red-team).
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    path = parts.path or "/"
    # Drop default ports (both original http:80 and https:443 after upgrade).
    if ":" in netloc and not netloc.startswith("["):
        host, _, port = netloc.rpartition(":")
        if port in ("80", "443"):
            netloc = host
    elif netloc.startswith("[") and "]:" in netloc:
        # Rare IPv6 + port; strip :80/:443 only.
        bracket, _, port = netloc.rpartition(":")
        if port in ("80", "443"):
            netloc = bracket

    # Share / cache wrappers → origin article before other host/path identity.
    # Order: AMP CDN, AMP viewers, Wayback, then query-embedded origins
    # (Translate, Facebook, Google /url, Outlook Safe Links, DuckDuckGo /l,
    # Instagram, LinkedIn safety/redir, Reddit outbound, YouTube /redirect,
    # Slack redir, WhatsApp l.wl.co, Telegram share/iv, href.li, Tumblr
    # redirect, Pocket redirect, Pinterest pin-create/offsite, Flipboard share,
    # Buffer compose/add, Medium external-link).
    unwrapped = _unwrap_amp_cdn(netloc, path)
    if unwrapped is not None:
        netloc, path = unwrapped
        # Origin query is not present on AMP CDN forms (path-only embed).
    else:
        viewer = _unwrap_viewer_amp(netloc, path)
        if viewer is not None:
            netloc, path = viewer
        else:
            wayback = _unwrap_wayback(netloc, path, parts.query)
            if wayback is not None:
                netloc, path, origin_q = wayback
                # Origin query (wrapper query reattached when it belonged to origin).
                parts = parts._replace(query=origin_q)
            else:
                # Query-embedded origins (share / SERP / mail / social outbound).
                embedded = (
                    _unwrap_translate(netloc, parts.query)
                    or _unwrap_facebook_redirect(netloc, parts.query)
                    or _unwrap_google_url_redirect(netloc, path, parts.query)
                    or _unwrap_outlook_safelinks(netloc, parts.query)
                    or _unwrap_duckduckgo_redirect(netloc, path, parts.query)
                    or _unwrap_instagram_redirect(netloc, parts.query)
                    or _unwrap_linkedin_redirect(netloc, path, parts.query)
                    or _unwrap_reddit_outbound(netloc, parts.query)
                    or _unwrap_youtube_redirect(netloc, path, parts.query)
                    or _unwrap_slack_redirect(netloc, path, parts.query)
                    or _unwrap_whatsapp_redirect(netloc, parts.query)
                    or _unwrap_telegram_share(netloc, path, parts.query)
                    or _unwrap_href_li(netloc, parts.query)
                    or _unwrap_tumblr_redirect(netloc, path, parts.query)
                    or _unwrap_pocket_redirect(netloc, path, parts.query)
                    or _unwrap_pinterest_redirect(netloc, path, parts.query)
                    or _unwrap_flipboard_redirect(netloc, path, parts.query)
                    or _unwrap_buffer_redirect(netloc, path, parts.query)
                    or _unwrap_medium_redirect(netloc, path, parts.query)
                )
                if embedded is not None:
                    try:
                        tp = urlsplit(embedded)
                    except (ValueError, AttributeError):
                        tp = None
                    if tp is not None and tp.netloc:
                        netloc = tp.netloc.lower()
                        path = tp.path or "/"
                        parts = parts._replace(query=tp.query or "")

    netloc = _strip_alias_host(netloc)

    path = _normalize_path(path)

    # Filter and sort query params for stable identity.
    pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_noise_query_pair(k, v)
    ]
    pairs.sort()
    query = urlencode(pairs, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def domain_of(url: str | None) -> str | None:
    """Return the registered domain (eTLD+1) of a URL."""
    if not url:
        return None
    try:
        ext = _TLD_EXTRACT(url)
    except (ValueError, AttributeError):
        return None
    # Prefer the modern attribute. Do NOT ``or`` into ``registered_domain``:
    # empty string is a valid "no eTLD+1" result (localhost, bare IPs) and
    # accessing the deprecated property emits DeprecationWarning on every call.
    if hasattr(ext, "top_domain_under_public_suffix"):
        primary = ext.top_domain_under_public_suffix
    else:
        primary = getattr(ext, "registered_domain", None)
    if primary:
        return str(primary).lower()
    if ext.domain:
        return ext.domain.lower()
    return None


def is_http_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        scheme = urlsplit(url).scheme.lower()
    except (ValueError, AttributeError):
        return False
    return scheme in ("http", "https")


def is_homepage_url(url: str | None) -> bool:
    """True when ``url`` is an HTTP(S) bare-domain homepage (path ``/`` only).

    Used by tail seed discovery: a seed like ``https://example.com`` or
    ``https://example.com/`` is homepage-like; feed paths and query strings
    are not.
    """
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except (ValueError, AttributeError):
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if not parts.netloc:
        return False
    path = parts.path or "/"
    if path not in ("", "/"):
        return False
    # Query or fragment means a specific resource, not a bare homepage seed.
    if parts.query or parts.fragment:
        return False
    return True


def is_public_http_url(url: str | None) -> bool:  # noqa: PLR0911
    """Return whether ``url`` is HTTP(S) and resolves only to public IPs.

    This is intended for fetch paths that consume untrusted discovered URLs. It
    rejects localhost names, IP literals, and DNS results that are loopback,
    private, link-local, multicast, reserved, unspecified, or otherwise not
    globally routable.
    """
    if not is_http_url(url):
        return False
    try:
        parts = urlsplit(url or "")
        host = parts.hostname
        port = parts.port
    except (ValueError, AttributeError):
        return False
    if not host:
        return False

    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False

    # M-02 red-team: reject non-canonical IPv4 literal forms BEFORE DNS so the
    # verdict is platform-independent. Decimal/hex integer forms ("2130706433",
    # "0x7f000001") and leading-zero octets ("0177.0.0.1") resolve to loopback
    # on some resolvers and to errors on others — never treat them as public.
    if re.fullmatch(r"[0-9]+", host):
        return False  # decimal IPv4-integer form
    if re.fullmatch(r"0x[0-9a-fA-F]+", host):
        return False  # hex IPv4-integer form
    if re.fullmatch(r"[0-9.]+", host):
        octets = host.split(".")
        if len(octets) != 4 or any(o == "" or len(o) > 3 for o in octets):
            return False
        if any(len(o) > 1 and o.startswith("0") for o in octets):
            return False  # leading zeros (e.g. 0177.0.0.1)
        if any(int(o) > 255 for o in octets):
            return False
        return bool(ipaddress.ip_address(host).is_global)

    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii")
        infos = socket.getaddrinfo(ascii_host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return False

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in infos:
        try:
            addresses.add(ipaddress.ip_address(info[4][0]))
        except (IndexError, ValueError):
            return False

    return bool(addresses) and all(addr.is_global for addr in addresses)
