"""Deterministic tweet-fetch simulation for X scraper sessions.

Generates realistic-looking tweets for a :class:`SessionStore` session so the
downstream analysis surface (``analyze.py``, the API, the CLI) can be
exercised without a live X connection. Output is fully deterministic for a
given ``(seed, n_tweets)`` pair: the same seed reproduces the same texts,
metrics, and timestamps.

Tweets are stored through the regular ``store.store_tweets`` path, so the
store's PK dedup still applies: re-running with the same seed inserts nothing
new (no crash), while a different seed produces fresh ``sim-<nonce>-<i>`` ids.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta

from awareness.util.timeutil import utcnow
from awareness.xscraper.models import SearchRequest, TweetRecord
from awareness.xscraper.query import normalize_handle
from awareness.xscraper.store import SessionStore

_MIN_TWEETS = 1
_MAX_TWEETS = 200
_DEFAULT_LOOKBACK_SECONDS = 3600

# Mixed-polarity templates: positive/negative words come from the sentiment
# lexicon so downstream analysis sees all three sentiment classes, plus
# neutral templates for balance. "{kw}" is substituted per tweet.
_TEMPLATES: tuple[str, ...] = (
    # positive
    "{kw} is surging today, eyeing fresh highs",
    "Breaking: {kw} rallies hard as buyers step in",
    "{kw} soars after strong earnings",
    "Big gains for {kw} as momentum builds",
    "{kw} breaks out to a record high",
    "Bullish signals stacking up for {kw}",
    "{kw} keeps climbing, growth story intact",
    "Analysts upgrade {kw} on strong fundamentals",
    "Buyers boost {kw} on improving outlook",
    "{kw} rebounds after the dip, solid recovery",
    "Positive momentum: {kw} gains for a third day",
    "Encouraging data for {kw}, opportunity ahead",
    # negative
    "{kw} crashes as investors panic",
    "Concerns grow: {kw} slumps to new lows",
    "Worries mount over {kw} after weak guidance",
    "Breaking: {kw} suffers a sharp selloff",
    "{kw} tumbles amid fear and uncertainty",
    "Losses pile up for {kw}, worst session in weeks",
    "Bearish pressure mounts on {kw}",
    "{kw} slips on disappointing results",
    "{kw} faces fresh risks as the downturn deepens",
    "Warning signs for {kw}, momentum fades",
    "Sellers hammer {kw}, risk rising",
    # neutral
    "{kw} update: what you need to know",
    "Thread: everything about {kw} this week",
    "Discussion: where does {kw} go from here?",
    "Live report from the {kw} scene",
    "My notes on {kw} after today's session",
    "Quick take: {kw} in five bullet points",
    "New interview about {kw} worth a watch",
)


async def _load_request(store: SessionStore, session_id: str) -> SearchRequest | None:
    """Read the persisted ``request_json`` column for a session.

    ``SessionSnapshot`` exposes keywords/accounts/lookback but not the
    language filter, so the original :class:`SearchRequest` is loaded from
    the store's own connection (the store has no accessor for this column).
    Returns ``None`` when the row or column is unparseable.
    """
    db = store._require_db()  # same-subsystem connection (no public accessor)
    cur = await db.execute("SELECT request_json FROM sessions WHERE session_id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None or not row["request_json"]:
        return None
    try:
        return SearchRequest.model_validate(json.loads(row["request_json"]))
    except ValueError:
        return None


async def simulate_session(
    store: SessionStore,
    session_id: str,
    n_tweets: int = 20,
    seed: int | None = None,
) -> int:
    """Generate and store *n_tweets* simulated tweets; return inserted count.

    Reads the session's :class:`SearchRequest` (falling back to the snapshot
    columns), then emits deterministic tweets: keyword-bearing texts over the
    session's lookback window, accounts (or generated ``user{i}`` handles) as
    authors, and seeded likes/retweets. The count is clamped to ``1..200``.
    Raises :class:`KeyError` when the session does not exist.
    """
    session = await store.get_session(session_id)
    if session is None:
        raise KeyError(f"session {session_id!r} not found")

    request = await _load_request(store, session_id)
    keywords = list(request.keywords) if request else list(session.keywords)
    accounts = list(request.accounts) if request else list(session.accounts)
    language = (request.language if request else None) or "en"
    lookback_seconds = session.lookback_seconds or _DEFAULT_LOOKBACK_SECONDS
    handles = [normalize_handle(account) for account in accounts if normalize_handle(account)]
    topics = keywords or handles or ["market"]

    # Seeded RNG for deterministic simulation output — not security-sensitive.
    rng = random.Random(seed)  # noqa: S311
    nonce = seed if seed is not None else rng.randrange(16**8)
    count = max(_MIN_TWEETS, min(int(n_tweets), _MAX_TWEETS))
    now = utcnow()

    tweets: list[TweetRecord] = []
    for i in range(count):
        template = rng.choice(_TEMPLATES)
        topic = rng.choice(topics)
        if handles:
            username = rng.choice(handles)
            display_name = username.title()
            author_id = f"sim-author-{username}"
        else:
            username = f"user{i + 1}"
            display_name = f"User {i + 1}"
            author_id = f"sim-author-user{i + 1}"
        created_at = now - timedelta(seconds=rng.uniform(0, lookback_seconds))
        tweet_id = f"sim-{nonce}-{i}"
        tweets.append(
            TweetRecord(
                tweet_id=tweet_id,
                session_id=session_id,
                author_id=author_id,
                username=username,
                display_name=display_name,
                text=template.format(kw=topic),
                created_at=created_at,
                fetched_at=now,
                url=f"https://x.com/{username}/status/{tweet_id}",
                source="simulated",
                query=session.query,
                lang=language,
                metrics={
                    "likes": rng.randint(0, 5000),
                    "retweets": rng.randint(0, 2000),
                    "replies": rng.randint(0, 500),
                },
                raw={"simulated": True, "seed": seed},
            )
        )
    tweets.sort(key=lambda tweet: tweet.created_at, reverse=True)
    return await store.store_tweets(session_id, tweets)
