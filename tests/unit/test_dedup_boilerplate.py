"""W19: boilerplate/template text must not collapse distinct articles.

The simhash band lookup merges by Hamming distance alone, so DISTINCT short
articles sharing a template footer can land within DEFAULT_NEAR_THRESHOLD and
fold into ONE parent_doc_or_dup_group — search collapse then returns 1 row
and export dedupe folds them. The content-diversity guard (token-set sketch:
token_hash + token_count on dedup_near) must keep those docs in separate
groups while genuine near-dups still merge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from awareness.dedup.engine import DedupDecision, DedupEngine
from awareness.schemas.doc import DocCapture, RobotsDecision, SourceKind, SourceRef
from awareness.storage.state import StateDB
from awareness.util.hashing import content_hash, doc_id_for, hamming128, simhash64, simhash128

# A long template footer shared verbatim by every doc in the W19 scenario.
FOOTER = (
    "Please read our full terms of service and privacy policy before continuing. "
    "This website uses cookies to improve your browsing experience and analyze site traffic. "
    "By clicking accept you agree to our data collection practices described in the policy. "
    "For questions about our editorial guidelines, contact the newsroom support team. "
    "All content is protected by copyright and may not be reproduced without written permission. "
    "The views expressed here are those of the individual authors and do not necessarily reflect "
    "the position of the publishing company or its affiliates. Stay tuned for more updates."
)

# Four DISTINCT short climate articles that all carry the footer.
W19_DOCS: list[tuple[str, str]] = [
    ("deforestation report", "A new report found deforestation rates rising in the Amazon basin this year. "),
    ("ice core study", "Scientists studying ice cores reported surprising temperature variations last decade. "),
    ("wind farm auction", "The latest wind farm auction attracted bids from three major energy consortiums. "),
    ("coral reef survey", "A coral reef survey documented widespread bleaching across the southern atolls. "),
]


def _make_cap(url: str, text: str, *, observed_str: str = "2024-01-01T00:00:00+00:00") -> DocCapture:
    ch = content_hash(text)
    did = doc_id_for(url, ch)
    return DocCapture(
        doc_id=did,
        capture_id=f"cap-{did[:8]}-{observed_str}",
        source=SourceRef(
            source_type=SourceKind.LOCAL_FIXTURE, source_name="fixture", source_locator="local"
        ),
        discovery_channel="test",
        ingest_version="0.0",
        url=url,
        canonical_url=url,
        domain="x.test",
        fetch_ts=datetime(2024, 1, 1, tzinfo=UTC),
        observed_ts=datetime.fromisoformat(observed_str),
        text=text,
        content_hash=ch,
        near_dup_hash=simhash64(text),
        robots_decision=RobotsDecision.NOT_APPLICABLE,
    )


def _state(tmp_path: Path) -> StateDB:
    db = StateDB(f"sqlite:///{tmp_path / 'state.db'}")
    db.init()
    return db


def test_fixture_is_a_real_w19_reproduction() -> None:
    """Sanity: without the guard the docs genuinely land within the threshold.

    The pairwise simhash128 Hamming distances must all be <= the default
    threshold, i.e. this fixture would collapse into ONE dup group under the
    pre-W19 engine (the band lookup retrieves them and hamming <= 32 merges).
    """
    sigs = [simhash128(text + FOOTER) for _title, text in W19_DOCS]
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            assert hamming128(sigs[i], sigs[j]) <= 32, (
                f"fixture pair {i}/{j} not within merge threshold — update the test"
            )


def test_boilerplate_articles_stay_in_distinct_groups(tmp_path: Path) -> None:
    """W19 regression: 4 distinct docs sharing only a footer -> 4 groups."""
    db = _state(tmp_path)
    eng = DedupEngine(db)
    groups: set[str] = set()
    for i, (title, content) in enumerate(W19_DOCS):
        cap = _make_cap(f"https://w19.example/{i}", content + FOOTER)
        out = eng.evaluate(cap)
        assert out.decision == DedupDecision.NEW, (
            f"doc {title!r} must stay NEW (boilerplate-only overlap), got {out.decision}"
        )
        groups.add(cap.parent_doc_or_dup_group)
    assert len(groups) == len(W19_DOCS), (
        f"distinct docs sharing a footer collapsed to {len(groups)} group(s); "
        "the W19 content-diversity guard must keep them apart"
    )


def test_short_genuine_near_dup_same_token_set_still_merges(tmp_path: Path, monkeypatch) -> None:
    """Short docs (<=200 unique tokens) merge when the token sets agree.

    The same words reordered produce the same token-set hash (short-doc path
    requires exact token-set agreement) while a different simhash; the guard
    must let this genuine near-dup through.
    """
    sig_a = (1 << 128) - 1
    sig_b = sig_a ^ ((1 << 32) - 1)

    calls = {"n": 0}

    def fake_simhash(text: str) -> int:
        calls["n"] += 1
        return sig_a if calls["n"] == 1 else sig_b

    monkeypatch.setattr("awareness.dedup.engine.simhash128", fake_simhash)

    tokens = (
        "global climate accord signed by delegations overnight for the new review "
        "process established under the agreement"
    )
    reordered = " ".join(reversed(tokens.split()))
    db = _state(tmp_path)
    eng = DedupEngine(db)
    a = _make_cap("https://a.test/1", tokens)
    out_a = eng.evaluate(a)
    assert out_a.decision == DedupDecision.NEW

    b = _make_cap("https://b.test/2", reordered, observed_str="2024-01-02T00:00:00+00:00")
    out_b = eng.evaluate(b)
    assert out_b.decision == DedupDecision.NEAR_DUP
    assert out_b.hamming == 32
    assert b.parent_doc_or_dup_group == out_a.dup_group


def test_long_genuine_near_dup_still_merges(tmp_path: Path) -> None:
    """Long docs (>200 unique tokens) merge on Hamming + token-count ratio.

    A vocabulary-rich article plus a trailing phrase is a genuine near-dup:
    the long-doc path skips the exact token-set requirement (boilerplate is
    dilute in long articles) and the count ratio is tiny.
    """
    base = " ".join(f"climate report token number {i} from the regional desk" for i in range(230))
    near = base + " extra trailing words to nudge the simhash a bit"
    db = _state(tmp_path)
    eng = DedupEngine(db, near_threshold=24)
    a = _make_cap("https://a.test/1", base)
    eng.evaluate(a)
    b = _make_cap("https://b.test/2", near, observed_str="2024-01-02T00:00:00+00:00")
    out = eng.evaluate(b)
    assert out.decision == DedupDecision.NEAR_DUP
    assert b.parent_doc_or_dup_group == a.doc_id


def test_legacy_null_sketch_merges_by_hamming_only(tmp_path: Path, monkeypatch) -> None:
    """Legacy dedup_near rows (NULL token sketch) keep the old behavior.

    The engine must treat an unknown candidate sketch as 'unknown' and merge
    on Hamming distance alone, so pre-migration indexes never start
    re-classifying near-dups as NEW.
    """
    sig_a = (1 << 128) - 1
    sig_b = sig_a ^ ((1 << 32) - 1)
    calls = {"n": 0}

    def fake_simhash(text: str) -> int:
        calls["n"] += 1
        return sig_a if calls["n"] == 1 else sig_b

    monkeypatch.setattr("awareness.dedup.engine.simhash128", fake_simhash)

    db = _state(tmp_path)
    # Index "docA" the legacy way: band rows WITHOUT the token-set sketch.
    db.add_near_dup_index("docA", sig_a)
    eng = DedupEngine(db)
    a = _make_cap("https://a.test/1", "alpha body")
    eng.evaluate(a)  # NEW (already indexed under docA by hand)
    b = _make_cap("https://b.test/2", "beta body", observed_str="2024-01-02T00:00:00+00:00")
    out_b = eng.evaluate(b)
    assert out_b.decision == DedupDecision.NEAR_DUP, (
        "candidate with NULL token sketch must merge by hamming alone (old behavior)"
    )
    assert out_b.hamming == 32


def test_dedup_near_rows_carry_token_sketch(tmp_path: Path) -> None:
    """Engine-written rows store the token-set sketch for the guard."""
    db = _state(tmp_path)
    eng = DedupEngine(db)
    text1 = "one two three four five six seven eight nine ten"
    cap = _make_cap("https://a.test/1", text1)
    eng.evaluate(cap)
    rows1 = db.find_near_dup_candidate_rows(simhash128(text1))
    matches = [r for r in rows1 if r[0] == cap.doc_id]
    assert matches, "engine must index NEW docs into dedup_near"
    token_hash, token_count = matches[0][2], matches[0][3]
    assert token_hash is not None and token_count == 10
    # Distinct docs must store distinct token hashes.
    text2 = "one two three four five six seven eight nine eleven"
    cap2 = _make_cap("https://b.test/2", text2)
    eng.evaluate(cap2)
    rows2 = db.find_near_dup_candidate_rows(simhash128(text2))
    h2 = next(r[2] for r in rows2 if r[0] == cap2.doc_id)
    assert h2 != token_hash
