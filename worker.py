#!/usr/bin/env python3
"""Always-on worker: handelt de hele beursdag door op de Nasdaq-100.

Werking per cyclus (default elke 15 min tijdens beursuren):
  1. (1×/dag) nieuws + sentiment verversen voor het hele universum.
  2. Trend/momentum + ATR per aandeel berekenen uit één gebatchte bars-call.
  3. Koopkandidaten rangschikken (trend + sentiment), binnen de risicolimieten
     de beste namen kopen tot de vrije posities vol zijn.
  4. Elke entry krijgt een ATR-gebaseerde (volatiliteit-adaptieve) trailing stop,
     begrensd door min/max — de "agent stelt de strategie af" maar binnen kaders.
  5. Exits lopen 24/5 server-side via die native trailing stops.

De kill-switch en alle limieten gelden onverkort. Paper is default; live vereist
EXECUTE=true, een AK-key (ALPACA_PAPER=false) én ALLOW_LIVE_AUTOTRADE=true.

Draai dit op een altijd-aan host (Railway/Fly/Render/VPS):  python worker.py
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame

from trailing_stop import Broker, load_settings
from storage import DB
from research import score_symbol
from research.news import fetch_news, make_news_client
from research.universe import load_universe
from strategy import Executor, RiskLimits, position_size, trend_from_closes, valuation
from daily_run import _write_snapshot, _truthy

log = logging.getLogger("trailing_stop")

# Afstemming (defaults; via env aanpasbaar).
SIGNAL_INTERVAL_MIN = int(os.getenv("SIGNAL_INTERVAL_MIN", "15"))
W_TREND, W_SENT = 0.6, 0.4          # gewichten in de gecombineerde score
TREND_MIN = 0.4                     # minimale trend voor een kandidaat
SENTIMENT_MIN = 0.1                 # minimale sentiment-score
BARS_LOOKBACK_DAYS = 260            # ~1 jaar: genoeg voor SMA50, ATR, RSI en 52w-range


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trail_price(price: float, atr: float, limits) -> float:
    """ATR-trail in $, begrensd op [min_trail_pct, max_trail_pct] van de koers."""
    t = atr * limits.atr_mult
    t = max(price * limits.min_trail_pct / 100, min(t, price * limits.max_trail_pct / 100))
    return round(t, 2)


def _atr_from_bars(bars, period: int = 14) -> float:
    bars = bars[-(period + 1):]
    if len(bars) < 2:
        return 0.0
    trs, prev_close = [], float(bars[0].close)
    for b in bars[1:]:
        h, l, c = float(b.high), float(b.low), float(b.close)
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = c
    return sum(trs) / len(trs)


def refresh_sentiment(broker, db, news_client, universe) -> None:
    """1×/dag: nieuws ophalen + sentiment scoren voor het hele universum."""
    log.info("Sentiment verversen voor %d symbolen…", len(universe))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for i, sym in enumerate(universe, 1):
        try:
            db.upsert_news(fetch_news(news_client, sym, days_back=2))
            sent = score_symbol(sym, [dict(r) for r in db.news_since(sym, since)])
            db.upsert_sentiment({
                "symbol": sym, "day": today, "score": sent.score, "label": sent.label,
                "confidence": sent.confidence, "rationale": sent.rationale,
                "n_articles": sent.n_articles, "engine": sent.engine,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            log.warning("Sentiment %s mislukt: %s", sym, exc)
        if i % 20 == 0:
            log.info("  …%d/%d", i, len(universe))
    log.info("Sentiment klaar.")


def run_cycle(broker, db, executor, limits, universe) -> None:
    """Eén cyclus: ladders beheren (goedkoop bijkopen op dips) + nieuwe posities openen."""
    if not executor.preflight():
        log.warning("Kill-switch actief — geen nieuwe entries deze cyclus.")
        return

    # Eén gebatchte bars-call + één latest-trade-call voor het hele universum.
    start = datetime.now(timezone.utc) - timedelta(days=BARS_LOOKBACK_DAYS)
    bars = broker.data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=universe, timeframe=TimeFrame.Day, start=start,
        feed=broker.settings.data_feed)).data
    try:
        trades = broker.data.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=universe, feed=broker.settings.data_feed))
    except Exception:
        trades = {}
    sentiment = {r["symbol"]: r for r in (dict(x) for x in db.latest_sentiment())}

    # Features per aandeel: trend, RSI/waardering ('goedkoop/duur'), ATR, koers, sentiment.
    feats = {}
    for sym in universe:
        sb = bars.get(sym, [])
        closes = [float(b.close) for b in sb]
        if len(closes) < 20:
            continue
        trend, last_close, _ = trend_from_closes(closes)
        rsi, _pos, val_label = valuation(closes)
        s = sentiment.get(sym, {})
        feats[sym] = {
            "trend": trend, "rsi": rsi, "val": val_label,
            "atr": _atr_from_bars(sb, limits.atr_period),
            "price": float(getattr(trades.get(sym), "price", 0) or last_close),
            "sent": float(s.get("score", 0.0)), "conf": float(s.get("confidence", 0.0)),
        }

    _manage_ladders(db, executor, limits, feats)   # 1) bijkopen op dips
    _open_new(db, executor, limits, feats)          # 2) nieuwe posities (eerste rung)
    _write_snapshot(db, mode=executor.mode)


def _manage_ladders(db, executor, limits, feats) -> None:
    """Goedkoop bijkopen op dips — zolang de uptrend intact is en boven de trailing stop."""
    held = executor._open_symbols
    for lad in db.active_ladders():
        sym = lad["symbol"]
        if sym not in held:
            db.deactivate_ladder(sym)                 # uitgestopt → ladder sluiten
            continue
        if lad["rungs_filled"] >= lad["rungs_total"]:
            db.deactivate_ladder(sym)                 # volledig opgebouwd
            continue
        f = feats.get(sym)
        if not f or f["trend"] < TREND_MIN:
            continue                                  # uptrend weg → niet bijkopen
        if f["price"] > lad["last_fill"] * (1 - limits.ladder_step_pct / 100):
            continue                                  # nog niet genoeg gezakt
        qty = int(lad["rung_qty"])
        if qty < 1:
            continue
        rung = lad["rungs_filled"] + 1
        if executor.execute:
            executor.add_rung(sym, qty, f["price"], trail_price=_trail_price(f["price"], f["atr"], limits))
            db.upsert_ladder({"symbol": sym, "rungs_total": lad["rungs_total"], "rungs_filled": rung,
                              "last_fill": f["price"], "rung_qty": lad["rung_qty"],
                              "active": 1 if rung < lad["rungs_total"] else 0, "updated_at": _now_iso()})
        else:
            log.info("[advisory] zou %s bijkopen +%s @ %.2f (rung %d/%d, %s)",
                     sym, qty, f["price"], rung, lad["rungs_total"], f["val"])


def _open_new(db, executor, limits, feats) -> None:
    """Nieuwe posities: eerste rung op uptrend + sentiment + niet-overbought ('niet te duur')."""
    slots = limits.max_open_positions - executor._open_count
    cands = []
    for sym, f in feats.items():
        if sym in executor._open_symbols or f["price"] <= 0:
            continue
        if f["trend"] < TREND_MIN or f["sent"] < SENTIMENT_MIN or f["conf"] < limits.min_confidence:
            continue
        if f["rsi"] > limits.rsi_max_entry:           # overbought/te duur → overslaan
            continue
        cands.append((W_TREND * f["trend"] + W_SENT * f["sent"], sym, f))
    cands.sort(reverse=True)
    log.info("Cyclus: %d nieuwe kandidaten, %d vrije posities, equity $%.0f.",
             len(cands), max(0, slots), executor._equity)

    for combined, sym, f in cands:
        if slots <= 0:
            break
        target_value = executor._equity * limits.max_position_pct / 100
        rung_qty = int((target_value / max(1, limits.ladder_rungs)) // f["price"])
        if rung_qty < 1:
            continue
        trail = _trail_price(f["price"], f["atr"], limits)
        if executor.execute:
            executor.buy(sym, rung_qty, f["price"], trail_price=trail)
            db.upsert_ladder({"symbol": sym, "rungs_total": limits.ladder_rungs, "rungs_filled": 1,
                              "last_fill": f["price"], "rung_qty": rung_qty,
                              "active": 1 if limits.ladder_rungs > 1 else 0, "updated_at": _now_iso()})
        else:
            log.info("[advisory] zou %s x%s @ %.2f kopen (rung 1/%d, %s, RSI %.0f, trail $%.2f)",
                     sym, rung_qty, f["price"], limits.ladder_rungs, f["val"], f["rsi"], trail)
        slots -= 1


def _sleep_until_open(broker) -> bool:
    """True als de markt open is; anders slaapt het tot (max 30 min vóór) de opening."""
    clock = broker.trading.get_clock()
    if clock.is_open:
        return True
    now = datetime.now(timezone.utc)
    secs = (clock.next_open - now).total_seconds() if clock.next_open else 1800
    nap = max(60, min(secs, 1800))
    log.info("Markt dicht. Slaap %.0f min (volgende open: %s).", nap / 60, clock.next_open)
    time.sleep(nap)
    return False


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    settings = load_settings()
    broker = Broker(settings)
    db = DB()
    news_client = make_news_client(settings)
    universe = load_universe()
    limits = RiskLimits(
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", "5")),
        max_open_positions=int(os.getenv("MAX_POSITIONS", "10")),
        max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS", "3")),
        atr_mult=float(os.getenv("ATR_MULT", "3")),
        min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.4")),
        ladder_rungs=int(os.getenv("LADDER_RUNGS", "3")),
        ladder_step_pct=float(os.getenv("LADDER_STEP_PCT", "4")),
        rsi_max_entry=float(os.getenv("RSI_MAX_ENTRY", "70")),
    )
    execute = _truthy(os.getenv("EXECUTE"))
    allow_live = _truthy(os.getenv("ALLOW_LIVE_AUTOTRADE"))
    executor = Executor(broker, db, limits, execute=execute, allow_live=allow_live)

    log.info("WORKER gestart | account=%s | mode=%s | universum=%d | interval=%dmin",
             settings.account_type, executor.mode, len(universe), SIGNAL_INTERVAL_MIN)

    last_sentiment_day = None
    while True:
        try:
            if not _sleep_until_open(broker):
                continue
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if last_sentiment_day != today:
                refresh_sentiment(broker, db, news_client, universe)
                last_sentiment_day = today
            run_cycle(broker, db, executor, limits, universe)
        except KeyboardInterrupt:
            log.info("Worker gestopt (Ctrl-C). Native trailing stops blijven actief bij Alpaca.")
            break
        except Exception as exc:
            log.error("Cyclus-fout: %s", exc)
        time.sleep(SIGNAL_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
