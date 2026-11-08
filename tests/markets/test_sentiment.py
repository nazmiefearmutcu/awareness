"""Financial sentiment: finance lexicon, FinBERT wrapper, entity-level sentiment."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from awareness.markets.models import EntityRef, SentimentScore
from awareness.markets.nlp import sentiment as sentiment_mod
from awareness.markets.nlp.lexicon import LEXICON_MODEL, LexiconScorer, load_lexicon
from awareness.markets.nlp.sentiment import SentimentScorer

LEX = LexiconScorer()


def _nvda() -> EntityRef:
    return EntityRef(type="equity", id="NVDA", name="Nvidia", confidence=0.95)


def _intc() -> EntityRef:
    return EntityRef(type="equity", id="INTC", name="Intel", confidence=0.95)


# ── lexicon ───────────────────────────────────────────────────────────────


def test_lexicon_positive_headline() -> None:
    s = LEX.score("Apple beats estimates and raises guidance")
    assert s.label == "positive"
    assert s.score > 0.5
    assert s.model == LEXICON_MODEL == "lexicon:aw-fin-1"


def test_lexicon_negative_headline() -> None:
    s = LEX.score("Company misses estimates, cuts guidance, announces layoffs")
    assert s.label == "negative"
    assert s.score < -0.5


def test_negation_flips_and_dampens() -> None:
    negated = LEX.score("Shares did not plunge")
    plain = LEX.score("Shares plunge")
    assert negated.score > 0  # flipped: a negative hit becomes positive
    assert abs(negated.score) < abs(plain.score)  # dampened by x0.6


def test_negation_does_not_cross_comma() -> None:
    assert LEX.score("Stock is not a bargain, layoffs announced").label == "negative"


def test_negation_does_not_cross_but() -> None:
    assert LEX.score("Not a bargain but layoffs announced").label == "negative"


def test_fails_to_negates_following_hit() -> None:
    s = LEX.score("Company fails to beat estimates")
    assert s.label != "positive"


def test_by_default_is_neutral() -> None:
    s = LEX.score("set to true by default")
    assert s.label == "neutral"
    assert s.score == 0.0


def test_no_hits_is_neutral_low_confidence() -> None:
    s = LEX.score("The company held its annual meeting on Tuesday in Austin.")
    assert s.label == "neutral"
    assert s.confidence == pytest.approx(0.3)


def test_intensifier_strengthens_hit() -> None:
    plain = LEX.score("Shares plunge after the call")
    sharp = LEX.score("Shares sharply plunge after the call")
    assert sharp.score < plain.score < 0


def test_turkish_positive_headline() -> None:
    assert LEX.score("Şirket beklentilerin üzerinde kâr açıkladı").label == "positive"


def test_turkish_negative_headline() -> None:
    assert LEX.score("Şirket iflas başvurusu yaptı").label == "negative"


def test_empty_text_is_neutral_zero() -> None:
    for text in ("", "   \n\t "):
        s = LEX.score(text)
        assert (s.label, s.score, s.confidence) == ("neutral", 0.0, 0.0)


def test_scores_stay_within_bounds() -> None:
    texts = [
        "record high record high record high surge soar rally beats raised guidance",
        "bankruptcy fraud default layoffs plunge sharply crash collapse iflas",
        "not not not never without plunge fraud bankruptcy",
    ]
    for text in texts:
        s = LEX.score(text)
        assert -1.0 <= s.score <= 1.0
        assert 0.0 <= s.confidence <= 1.0


def test_lexicon_has_enough_terms_each_side() -> None:
    lex = load_lexicon()
    assert len(lex.positive) >= 150
    assert len(lex.negative) >= 150


# ── scorer facade ─────────────────────────────────────────────────────────


def test_lexicon_backend_reports_lexicon() -> None:
    scorer = SentimentScorer("lexicon")
    assert scorer.backend_name == "lexicon"
    assert scorer.model == "lexicon:aw-fin-1"
    assert scorer.score("Stock surges on record high").label == "positive"


def test_lexicon_score_many_matches_single() -> None:
    scorer = SentimentScorer("lexicon")
    texts = ["Apple beats estimates", "Bank faces fraud probe", ""]
    assert scorer.score_many(texts) == [scorer.score(t) for t in texts]


class _FakeConfig:
    def __init__(self) -> None:
        self.id2label = {0: "positive", 1: "negative", 2: "neutral"}


class _FakeModel:
    def __init__(self) -> None:
        self.config = _FakeConfig()

    def eval(self) -> _FakeModel:
        return self


@pytest.fixture(autouse=True)
def _fresh_finbert_cache() -> Iterator[None]:
    """Keep monkeypatched FinBERT loaders from leaking through the process-wide cache."""
    sentiment_mod.clear_finbert_cache()
    yield
    sentiment_mod.clear_finbert_cache()


def _fake_finbert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in for the real loader: no weights, no network, no torch."""
    monkeypatch.setattr(sentiment_mod, "_load_finbert_model", lambda _name, **_kw: (None, _FakeModel()))


def _oom(self: object, texts: object, model_name: str) -> None:
    raise RuntimeError("CUDA out of memory")


def test_auto_uses_finbert_when_loadable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert(monkeypatch)
    scorer = SentimentScorer("auto")
    assert scorer.backend_name == "finbert"
    assert scorer.model == "finbert:ProsusAI/finbert"


def test_auto_falls_back_to_lexicon_when_finbert_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(*_args: object, **_kwargs: object) -> None:
        raise OSError("model not cached")

    monkeypatch.setattr(sentiment_mod, "_load_finbert_model", _missing)
    scorer = SentimentScorer("auto")
    assert scorer.backend_name == "lexicon"
    assert scorer.model == "lexicon:aw-fin-1"
    assert scorer.score("Stock plunges on fraud").label == "negative"


def test_finbert_backend_raises_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(*_args: object, **_kwargs: object) -> None:
        raise OSError("model not cached")

    monkeypatch.setattr(sentiment_mod, "_load_finbert_model", _missing)
    with pytest.raises(RuntimeError):
        SentimentScorer("finbert")


def test_auto_inference_error_falls_back_for_that_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_finbert(monkeypatch)
    monkeypatch.setattr(sentiment_mod._FinBertRuntime, "score", _oom)
    scorer = SentimentScorer("auto")
    with caplog.at_level("WARNING"):
        first = scorer.score("Apple beats estimates")
        second = scorer.score("Apple beats estimates")
    assert first.model == "lexicon:aw-fin-1"
    assert first.label == "positive"
    assert second.model == "lexicon:aw-fin-1"
    # One failure is not permanent, and the warning is logged once.
    assert scorer.backend_name == "finbert"
    warnings = [r for r in caplog.records if "inference failed" in r.getMessage()]
    assert len(warnings) == 1


def test_auto_switches_to_lexicon_after_three_inference_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert(monkeypatch)
    monkeypatch.setattr(sentiment_mod._FinBertRuntime, "score", _oom)
    scorer = SentimentScorer("auto")
    for _ in range(3):
        assert scorer.score("Bank faces fraud probe").model == "lexicon:aw-fin-1"
    assert scorer.backend_name == "lexicon"
    assert scorer.model == "lexicon:aw-fin-1"


def test_finbert_mode_raises_on_inference_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert(monkeypatch)
    monkeypatch.setattr(sentiment_mod._FinBertRuntime, "score", _oom)
    scorer = SentimentScorer("finbert")
    with pytest.raises(RuntimeError, match="out of memory"):
        scorer.score("Apple beats estimates")


# ── entity surface forms and clause attribution ───────────────────────────


def test_lowercase_ticker_word_is_not_an_entity_mention() -> None:
    on = EntityRef(type="equity", id="ON", name="ON Semiconductor", confidence=0.9)
    out = SentimentScorer("lexicon").entity_sentiment("Markets rally on strong data", None, [on])
    assert "ON" not in out


def test_pronoun_it_is_not_a_ticker_mention() -> None:
    it = EntityRef(type="equity", id="IT", name="Gartner", confidence=0.9)
    out = SentimentScorer("lexicon").entity_sentiment(
        "Shares plunge after weak outlook",
        "It is a bankruptcy risk for many firms today.",
        [it],
    )
    assert "IT" not in out


def test_name_mention_is_case_insensitive() -> None:
    on = EntityRef(type="equity", id="ON", name="ON Semiconductor", confidence=0.9)
    out = SentimentScorer("lexicon").entity_sentiment("on semiconductor beats estimates", None, [on])
    assert out["ON"].label == "positive"


def test_comma_appositive_keeps_the_predicate() -> None:
    out = SentimentScorer("lexicon").entity_sentiment(
        "Nvidia, the chipmaker, soars while Intel slumps after weak outlook.",
        None,
        [_nvda(), _intc()],
    )
    assert out["NVDA"].label == "positive"
    assert out["INTC"].label == "negative"


def test_mention_order_attribution_without_clause_break() -> None:
    out = SentimentScorer("lexicon").entity_sentiment(
        "Intel slumps after Nvidia record revenue beats.", None, [_nvda(), _intc()]
    )
    assert out["INTC"].label == "negative"
    assert out["NVDA"].label == "positive"


@pytest.mark.parametrize(
    ("headline", "nvda_label", "intc_label"),
    [
        ("Nvidia soars on record revenue while Intel slumps after weak outlook.", "positive", "negative"),
        ("Analysts cut Intel rating while Nvidia rallies.", "positive", "negative"),
        ("Weak outlook hits Intel while Nvidia rallies.", "positive", "negative"),
        ("Nvidia, the chipmaker, soars while Intel slumps after weak outlook.", "positive", "negative"),
        ("Intel shares slump as Nvidia stock surges to record.", "positive", "negative"),
        ("Nvidia stock surges to record as Intel shares slump.", "positive", "negative"),
        ("Intel slumps after Nvidia record revenue beats.", "positive", "negative"),
    ],
)
def test_predicate_on_either_side_of_mention_is_kept(headline: str, nvda_label: str, intc_label: str) -> None:
    out = SentimentScorer("lexicon").entity_sentiment(headline, None, [_nvda(), _intc()])
    assert out["NVDA"].label == nvda_label
    assert out["INTC"].label == intc_label


def test_words_between_mentions_belong_to_one_side() -> None:
    pieces = sentiment_mod._sentence_pieces(
        "Intel shares slump as Nvidia stock surges to record.",
        {"NVDA": sentiment_mod._surface_pattern(_nvda()), "INTC": sentiment_mod._surface_pattern(_intc())},
    )
    assert pieces == {"INTC": "Intel shares slump", "NVDA": "Nvidia stock surges to record."}


def test_gapped_rules_require_analyst_or_price_target_object() -> None:
    assert LEX.score("Oil falls after OPEC raised output target").label == "negative"
    assert LEX.score("Company cuts emissions target").label == "neutral"
    assert LEX.score("Analysts cut Intel rating").label == "negative"
    assert LEX.score("Analysts raised price target on Nvidia").label == "positive"


def test_oil_entity_not_flipped_by_output_target() -> None:
    oil = EntityRef(type="commodity", id="CL", name="Oil", confidence=0.9)
    out = SentimentScorer("lexicon").entity_sentiment(
        "Oil falls after OPEC raised output target", None, [oil]
    )
    assert out["CL"].label == "negative"


# ── entity sentiment ──────────────────────────────────────────────────────


def test_entity_sentiment_splits_clauses_per_entity() -> None:
    scorer = SentimentScorer("lexicon")
    out = scorer.entity_sentiment(
        "Nvidia soars on record revenue while Intel slumps after weak outlook.",
        None,
        [_nvda(), _intc()],
    )
    assert out["NVDA"].label == "positive"
    assert out["INTC"].label == "negative"
    assert out["NVDA"].model == "lexicon:aw-fin-1"


def test_entity_sentiment_omits_unmentioned_entities() -> None:
    scorer = SentimentScorer("lexicon")
    oracle = EntityRef(type="equity", id="ORCL", name="Oracle", confidence=0.9)
    out = scorer.entity_sentiment("Nvidia soars on record revenue", "", [_nvda(), oracle])
    assert set(out) == {"NVDA"}


def test_entity_sentiment_matches_dollar_ticker_in_text() -> None:
    scorer = SentimentScorer("lexicon")
    text = "Markets opened quietly on Monday. $NVDA shares plunge after an investigation into accounting."
    out = scorer.entity_sentiment("Chip stocks move", text, [_nvda()], max_sentences_per_entity=4)
    assert out["NVDA"].label == "negative"


def test_entity_sentiment_is_json_safe() -> None:
    out = SentimentScorer("lexicon").entity_sentiment("Nvidia soars", None, [_nvda()])
    payload = {k: v.model_dump() for k, v in out.items()}
    assert json.loads(json.dumps(payload))["NVDA"]["label"] == "positive"
    assert isinstance(out["NVDA"], SentimentScore)


# ── FinBERT (slow: needs the ProsusAI/finbert weights in the local HF cache) ──


def _finbert_cached() -> bool:
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from transformers import AutoConfig  # noqa: PLC0415

    try:
        AutoConfig.from_pretrained("ProsusAI/finbert", local_files_only=True)
    except OSError:
        return False
    return True


@pytest.mark.slow
def test_finbert_labels_and_batch_consistency() -> None:
    if not _finbert_cached():
        pytest.skip("ProsusAI/finbert not in the local Hugging Face cache")
    scorer = SentimentScorer("finbert")
    assert scorer.backend_name == "finbert"
    assert scorer.model == "finbert:ProsusAI/finbert"

    pos_text = "Company reports record profit and raises full-year guidance"
    neg_text = "Company files for bankruptcy after massive fraud losses"
    pos, neg = scorer.score(pos_text), scorer.score(neg_text)
    assert pos.label == "positive"
    assert neg.label == "negative"

    batch = scorer.score_many([pos_text, "", neg_text])
    assert batch[1] == SentimentScore(label="neutral", score=0.0, confidence=0.0, model=scorer.model)
    assert batch[0].score == pytest.approx(pos.score, abs=1e-4)
    assert batch[2].score == pytest.approx(neg.score, abs=1e-4)


# ── language routing and lexicon veto (final review wave B) ───────────────


def _fake_finbert_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the fake model and make every FinBERT verdict positive, whatever the text."""
    _fake_finbert(monkeypatch)

    def _always_positive(self: object, texts: object, model_name: str) -> list[SentimentScore]:
        return [SentimentScore(label="positive", score=0.8, confidence=0.9, model=model_name)
                for _ in texts]  # type: ignore[attr-defined]

    monkeypatch.setattr(sentiment_mod._FinBertRuntime, "score", _always_positive)


def test_score_many_routes_turkish_to_lexicon(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert_positive(monkeypatch)
    scorer = SentimentScorer("auto")
    tr_text = "Şirket iflas başvurusu yaptı"
    en_text = "Company beats estimates"
    out = scorer.score_many([tr_text, en_text], ["tr", "en"])
    assert out[0].model == LEXICON_MODEL
    assert out[0].label == "negative"
    assert out[1].model == "finbert:ProsusAI/finbert"
    assert out[1].label == "positive"


def test_score_many_without_languages_is_english(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert_positive(monkeypatch)
    scorer = SentimentScorer("auto")
    assert scorer.score_many(["Company beats estimates"])[0].model == "finbert:ProsusAI/finbert"


def test_score_many_language_length_must_match() -> None:
    with pytest.raises(ValueError):
        SentimentScorer("lexicon").score_many(["a text here"], ["en", "tr"])


def test_entity_sentiment_language_uses_lexicon(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert_positive(monkeypatch)
    scorer = SentimentScorer("auto")
    result = scorer.entity_sentiment(
        "Nvidia iflas başvurusu yaptı", None, [_nvda()], language="tr",
    )
    assert result["NVDA"].model == LEXICON_MODEL
    assert result["NVDA"].label == "negative"


def test_finbert_positive_is_vetoed_by_strong_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert_positive(monkeypatch)
    scorer = SentimentScorer("auto")
    out = scorer.score("Private credit defaults record largest quarterly jump")
    assert out.label == "negative"
    assert out.model == "finbert+lexicon-veto:ProsusAI/finbert"


def test_by_default_is_not_a_strong_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert_positive(monkeypatch)
    scorer = SentimentScorer("auto")
    out = scorer.score("Index funds are enrolled by default and inflows remain strong")
    assert out.label == "positive"
    assert out.model == "finbert:ProsusAI/finbert"


def test_negated_strong_negative_does_not_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_finbert_positive(monkeypatch)
    scorer = SentimentScorer("auto")
    out = scorer.score("Audit found no fraud and shares rallied")
    assert out.label == "positive"
    assert out.model == "finbert:ProsusAI/finbert"


def test_lexicon_only_backend_never_reports_veto() -> None:
    out = SentimentScorer("lexicon").score("Private credit defaults record largest quarterly jump")
    assert out.model == LEXICON_MODEL
