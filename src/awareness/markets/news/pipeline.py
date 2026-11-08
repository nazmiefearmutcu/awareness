"""News pipeline: RawNewsItem → enriched, quality-gated, clustered NewsItem in the store.

Stages (spec §5): publisher resolution → optional full-text extraction →
entities → taxonomy → sentiment (item + per entity) → quality gate → story
clustering (+ corroboration re-scoring of existing members) → importance →
persist items and stories.

All heavy CPU work (FinBERT, regex scanning) runs in a worker thread so the
collector event loop stays responsive. Components are injected so the
pipeline is testable with fakes; :meth:`NewsPipeline.build` wires the real ones.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from awareness.markets.config import MarketsSettings
from awareness.markets.http import Fetcher
from awareness.markets.models import (
    EntityRef,
    NewsItem,
    ProviderRef,
    Publisher,
    RawNewsItem,
    SentimentScore,
    Story,
)
from awareness.markets.sources.registry import PublisherRegistry, registered_domain
from awareness.markets.store import MarketsStore

log = logging.getLogger(__name__)

FILTER_ENTITY_TYPES = {"equity", "crypto", "fx", "commodity", "index"}
EXCERPT_CHARS = 400


# ── component protocols (implemented in nlp/, news/quality.py, news/cluster.py) ──


class _Linker(Protocol):
    def link(self, title: str, text: str | None = None, *, language: str | None = None) -> list[EntityRef]: ...


class _Taxonomy(Protocol):
    def classify(
        self,
        title: str,
        text: str | None,
        entities: list[EntityRef],
        *,
        publisher: Publisher | None = None,
        feed_categories: list[str] | None = None,
    ) -> Any: ...


class _Scorer(Protocol):
    @property
    def model(self) -> str: ...

    def score_many(self, texts: list[str]) -> list[SentimentScore]: ...

    def entity_sentiment(
        self, title: str, text: str | None, entities: list[EntityRef], *, max_sentences_per_entity: int = 4
    ) -> dict[str, SentimentScore]: ...


class _Gate(Protocol):
    def evaluate(
        self,
        item: NewsItem,
        publisher: Publisher | None,
        *,
        now: datetime,
        finance_relevance: float,
        corroboration: int = 1,
        timestamp_source: str = "feed",
        content_type_hint: str | None = None,
    ) -> Any: ...


class _Clusterer(Protocol):
    def find_story(self, item: NewsItem, candidates: list[NewsItem]) -> str | None: ...

    def new_story_id(self, item: NewsItem) -> str: ...

    def build_story(self, items: list[NewsItem], *, story_id: str) -> Story: ...

    @staticmethod
    def corroboration(items: list[NewsItem]) -> int: ...


Extractor = Callable[[Fetcher, str], Any]  # async (fetcher, url) -> ExtractedArticle | None


@dataclass
class _Draft:
    raw: RawNewsItem
    publisher: Publisher | None
    item: NewsItem
    timestamp_source: str
    content_type_hint: str | None = None
    text_for_nlp: str = ""


@dataclass
class PipelineStats:
    received: int = 0
    skipped_existing: int = 0
    extracted: int = 0
    stored: int = 0
    rejected: int = 0
    stories_touched: int = 0
    rescored_members: int = 0
    notes: list[str] = field(default_factory=list)


def _excerpt(body: str | None, summary: str | None) -> str | None:
    source = (body or summary or "").strip()
    if not source:
        return None
    if len(source) <= EXCERPT_CHARS:
        return source
    cut = source[:EXCERPT_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > EXCERPT_CHARS * 0.6 else cut).rstrip(" ,.;:") + "…"


class NewsPipeline:
    """Implements ``NewsPipelineProtocol`` (``await process(raws) -> stored items``)."""

    def __init__(
        self,
        *,
        settings: MarketsSettings,
        store: MarketsStore,
        registry: PublisherRegistry,
        fetcher: Fetcher | None,
        linker: _Linker,
        taxonomy: _Taxonomy,
        scorer: _Scorer,
        gate: _Gate,
        clusterer: _Clusterer,
        importance: Callable[..., float],
        extractor: Extractor | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.fetcher = fetcher
        self.linker = linker
        self.taxonomy = taxonomy
        self.scorer = scorer
        self.gate = gate
        self.clusterer = clusterer
        self.importance = importance
        self.extractor = extractor
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self.last_stats = PipelineStats()

    # ── wiring ────────────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        *,
        settings: MarketsSettings,
        store: MarketsStore,
        registry: PublisherRegistry,
        fetcher: Fetcher | None,
    ) -> NewsPipeline:
        """Wire the production components (imports are local so a missing module fails loudly here)."""
        from awareness.markets.news.cluster import StoryClusterer, publisher_names_from
        from awareness.markets.news.quality import QualityGate, compute_importance
        from awareness.markets.nlp.entities import EntityLinker
        from awareness.markets.nlp.sentiment import SentimentScorer
        from awareness.markets.nlp.taxonomy import TaxonomyClassifier

        extractor: Extractor | None = None
        if settings.fulltext and fetcher is not None:
            from awareness.markets.news.extract import RobotsChecker, fetch_and_extract

            robots = RobotsChecker()

            async def _extract(f: Fetcher, url: str) -> Any:
                return await fetch_and_extract(f, url, robots=robots, user_agent=settings.bot_user_agent)

            extractor = _extract

        return cls(
            settings=settings,
            store=store,
            registry=registry,
            fetcher=fetcher,
            linker=EntityLinker.from_settings(settings),
            taxonomy=TaxonomyClassifier(settings.config_file("taxonomy.yaml")),
            scorer=SentimentScorer(settings.sentiment_backend, model_name=settings.finbert_model),
            gate=QualityGate(max_age_days=settings.max_item_age_days, rules_path=settings.config_file("quality_rules.yaml")),
            clusterer=StoryClusterer(
                window_hours=settings.cluster_window_hours,
                publisher_names=publisher_names_from(registry.all()),
            ),
            importance=compute_importance,
            extractor=extractor,
        )

    # ── public API ────────────────────────────────────────────────────────

    async def process(self, raws: list[RawNewsItem]) -> list[NewsItem]:
        async with self._lock:  # one batch at a time keeps clustering consistent
            return await self._process(raws)

    async def _process(self, raws: list[RawNewsItem]) -> list[NewsItem]:
        stats = PipelineStats(received=len(raws))
        now = self._now()
        unique: dict[str, RawNewsItem] = {}
        for raw in raws:
            unique.setdefault(NewsItem.id_for_url(raw.url), raw)
        existing = self.store.news_exists(unique.keys())
        stats.skipped_existing = len(existing)
        todo = [(item_id, raw) for item_id, raw in unique.items() if item_id not in existing]
        if not todo:
            self.last_stats = stats
            return []

        drafts = [self._draft(item_id, raw, now) for item_id, raw in todo]
        stats.extracted = await self._extract_fulltext(drafts)
        await asyncio.to_thread(self._enrich, drafts, now)

        stored, touched_stories, rescored = self._cluster_and_score(drafts, now)
        stats.stored = len(stored)
        stats.rejected = sum(1 for d in drafts if d.item.quality_label == "rejected")
        stats.stories_touched = len(touched_stories)
        stats.rescored_members = len(rescored)
        self.store.upsert_news([*stored, *rescored])
        if touched_stories:
            self.store.upsert_stories(touched_stories)
        self.last_stats = stats
        log.info(
            "news pipeline: received=%d new=%d stored=%d rejected=%d stories=%d rescored=%d",
            stats.received, len(todo), stats.stored, stats.rejected, stats.stories_touched, stats.rescored_members,
        )
        return stored

    async def recluster(self, *, hours: int | None = None) -> int:
        """Re-run story clustering over the recent window (after a rule change or at startup).

        Items are replayed in publication order against the stories formed so far; story rows are
        rebuilt and members re-scored for their corroboration. Returns the number of items whose
        story or score changed.
        """
        async with self._lock:
            return await asyncio.to_thread(self._recluster_sync, hours or self.settings.cluster_window_hours * 2)

    def _recluster_sync(self, hours: int) -> int:
        now = self._now()
        items = [
            i for i in self.store.recent_news(now - timedelta(hours=hours), min_quality="low", limit=20000)
            if i.quality_label != "rejected"
        ]
        items.sort(key=lambda i: (i.published_at, i.id))
        processed: list[NewsItem] = []
        assigned: dict[str, str] = {}
        for item in items:
            probe = item.model_copy(update={"story_id": None})
            story_id = self.clusterer.find_story(probe, processed) or self.clusterer.new_story_id(item)
            assigned[item.id] = story_id
            processed.append(item.model_copy(update={"story_id": story_id}))
        groups: dict[str, list[NewsItem]] = {}
        for item in processed:
            groups.setdefault(item.story_id or item.id, []).append(item)
        changed: list[NewsItem] = []
        stories: list[Story] = []
        original = {i.id: i for i in items}
        for story_id, group in groups.items():
            corroboration = self.clusterer.corroboration(group)
            refreshed: list[NewsItem] = []
            for item in group:
                base = original[item.id]
                updated = item
                rescored = self._rescore_existing(item, now, corroboration) if corroboration > 1 or base.story_id != story_id else None
                if rescored is not None:
                    updated = rescored.model_copy(update={"story_id": story_id})
                if updated.story_id != base.story_id or updated.quality_score != base.quality_score:
                    changed.append(updated)
                refreshed.append(updated)
            stories.append(self.clusterer.build_story(refreshed, story_id=story_id))
        if changed:
            self.store.upsert_news(changed)
        self.store.upsert_stories(stories)
        log.info("recluster: %d items, %d stories, %d changed", len(items), len(stories), len(changed))
        return len(changed)

    # ── stages ────────────────────────────────────────────────────────────

    def _resolve_publisher(self, raw: RawNewsItem) -> Publisher | None:
        if raw.origin_publisher_domain:
            pub = self.registry.by_domain(raw.origin_publisher_domain)
            if pub is not None:
                return pub
        if raw.origin_publisher_name:
            pub = self.registry.by_name(raw.origin_publisher_name)
            if pub is not None:
                return pub
        pub = self.registry.get(raw.publisher_id)
        if pub is not None and pub.kind == "aggregator" and raw.origin_publisher_domain:
            return None  # aggregator item from an unregistered origin → unknown publisher
        return pub

    def _draft(self, item_id: str, raw: RawNewsItem, now: datetime) -> _Draft:
        publisher = self._resolve_publisher(raw)
        fetched_at = raw.fetched_at or now
        if raw.published_at is not None:
            published_at, ts_source = raw.published_at, "feed"
        else:
            published_at, ts_source = fetched_at, "fetched"
        domain = (
            publisher.domain
            if publisher
            else (registered_domain(raw.origin_publisher_domain) or registered_domain(raw.url) or "unknown")
        )
        item = NewsItem(
            id=item_id,
            url=raw.url,
            title=raw.title.strip(),
            summary=raw.summary,
            body_text=raw.body_text,
            excerpt=_excerpt(raw.body_text, raw.summary),
            publisher_id=publisher.id if publisher else (raw.origin_publisher_name or raw.publisher_id),
            publisher_name=publisher.name if publisher else (raw.origin_publisher_name or raw.publisher_id),
            publisher_domain=domain,
            publisher_tier=publisher.tier if publisher else 5,
            author=raw.author,
            language=(raw.language or (publisher.languages[0] if publisher and publisher.languages else "en"))[:8],
            region=publisher.region if publisher else None,
            published_at=published_at,
            first_seen_at=now,
            feed_id=raw.feed_id,
            categories=list(raw.categories),
            is_paywalled=bool(publisher.paywalled) if publisher else False,
            word_count=len((raw.body_text or "").split()),
            sources=[ProviderRef(provider=raw.feed_id, url=raw.url, fetched_at=fetched_at)],
        )
        return _Draft(raw=raw, publisher=publisher, item=item, timestamp_source=ts_source)

    async def _extract_fulltext(self, drafts: list[_Draft]) -> int:
        if self.extractor is None or self.fetcher is None or not self.settings.fulltext:
            return 0
        eligible = [
            d
            for d in drafts
            if d.publisher is not None
            and d.publisher.fetch_fulltext
            and not d.publisher.paywalled
            and d.publisher.tier <= 3
            and not d.item.body_text
            and d.item.url.startswith("http")
        ]
        if not eligible:
            return 0
        sem = asyncio.Semaphore(max(1, self.settings.fulltext_concurrency))
        fetcher = self.fetcher
        extractor = self.extractor

        async def one(d: _Draft) -> bool:
            async with sem:
                try:
                    art = await extractor(fetcher, d.item.url)
                except Exception as exc:  # extraction is best-effort
                    log.debug("extract failed for %s: %s", d.item.url, exc)
                    return False
            if art is None or not getattr(art, "text", None):
                return False
            text = str(art.text)
            update: dict[str, Any] = {
                "body_text": text,
                "word_count": int(getattr(art, "word_count", 0) or len(text.split())),
                "excerpt": _excerpt(text, d.item.summary),
            }
            if not d.item.author and getattr(art, "author", None):
                update["author"] = art.author
            art_published = getattr(art, "published_at", None)
            if d.timestamp_source == "fetched" and art_published is not None:
                update["published_at"] = art_published
                d.timestamp_source = "article"
            d.item = d.item.model_copy(update=update)
            return True

        results = await asyncio.gather(*(one(d) for d in eligible))
        return sum(1 for r in results if r)

    def _enrich(self, drafts: list[_Draft], now: datetime) -> None:
        texts: list[str] = []
        for d in drafts:
            body = d.item.body_text or ""
            d.text_for_nlp = " ".join(x for x in (d.item.summary or "", body[:3000]) if x).strip()
            entities = self.linker.link(d.item.title, d.text_for_nlp or None, language=d.item.language)
            tax = self.taxonomy.classify(
                d.item.title,
                d.text_for_nlp or None,
                entities,
                publisher=d.publisher,
                feed_categories=list(d.raw.categories),
            )
            d.content_type_hint = getattr(tax, "content_type_hint", None)
            symbols = [e.id for e in entities if e.type in FILTER_ENTITY_TYPES]
            countries = [e.id for e in entities if e.type == "country"]
            d.item = d.item.model_copy(
                update={
                    "entities": entities,
                    "symbols": list(dict.fromkeys(symbols)),
                    "countries": list(dict.fromkeys(countries)),
                    "asset_classes": list(getattr(tax, "asset_classes", []) or []),
                    "topics": list(getattr(tax, "topics", []) or []),
                    "finance_relevance": float(getattr(tax, "finance_relevance", 0.0) or 0.0),
                }
            )
            lead = d.item.title if not d.item.summary else f"{d.item.title}. {d.item.summary[:400]}"
            texts.append(lead)
        scores = self.scorer.score_many(texts) if texts else []
        for d, score in zip(drafts, scores, strict=True):
            ent_sent: dict[str, SentimentScore] = {}
            relevant = [e for e in d.item.entities if e.type in FILTER_ENTITY_TYPES or e.type == "central_bank"]
            if relevant:
                ent_sent = self.scorer.entity_sentiment(d.item.title, d.text_for_nlp or None, relevant)
            d.item = d.item.model_copy(update={"sentiment": score, "entity_sentiment": ent_sent})
            self._apply_quality(d, now, corroboration=1)

    def _apply_quality(self, d: _Draft, now: datetime, *, corroboration: int) -> None:
        verdict = self.gate.evaluate(
            d.item,
            d.publisher,
            now=now,
            finance_relevance=d.item.finance_relevance,
            corroboration=corroboration,
            timestamp_source=d.timestamp_source,
            content_type_hint=d.content_type_hint,
        )
        d.item = d.item.model_copy(
            update={
                "quality_score": float(verdict.score),
                "quality_label": verdict.label,
                "quality_reasons": list(verdict.reasons),
                "content_type": verdict.content_type,
            }
        )
        d.item = d.item.model_copy(update={"importance": float(self.importance(d.item, corroboration=corroboration))})

    def _cluster_and_score(
        self, drafts: list[_Draft], now: datetime
    ) -> tuple[list[NewsItem], list[Story], list[NewsItem]]:
        window = timedelta(hours=self.settings.cluster_window_hours)
        pool: list[NewsItem] = [
            n for n in self.store.recent_news(now - window - timedelta(hours=12), min_quality="low") if n.story_id
        ]
        members: dict[str, dict[str, NewsItem]] = {}
        for n in pool:
            members.setdefault(n.story_id or n.id, {})[n.id] = n
        new_story_ids: set[str] = set()
        for d in drafts:
            if d.item.quality_label == "rejected":
                continue
            story_id = self.clusterer.find_story(d.item, pool) or self.clusterer.new_story_id(d.item)
            d.item = d.item.model_copy(update={"story_id": story_id})
            pool.append(d.item)
            members.setdefault(story_id, {})[d.item.id] = d.item
            new_story_ids.add(story_id)

        stored: list[NewsItem] = []
        rescored: list[NewsItem] = []
        stories: list[Story] = []
        draft_by_id = {d.item.id: d for d in drafts}
        for story_id in new_story_ids:
            group = list(members[story_id].values())
            corroboration = self.clusterer.corroboration(group)
            refreshed: list[NewsItem] = []
            for item in group:
                draft = draft_by_id.get(item.id)
                if draft is not None:
                    if corroboration > 1:
                        self._apply_quality(draft, now, corroboration=corroboration)
                    refreshed.append(draft.item)
                elif corroboration > 1:
                    updated = self._rescore_existing(item, now, corroboration)
                    if updated is not None:
                        rescored.append(updated)
                        refreshed.append(updated)
                    else:
                        refreshed.append(item)
                else:
                    refreshed.append(item)
            stories.append(self.clusterer.build_story(refreshed, story_id=story_id))
        for d in drafts:
            stored.append(d.item)
        return stored, stories, rescored

    def _rescore_existing(self, item: NewsItem, now: datetime, corroboration: int) -> NewsItem | None:
        publisher = self.registry.get(item.publisher_id)
        if publisher is None:
            return None
        verdict = self.gate.evaluate(
            item,
            publisher,
            now=now,
            finance_relevance=item.finance_relevance,
            corroboration=corroboration,
            timestamp_source="feed",
            content_type_hint=item.content_type,
        )
        if verdict.label == "rejected":
            return None  # stale by now: keep the stored verdict
        updated = item.model_copy(
            update={
                "quality_score": float(verdict.score),
                "quality_label": verdict.label,
                "quality_reasons": list(verdict.reasons),
            }
        )
        updated = updated.model_copy(update={"importance": float(self.importance(updated, corroboration=corroboration))})
        if (
            updated.quality_score == item.quality_score
            and updated.importance == item.importance
            and updated.quality_label == item.quality_label
        ):
            return None
        return updated


def default_now() -> datetime:
    return datetime.now(UTC)


__all__: Sequence[str] = ["NewsPipeline", "PipelineStats"]
