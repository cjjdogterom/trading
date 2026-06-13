"""Dunne wrapper rond de Alpaca clients met precies de calls die we nodig hebben."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopOrderRequest,
    TrailingStopOrderRequest,
)
from alpaca.trading.enums import OrderSide, PositionSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame

from .config import Settings


@dataclass
class PositionInfo:
    symbol: str
    qty: float          # altijd > 0
    side: PositionSide  # LONG of SHORT
    avg_entry_price: float
    current_price: float


class Broker:
    """Bundelt de trading- en data-client en biedt de calls die de engine gebruikt."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.trading = TradingClient(
            settings.api_key, settings.secret_key, paper=settings.paper
        )
        self.data = StockHistoricalDataClient(settings.api_key, settings.secret_key)

    # ---- account / markt ------------------------------------------------

    def is_market_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    # ---- marktdata ------------------------------------------------------

    def latest_price(self, symbol: str) -> float:
        req = StockLatestTradeRequest(
            symbol_or_symbols=symbol, feed=self.settings.data_feed
        )
        trade = self.data.get_stock_latest_trade(req)[symbol]
        return float(trade.price)

    def atr(self, symbol: str, period: int = 14, timeframe: TimeFrame | None = None) -> float:
        """Average True Range over `period` bars (default dagbars)."""
        tf = timeframe or TimeFrame.Day
        # Met alleen `limit` (zonder start) geeft Alpaca 0 dagbars; vraag op datum.
        start = datetime.now(timezone.utc) - timedelta(days=int((period + 1) * 1.7) + 10)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            feed=self.settings.data_feed,
        )
        bars = self.data.get_stock_bars(req).data.get(symbol, [])[-(period + 1):]
        if len(bars) < 2:
            raise RuntimeError(f"Te weinig bars om ATR te berekenen voor {symbol}.")
        trs = []
        prev_close = float(bars[0].close)
        for bar in bars[1:]:
            high, low, close = float(bar.high), float(bar.low), float(bar.close)
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
            prev_close = close
        return sum(trs) / len(trs)

    # ---- posities -------------------------------------------------------

    def get_position(self, symbol: str) -> PositionInfo | None:
        try:
            p = self.trading.get_open_position(symbol)
        except Exception:
            return None  # geen open positie
        return PositionInfo(
            symbol=p.symbol,
            qty=abs(float(p.qty)),
            side=p.side,
            avg_entry_price=float(p.avg_entry_price),
            current_price=float(p.current_price),
        )

    # ---- orders ---------------------------------------------------------

    def submit_native_trailing_stop(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        *,
        trail_percent: float | None = None,
        trail_price: float | None = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ):
        req = TrailingStopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
            trail_percent=trail_percent,
            trail_price=trail_price,
        )
        return self.trading.submit_order(req)

    def submit_stop(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        stop_price: float,
        *,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ):
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
            stop_price=round(stop_price, 2),
        )
        return self.trading.submit_order(req)

    def replace_stop(self, order_id, new_stop_price: float):
        return self.trading.replace_order_by_id(
            order_id, ReplaceOrderRequest(stop_price=round(new_stop_price, 2))
        )

    def submit_market(self, symbol: str, qty: float, side: OrderSide):
        """Markt-order (BUY om te openen, SELL om te sluiten)."""
        req = MarketOrderRequest(
            symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY
        )
        return self.trading.submit_order(req)

    def get_order(self, order_id):
        return self.trading.get_order_by_id(order_id)

    def cancel_order(self, order_id) -> None:
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception:
            pass  # al gevuld/geannuleerd
