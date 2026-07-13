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
    }
)


_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _is_tracking_param(key: str) -> bool:
    kl = key.lower()
    return kl in _TRACKING_PARAMS or kl.startswith("utm_")


def _strip_www_host(netloc: str) -> str:
    """Strip a leading ``www.`` label from host (preserve userinfo / port)."""
    if "@" in netloc:
        userinfo, _, hostport = netloc.rpartition("@")
        return f"{userinfo}@{_strip_www_host(hostport)}"
    # IPv6 literals are bracketed; never treat them as www hosts.
    if netloc.startswith("["):
        return netloc
    if netloc.startswith("www."):
        return netloc[4:]
    return netloc


def _normalize_path(path: str) -> str:
    """Normalize path for identity: empty → ``/``; strip a single trailing slash."""
    if not path:
        return "/"
    # Keep bare root as ``/``; collapse ``/foo/`` → ``/foo`` (and multi-segment).
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def canonical_url(url: str | None) -> str | None:
    """Canonicalize a URL for dedup/identity purposes.

    Operations:
      - scheme/host lowercased
      - default ports dropped
      - leading ``www.`` stripped from host (common news alias)
      - path trailing slash normalized (``/`` kept; ``/foo/`` → ``/foo``)
      - tracking query parameters stripped (incl. any ``utm_*``)
      - remaining query keys sorted
      - fragment dropped
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
    netloc = parts.netloc.lower()
    # Drop default ports.
    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        if (scheme, port) in (("http", "80"), ("https", "443")):
            netloc = host

    netloc = _strip_www_host(netloc)

    path = _normalize_path(parts.path or "/")

    # Filter and sort query params for stable identity.
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
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
