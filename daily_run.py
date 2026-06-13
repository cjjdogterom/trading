#!/usr/bin/env python3
"""Dagelijkse run: nieuws ophalen -> sentiment scoren -> signalen -> (optioneel) traden.

Gebruik via de CLI:  python cli.py run [--execute] [...]
Of direct (cron):    python daily_run.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from trailing_stop import Broker, load_settings
from storage import DB
from research import load_watchlist, score_symbol
from research.news import fetch_news, make_news_client
from strategy import Executor, RiskLimits, generate_signal

log = logging.getLogger("trailing_stop")


def run_daily(
    *,
    execute: bool = False,
    allow_live: bool = False,
    force_live: bool | None = None,
    limits: RiskLimits | None = None,
    days_back: int = 2,
) -> list[dict]:
    settings = load_settings(force_live=force_live)
    broker = Broker(settings)
    db = DB()
    news_client = make_news_client(settings)
    limits = limits or RiskLimits()
    symbols = load_watchlist()

    log.info("Dagelijkse run | account=%s | execute=%s | %d symbolen",
             settings.account_type, execute, len(symbols))

    executor = Executor(broker, db, limits, execute=execute, allow_live=allow_live)
    if not executor.preflight():
        log.warning("Preflight gehalt — alleen signalen, geen orders.")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    results: list[dict] = []

    for symbol in symbols:
        try:
            # 1) nieuws ophalen + opslaan
            rows = fetch_news(news_client, symbol, days_back=days_back)
            db.upsert_news(rows)

            # 2) sentiment scoren over recent nieuws
            articles = [dict(r) for r in db.news_since(symbol, since)]
            sent = score_symbol(symbol, articles)
            db.upsert_sentiment({
                "symbol": symbol, "day": today, "score": sent.score, "label": sent.label,
                "confidence": sent.confidence, "rationale": sent.rationale,
                "n_articles": sent.n_articles, "engine": sent.engine,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

            # 3) signaal + 4) uitvoeren/loggen
            signal = generate_signal(broker, symbol, sent.score)
            res = executor.handle(signal, sent)
            res["sentiment_label"] = sent.label
            results.append(res)
            log.info("%-6s %-6s trend=%+.2f sent=%+.2f (%s) | %s",
                     symbol, signal.action, signal.trend, sent.score, sent.label, res["reason"])
        except Exception as exc:
            log.error("%-6s overgeslagen: %s", symbol, exc)
            results.append({"symbol": symbol, "action": "error", "reason": str(exc)})

    placed = sum(1 for r in results if r.get("placed"))
    buys = sum(1 for r in results if r.get("action") == "buy")
    log.info("Klaar. %d koopsignalen, %d orders geplaatst (mode=%s).",
             buys, placed, executor.mode)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    run_daily(execute=False)
