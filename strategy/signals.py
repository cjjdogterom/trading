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


def trend_score(broker, symbol: str) -> tuple[float, float, str]:
    """Trend/momentum-score in [-1, 1], plus de laatste koers en een uitleg.

    Opbouw: 40% prijs t.o.v. SMA20, 30% SMA20 t.o.v. SMA50, 30% 20-daags momentum.
    """
    closes = _closes(broker, symbol)
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
