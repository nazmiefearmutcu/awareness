"""Financial sentiment: finance lexicon, FinBERT wrapper, entity-level sentiment."""

from __future__ import annotations

import json

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
    s = LEX.score("Shares did not plunge")
    assert s.label != "negative"


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
    assert LEX.score("Şirket beklentilerin üzerinde kâr açıkladı").label == "positive"  # noqa: RUF001


def test_turkish_negative_headline() -> None:
    assert LEX.score("Şirket iflas başvurusu yaptı").label == "negative"  # noqa: RUF001


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


def test_auto_backend_returns_valid_model_name() -> None:
    scorer = SentimentScorer("auto")
    assert scorer.model in {"finbert:ProsusAI/finbert", "lexicon:aw-fin-1"}
    assert scorer.backend_name == ("finbert" if scorer.model.startswith("finbert") else "lexicon")


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
    try:
        sentiment_mod._load_finbert_model("ProsusAI/finbert", local_only=True)
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
