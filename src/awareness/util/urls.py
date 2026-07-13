"""URL canonicalization and identity helpers."""

from __future__ import annotations

import ipaddress
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
                host = host[len(prefix) :]
                changed = True
                break
    return f"{host}{sep}{port}" if sep else host


# Back-compat alias used by older call sites / tests that imported the name.
_strip_www_host = _strip_alias_host


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
    host = netloc.lower()
    # Drop userinfo if present (rare on AMP CDN).
    if "@" in host:
        _, _, host = host.rpartition("@")
    # Drop port (AMP CDN is always 443 in practice).
    if host.startswith("["):
        return None  # IPv6 never hosts ampproject CDN
    if ":" in host:
        host, _, _ = host.partition(":")
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
    # rest = "www.example.com/world/story" or "www.example.com"
    origin_host, sep, origin_path = rest.partition("/")
    if not origin_host or "." not in origin_host:
        # Reject path-only leftovers or single-label junk.
        return None
    # Basic host sanity: no spaces, no scheme smuggling.
    if any(ch in origin_host for ch in (" ", "?", "#", "@")):
        return None
    origin_netloc = origin_host.lower()
    origin_path_out = f"/{origin_path}" if sep else "/"
    return origin_netloc, origin_path_out


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
      - leading ``www.`` / ``m.`` / ``mobile.`` / ``amp.`` stripped from host
      - AMP path suffixes stripped (``/amp``, ``/amp.html``, leading ``/amp/``)
      - print-view path suffixes stripped (``/print``, ``/print.html``)
      - embed/comments/share path suffixes stripped (``/embed``, ``/comments``, …)
      - mobile/lite/app path prefixes and suffixes stripped (``/m/…``, ``/mobile``, …)
      - CMS default basenames stripped (``/index.html``, ``/index.php``, …)
      - trailing ``.html`` / ``.htm`` stripped (``/story.html`` → ``/story``)
      - path trailing slash normalized (``/`` kept; ``/foo/`` → ``/foo``)
      - tracking / AMP / print / share query parameters stripped (incl. ``utm_*``)
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

    # Google AMP Cache → origin article before other host/path identity rules.
    unwrapped = _unwrap_amp_cdn(netloc, path)
    if unwrapped is not None:
        netloc, path = unwrapped

    netloc = _strip_alias_host(netloc)

    path = _normalize_path(path)

    # Filter and sort query params for stable identity.
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_noise_query_pair(k, v)
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
