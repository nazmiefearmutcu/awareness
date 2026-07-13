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


_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _is_tracking_param(key: str) -> bool:
    kl = key.lower()
    return kl in _TRACKING_PARAMS or kl.startswith("utm_")


def _is_noise_query_pair(key: str, value: str) -> bool:
    """True for tracking keys or AMP-only query values (``output=amp``)."""
    if _is_tracking_param(key):
        # ``output`` is only noise when it marks AMP; keep other output= values.
        if key.lower() == "output":
            return value.strip().lower() in ("amp", "amphtml", "htmlamp")
        return True
    return False


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


def _normalize_path(path: str) -> str:
    """Normalize path for identity: empty → ``/``; strip AMP suffixes + trailing slash."""
    if not path:
        return "/"
    # Lowercase only for AMP suffix detection; rebuild with original casing
    # collapsed via the trailing-slash rule. AMP markers are ASCII.
    lower = path.lower()
    for suffix in _AMP_PATH_SUFFIXES:
        if lower.endswith(suffix) and len(path) > len(suffix):
            path = path[: -len(suffix)]
            lower = path.lower()
            break
    # Leading ``/amp/`` segment (``/amp/world/story`` → ``/world/story``).
    if lower.startswith("/amp/") and len(path) > 5:
        path = path[4:]  # drop leading "/amp"
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
      - leading ``www.`` / ``m.`` / ``mobile.`` / ``amp.`` stripped from host
      - AMP path suffixes stripped (``/amp``, ``/amp.html``, leading ``/amp/``)
      - path trailing slash normalized (``/`` kept; ``/foo/`` → ``/foo``)
      - tracking / AMP query parameters stripped (incl. any ``utm_*``)
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

    netloc = _strip_alias_host(netloc)

    path = _normalize_path(parts.path or "/")

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
