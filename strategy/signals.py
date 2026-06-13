"""Swing-trading signalen: combineert trend/momentum met nieuwssentiment.

Filosofie (eerlijk): dit is geen voorspelling van de toekomst. We handelen
alleen mét de trend (de trend is je vriend) en gebruiken sentiment als extra
bevestiging. De échte bescherming zit in het risicobeheer en de trailing stop,
niet in het signaal zelf.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


@dataclass
class Signal:
    symbol: str
    action: str          # "buy" | "hold" | "avoid"
    trend: float         # -1..1
    sentiment: float     # -1..1
    price: float
    reason: str


def _closes(broker, symbol: str, n: int = 60) -> list[float]:
    # Alpaca geeft met alleen `limit` (zonder start) 0 dagbars terug; daarom
    # vragen we op startdatum en houden we de laatste n over (×1.7 voor weekends).
    start = datetime.now(timezone.utc) - timedelta(days=int(n * 1.7) + 10)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        feed=broker.settings.data_feed,
    )
    bars = broker.data.get_stock_bars(req).data.get(symbol, [])
    return [float(b.close) for b in bars][-n:]


def _sma(values: list[float], window: int) -> float:
    window = min(window, len(values))
    return sum(values[-window:]) / window


def trend_from_closes(closes: list[float]) -> tuple[float, float, str]:
    """Trend/momentum-score in [-1, 1] uit een reeks slotkoersen, plus laatste
    koers en uitleg. Opbouw: 40% prijs vs SMA20, 30% SMA20 vs SMA50, 30% 20d-momentum.
    Pure functie — geen netwerk; werkt met losse én gebatchte bars."""
    if len(closes) < 20:
        return 0.0, (closes[-1] if closes else 0.0), "Te weinig historie."
    price = closes[-1]
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    momentum = price / closes[-20] - 1.0  # 20-daags rendement

    def clamp(x: float) -> float:
        return max(-1.0, min(1.0, x))

    s = (
        0.40 * (1 if price > sma20 else -1)
        + 0.30 * (1 if sma20 > sma50 else -1)
        + 0.30 * clamp(momentum / 0.10)  # 10% momentum = volledige tilt
    )
    reason = (
        f"koers {price:.2f} {'>' if price > sma20 else '<'} SMA20 {sma20:.2f}; "
        f"SMA20 {'>' if sma20 > sma50 else '<'} SMA50 {sma50:.2f}; "
        f"20d-momentum {momentum * 100:+.1f}%"
    )
    return round(clamp(s), 3), price, reason


def trend_score(broker, symbol: str) -> tuple[float, float, str]:
    """Trend/momentum voor één symbool (haalt zelf de bars op)."""
    return trend_from_closes(_closes(broker, symbol))


def rsi_from_closes(closes: list[float], period: int = 14) -> float:
    """RSI (Relative Strength Index) — <30 oversold ('goedkoop'), >70 overbought ('duur')."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))][-period:]
    avg_gain = sum(d for d in deltas if d > 0) / period
    avg_loss = sum(-d for d in deltas if d < 0) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def valuation(closes: list[float]) -> tuple[float, float, str]:
    """Is het aandeel nu 'goedkoop' of 'duur'? Op basis van historische koersen.

    Geeft (rsi, positie-in-range%, label). positie-in-range: 0 = jaarbodem, 100 = jaartop.
    """
    if not closes:
        return 50.0, 50.0, "neutraal"
    rsi = rsi_from_closes(closes)
    window = closes[-252:]  # ~1 jaar handelsdagen (zoveel als beschikbaar)
    hi, lo, price = max(window), min(window), closes[-1]
    pos = (price - lo) / (hi - lo) * 100 if hi > lo else 50.0
    if rsi < 35 or pos < 25:
        label = "goedkoop"
    elif rsi > 70 or pos > 80:
        label = "duur"
    else:
        label = "neutraal"
    return round(rsi, 1), round(pos, 1), label


def generate_signal(
    broker,
    symbol: str,
    sentiment: float,
    *,
    trend_min: float = 0.4,
    sentiment_min: float = 0.2,
    sentiment_avoid: float = -0.4,
    trend_avoid: float = -0.4,
) -> Signal:
    trend, price, treason = trend_score(broker, symbol)

    if trend >= trend_min and sentiment >= sentiment_min:
        action = "buy"
        reason = f"Trend bullish ({trend:+.2f}) én sentiment positief ({sentiment:+.2f}). {treason}"
    elif trend <= trend_avoid or sentiment <= sentiment_avoid:
        action = "avoid"
        reason = f"Trend/sentiment negatief (trend {trend:+.2f}, sent {sentiment:+.2f}). {treason}"
    else:
        action = "hold"
        reason = f"Geen confluentie (trend {trend:+.2f}, sent {sentiment:+.2f}). {treason}"

    return Signal(symbol=symbol, action=action, trend=trend, sentiment=sentiment,
                  price=price, reason=reason)
