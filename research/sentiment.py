"""Bullish/bearish scoren van nieuws.

Twee engines:
  - claude:  gebruikt de Anthropic API (als ANTHROPIC_API_KEY is gezet).
  - lexicon: gratis financieel woordenboek (geen key, geen kosten).

available_engine() kiest automatisch claude als de key er is, anders lexicon.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("trailing_stop")

# Standaardmodel: per Anthropic-richtlijn Opus. Zet SENTIMENT_MODEL=claude-haiku-4-5
# om bulk-sentiment veel goedkoper te draaien.
DEFAULT_MODEL = "claude-opus-4-8"
MAX_ARTICLES = 25  # cap per symbool om tokens/kosten te beperken


@dataclass
class SentimentResult:
    score: float        # -1.0 (bearish) .. +1.0 (bullish)
    label: str          # bullish / bearish / neutral
    confidence: float   # 0..1
    rationale: str
    engine: str
    n_articles: int


def available_engine() -> str:
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "claude"
        except ImportError:
            log.warning("ANTHROPIC_API_KEY gezet maar 'anthropic' niet geïnstalleerd; val terug op lexicon.")
    return "lexicon"


def score_symbol(symbol: str, articles: list[dict], *, model: str | None = None) -> SentimentResult:
    """Scoor het sentiment voor één symbool op basis van zijn artikelen."""
    if not articles:
        return SentimentResult(0.0, "neutral", 0.0, "Geen recent nieuws.", "none", 0)

    if available_engine() == "claude":
        try:
            return _score_claude(symbol, articles, model=model or os.getenv("SENTIMENT_MODEL", DEFAULT_MODEL))
        except Exception as exc:
            log.warning("Claude-sentiment mislukt (%s); val terug op lexicon.", exc)
    return _score_lexicon(articles)


# --------------------------------------------------------------------------
# Claude-engine
# --------------------------------------------------------------------------

def _score_claude(symbol: str, articles: list[dict], *, model: str) -> SentimentResult:
    import anthropic
    from pydantic import BaseModel, Field
    from typing import Literal

    class _Score(BaseModel):
        score: float = Field(description="-1.0 = zeer bearish, 0 = neutraal, +1.0 = zeer bullish")
        label: Literal["bullish", "bearish", "neutral"]
        confidence: float = Field(description="0..1 hoe zeker")
        rationale: str = Field(description="Eén zin, in het Nederlands, waarom.")

    headlines = "\n".join(
        f"- {a['headline']}" + (f" — {a['summary'][:200]}" if a.get("summary") else "")
        for a in articles[:MAX_ARTICLES]
    )
    prompt = (
        f"Beoordeel het beleggerssentiment voor {symbol} op basis van deze recente "
        f"nieuwskoppen. Weeg materiële zaken (resultaten, guidance, deals, regelgeving) "
        f"zwaarder dan ruis. Geef een score van -1 (bearish) tot +1 (bullish).\n\n"
        f"Koppen:\n{headlines}"
    )

    client = anthropic.Anthropic()
    resp = client.messages.parse(
        model=model,
        max_tokens=1024,
        system="Je bent een nuchtere financiële nieuws-analist. Wees voorzichtig en "
               "objectief; hype is geen koopsignaal.",
        messages=[{"role": "user", "content": prompt}],
        output_format=_Score,
    )
    out = resp.parsed_output
    return SentimentResult(
        score=max(-1.0, min(1.0, float(out.score))),
        label=out.label,
        confidence=max(0.0, min(1.0, float(out.confidence))),
        rationale=out.rationale,
        engine=f"claude:{model}",
        n_articles=min(len(articles), MAX_ARTICLES),
    )


# --------------------------------------------------------------------------
# Lexicon-engine (gratis fallback)
# --------------------------------------------------------------------------

_BULLISH = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies", "gain",
    "gains", "jump", "jumps", "rise", "rises", "upgrade", "upgraded", "outperform",
    "record", "strong", "growth", "profit", "profits", "bullish", "buy", "boost",
    "boosts", "raise", "raised", "tops", "top", "win", "wins", "expand", "expands",
    "approval", "approved", "breakthrough", "optimistic", "rebound", "high",
}
_BEARISH = {
    "miss", "misses", "plunge", "plunges", "fall", "falls", "drop", "drops", "slump",
    "slumps", "decline", "declines", "downgrade", "downgraded", "underperform", "weak",
    "loss", "losses", "bearish", "sell", "cut", "cuts", "warn", "warning", "warns",
    "lawsuit", "probe", "investigation", "recall", "layoff", "layoffs", "fraud",
    "default", "bankruptcy", "slowdown", "concern", "concerns", "risk", "low", "fears",
}
_WORD = re.compile(r"[a-z']+")


def _score_lexicon(articles: list[dict]) -> SentimentResult:
    bull = bear = 0
    for a in articles[:MAX_ARTICLES]:
        text = f"{a.get('headline','')} {a.get('summary','')}".lower()
        for w in _WORD.findall(text):
            if w in _BULLISH:
                bull += 1
            elif w in _BEARISH:
                bear += 1
    total = bull + bear
    if total == 0:
        return SentimentResult(0.0, "neutral", 0.2,
                               "Geen duidelijke sentiment-woorden gevonden.", "lexicon",
                               min(len(articles), MAX_ARTICLES))
    score = (bull - bear) / total
    label = "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"
    confidence = min(1.0, total / 10.0)  # meer signaalwoorden -> meer zekerheid
    return SentimentResult(
        score=round(score, 3),
        label=label,
        confidence=round(confidence, 2),
        rationale=f"{bull} bullish vs {bear} bearish signaalwoorden in de koppen.",
        engine="lexicon",
        n_articles=min(len(articles), MAX_ARTICLES),
    )
