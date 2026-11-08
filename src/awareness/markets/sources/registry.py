"""Curated publisher registry loaded from ``data/publishers.yaml``.

The registry is the allowlist: news from a domain that is not registered is
rejected (``unknown_publisher``) by the quality gate. Lookups match the
registered domain and any subdomain (``markets.ft.com`` → ``ft.com``) as well
as each publisher's ``aliases`` domains.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from awareness.markets.models import FeedSpec, Publisher

_MULTI_PART_SUFFIXES = {
    "co.uk", "com.tr", "org.uk", "com.au", "co.jp", "com.hk", "co.in", "com.br", "co.kr",
    "com.cn", "com.sg", "co.nz", "co.za", "gov.uk", "gov.tr", "org.tr", "net.tr", "ac.uk",
}


def registered_domain(value: str | None) -> str | None:
    """``https://www.markets.ft.com/x`` → ``ft.com``; handles ``co.uk``-style suffixes."""
    if not value:
        return None
    raw = value.strip().lower()
    host = urlsplit(raw).hostname if "://" in raw else raw.split("/")[0]
    if not host:
        return None
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in _MULTI_PART_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last_two


class PublisherRegistry:
    def __init__(self, publishers: Iterable[Publisher]) -> None:
        self._by_id: dict[str, Publisher] = {}
        self._by_domain: dict[str, Publisher] = {}
        self._by_name: dict[str, Publisher] = {}
        for pub in publishers:
            if pub.id in self._by_id:
                raise ValueError(f"duplicate publisher id: {pub.id}")
            self._by_id[pub.id] = pub
            for dom in [pub.domain, *pub.aliases]:
                key = registered_domain(dom) or dom.lower()
                if "." in dom and key not in self._by_domain:
                    self._by_domain[key] = pub
                if "." in dom:
                    self._by_domain.setdefault(dom.lower().removeprefix("www."), pub)
            for name in [pub.name, *[a for a in pub.aliases if "." not in a]]:
                self._by_name.setdefault(name.strip().lower(), pub)
        feed_ids = [f.id for p in self._by_id.values() for f in p.feeds]
        dupes = {f for f in feed_ids if feed_ids.count(f) > 1}
        if dupes:
            raise ValueError(f"duplicate feed ids: {sorted(dupes)}")

    @classmethod
    def load(cls, path: Path | str) -> PublisherRegistry:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        items = data.get("publishers", data) if isinstance(data, dict) else data
        return cls(Publisher.model_validate(item) for item in items)

    def all(self) -> list[Publisher]:
        return list(self._by_id.values())

    def get(self, publisher_id: str) -> Publisher | None:
        return self._by_id.get(publisher_id)

    def by_domain(self, domain_or_url: str | None) -> Publisher | None:
        if not domain_or_url:
            return None
        raw = domain_or_url.strip().lower()
        host = urlsplit(raw).hostname if "://" in raw else raw.split("/")[0]
        host = (host or "").removeprefix("www.")
        if host in self._by_domain:
            return self._by_domain[host]
        # walk up subdomains: a.b.example.com → b.example.com → example.com
        parts = host.split(".")
        for i in range(1, max(1, len(parts) - 1)):
            candidate = ".".join(parts[i:])
            if candidate in self._by_domain:
                return self._by_domain[candidate]
        reg = registered_domain(host)
        return self._by_domain.get(reg) if reg else None

    def by_name(self, name: str | None) -> Publisher | None:
        if not name:
            return None
        return self._by_name.get(name.strip().lower())

    def feeds(self) -> list[tuple[Publisher, FeedSpec]]:
        return [(p, f) for p in self._by_id.values() for f in p.feeds]

    def __len__(self) -> int:
        return len(self._by_id)
