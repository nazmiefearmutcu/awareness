"""Deterministic English financial-news sentiment lexicon.

Pure code — no data files. Two polarity word sets (:data:`POSITIVE`,
:data:`NEGATIVE`) sized for coverage of finance-flavored copy (markets,
earnings, M&A, regulation, macro), plus small modifier sets used by
:class:`~awareness.sentiment.engine.SentimentEngine`:

* :data:`NEGATIONS` — words within 3 tokens before a sentiment word flip
  its polarity ("not good" is negative).
* :data:`INTENSIFIERS` — words within 3 tokens before a sentiment word add
  ``+0.5`` weight ("very good" counts 1.5x).

All sets are lowercase, single-token (punctuation already stripped by the
engine's tokenizer) and pairwise disjoint so a token can never be scored
twice or in two directions.
"""

from __future__ import annotations

POSITIVE: frozenset[str] = frozenset(
    {
        # markets / price action
        "rally", "rallies", "rallied", "rallying",
        "surge", "surges", "surged", "surging",
        "soar", "soars", "soared", "soaring",
        "jump", "jumps", "jumped", "jumping",
        "rise", "rises", "rose", "rising",
        "gain", "gains", "gained", "gaining",
        "climb", "climbs", "climbed", "climbing",
        "advance", "advances", "advanced", "advancing",
        "ascent", "upswing", "upswings", "uptick", "upticks",
        "record", "records", "high", "highs", "milestone", "milestones",
        "breakout", "breakouts", "momentum", "rebound", "rebounds",
        "rebounded", "resurgence", "turnaround", "pickup",
        # results / forecasts
        "beat", "beats", "beating", "outperform", "outperforms",
        "outperformed", "outperforming", "exceed", "exceeds", "exceeded",
        "exceeding", "surpass", "surpasses", "surpassed", "surpassing",
        "boost", "boosts", "boosted", "boosting", "windfall", "windfalls",
        "bonanza", "boom", "booms", "booming", "banner",
        # fundamentals
        "growth", "grow", "grows", "growing", "grew", "expansion",
        "expanding", "expand", "expands", "profit", "profits", "profitable",
        "profitability", "earnings", "income", "surplus", "yield", "yields",
        "dividend", "dividends", "buyback", "buybacks", "shareholder",
        "shareholders", "return", "returns", "alpha",
        # sentiment tone
        "bullish", "optimism", "optimistic", "positive", "upside", "upbeat",
        "strong", "stronger", "strongest", "robust", "solid", "healthy",
        "resilient", "resilience", "buoyant", "constructive", "promising",
        "stellar", "exceptional", "impressive", "lucrative", "prosperous",
        "prosperity", "flourish", "flourishes", "flourished", "flourishing",
        "thrive", "thrives", "thriving", "confident", "confidence",
        "enthusiasm", "enthusiastic", "encouraging", "favorable", "favourable",
        # outcomes
        "win", "wins", "winning", "won", "winner", "winners", "victory",
        "victories", "breakthrough", "breakthroughs", "success", "successes",
        "successful", "succeed", "succeeds", "succeeded", "reward", "rewards",
        "rewarded", "improve", "improves", "improved", "improving",
        "improvement", "improvements", "good", "better", "best", "great",
        "excellent", "pleased", "approval", "approvals", "approved",
        "opportunity", "opportunities",
        # analyst / rating language
        "upgrade", "upgrades", "upgraded", "upgrading", "tailwind",
        "tailwinds", "acceleration", "accelerate", "accelerates",
        "accelerating", "outperformance",
    }
)

NEGATIVE: frozenset[str] = frozenset(
    {
        # markets / price action
        "slump", "slumps", "slumped", "slumping",
        "crash", "crashes", "crashed", "crashing",
        "plunge", "plunges", "plunged", "plunging",
        "tumble", "tumbles", "tumbled", "tumbling",
        "drop", "drops", "dropped", "dropping",
        "decline", "declines", "declined", "declining",
        "slide", "slides", "slid", "slipping", "slip", "slips",
        "wobble", "wobbles", "wobbled", "selloff", "selloffs",
        "downturn", "downturns", "dive", "dives", "dived", "nosedive",
        "nosedives", "nosedived", "collapse", "collapses", "collapsed",
        "collapsing", "burst", "bursts", "bursting", "bubble", "bubbles",
        "overvalued", "wipeout", "wiped", "rout", "routed", "carnage",
        "bloodbath", "massacre", "devastation", "devastated",
        # sentiment tone
        "bearish", "pessimistic", "pessimism", "fear", "fears", "fearful",
        "panic", "panics", "panicked", "panicking", "shock", "shocks",
        "worried", "worry", "worries", "weak", "weaker", "weakness",
        "weaknesses", "negative", "gloomy", "grim", "bleak", "dismal",
        "dire", "awful", "terrible", "bad", "worse", "worst", "troubled",
        "problem", "problems", "uncertainty", "uncertainties", "turmoil",
        "turbulence", "risk", "risks", "risky", "threat", "threats",
        "danger", "dangers", "dangerous", "fragile", "fragility",
        "volatile", "deteriorate", "deteriorates", "deteriorated",
        "deteriorating", "erosion", "eroding", "impaired", "impairment",
        # results / forecasts
        "miss", "misses", "missed", "disappointing", "disappointment",
        "disappointed", "disappoint", "fail", "fails", "failed", "failing",
        "failure", "failures", "loss", "losses", "lose", "loses", "losing",
        "lost", "writedown", "writedowns", "shortfall", "shortfalls",
        "shortage", "shortages", "backlog", "backlogs", "stumble",
        "stumbles", "stumbled", "stumbling", "falter", "faltered",
        "faltering", "setback", "setbacks", "squeeze", "squeezed",
        "strain", "strains", "strained", "pressure", "pressures",
        "pressured", "delays", "delay", "delayed", "delaying",
        # corporate action
        "cut", "cuts", "cutting", "layoff", "layoffs", "halt", "halts",
        "halted", "halting", "freeze", "freezes", "froze", "frozen",
        "freezing", "shutdown", "shutdowns", "suspension", "suspensions",
        "suspended", "suspend", "recall", "recalls", "recalled",
        "downgrade", "downgrades", "downgraded", "downgrading",
        # credit / solvency
        "debt", "debts", "deficit", "deficits", "bankruptcy", "bankrupt",
        "insolvent", "insolvency", "default", "defaults", "defaulted",
        "defaulting", "recession", "recessions", "stagflation",
        "contagion", "depression",
        # macro / policy
        "inflation", "tariffs", "tariff", "war", "wars", "sanctions",
        "sanction", "sanctioned", "violation", "violations", "embargo",
        "embargoes", "boycott", "boycotts",
        # legal / compliance
        "fraud", "fraudulent", "probe", "probes", "lawsuit", "lawsuits",
        "warning", "warnings", "warned", "penalty", "penalties",
        "fines", "fined", "charges", "charged", "indictment", "indicted",
        "guilty", "conviction", "convicted", "subpoena", "subpoenas",
        "defective", "contamination", "contaminated", "poison", "toxic",
    }
)

# Words within 3 tokens before a sentiment word flip its polarity.
NEGATIONS: frozenset[str] = frozenset(
    {"not", "no", "never", "without", "despite", "lacks"}
)

# Words within 3 tokens before a sentiment word add +0.5 weight.
INTENSIFIERS: frozenset[str] = frozenset(
    {"very", "extremely", "strongly", "sharply", "significantly"}
)
