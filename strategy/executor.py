"""Zet signalen om in orders — met meerdere vangrails en een live-grendel.

Modi:
  advisory : alleen signalen tonen/opslaan, GEEN orders (default).
  paper    : orders op je paper-account (oefengeld).
  live     : orders op je live-account. Geblokkeerd tenzij expliciet vrijgegeven.

Elke entry krijgt automatisch een trailing stop. De kill-switch stopt alles als
het dagverlies te groot wordt.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide

from trailing_stop.engine import place_native_trailing_stop
from .risk import RiskLimits, daily_loss_pct, position_size

log = logging.getLogger("trailing_stop")


class Executor:
    def __init__(self, broker, db, limits: RiskLimits, *,
                 execute: bool = False, allow_live: bool = False):
        self.broker = broker
        self.db = db
        self.limits = limits
        self.execute = execute
        self.live = not broker.settings.paper

        # ---- Live-grendel: auto-traden met echt geld kan niet per ongeluk ----
        if self.execute and self.live and not allow_live:
            raise RuntimeError(
                "Auto-trading op een LIVE account is geblokkeerd. Vereist: "
                "env ALLOW_LIVE_AUTOTRADE=true én de vlag --i-understand-live-risk."
            )

        self._halted = False
        self._open_symbols: set[str] = set()
        self._open_count = 0
        self._equity = 0.0

    @property
    def mode(self) -> str:
        if not self.execute:
            return "advisory"
        return "live" if self.live else "paper"

    # ---- vooraf: kill-switch + accountstatus ----------------------------

    def preflight(self) -> bool:
        acct = self.broker.trading.get_account()
        self._equity = float(acct.equity)
        dl = daily_loss_pct(acct)
        self._halted = dl <= -self.limits.max_daily_loss_pct  # elke cyclus opnieuw bepaald
        if self._halted:
            log.warning("🛑 KILL-SWITCH: dagverlies %.2f%% (limiet -%.2f%%). Geen nieuwe trades.",
                        dl, self.limits.max_daily_loss_pct)
        positions = self.broker.trading.get_all_positions()
        self._open_symbols = {p.symbol for p in positions}
        self._open_count = len(positions)
        return not self._halted

    # ---- per signaal ----------------------------------------------------

    def handle(self, signal, sentiment) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.db.add_signal({
            "symbol": signal.symbol, "day": today, "action": signal.action,
            "reason": signal.reason, "sentiment": signal.sentiment, "trend": signal.trend,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        res = {"symbol": signal.symbol, "action": signal.action, "mode": self.mode,
               "placed": False, "reason": signal.reason}

        if signal.action != "buy":
            return res

        # ---- vangrails vóór een koop ----
        block = self._blocker(signal, sentiment)
        if block:
            res["reason"] = block
            return res

        qty = position_size(self._equity, signal.price, self.limits.max_position_pct)
        if qty < 1:
            res["reason"] = "positiegrootte < 1 aandeel bij deze koers/limiet"
            return res
        res["qty"] = qty

        if not self.execute:
            res["reason"] = f"advisory: zou {qty}x {signal.symbol} kopen (geen order geplaatst)"
            return res

        self._buy_with_trailing_stop(signal, qty, today, res)
        return res

    def _blocker(self, signal, sentiment) -> str | None:
        if self._halted:
            return "kill-switch actief (dagverlies-limiet)"
        if sentiment.confidence < self.limits.min_confidence:
            return f"sentiment-zekerheid {sentiment.confidence:.2f} < {self.limits.min_confidence}"
        if signal.symbol in self._open_symbols:
            return "al een open positie"
        if self._open_count >= self.limits.max_open_positions:
            return f"max {self.limits.max_open_positions} posities bereikt"
        return None

    def buy(self, symbol: str, qty: float, price: float, *,
            trail_percent: float | None = None, trail_price: float | None = None) -> dict:
        """Market BUY + native trailing stop + trade vastleggen.

        Gaat uit van self.execute=True (de aanroeper beslist over advisory).
        Geef precies één van trail_percent of trail_price (bv. ATR-gebaseerd).
        De live-grendel zit al in __init__, dus dit kan niet ongemerkt live gaan.
        """
        res = {"symbol": symbol, "qty": qty, "placed": False, "mode": self.mode}
        order = self.broker.submit_market(symbol, qty, OrderSide.BUY)
        log.info("Order BUY %s x%s (%s, order %s).", symbol, qty, self.mode, order.id)
        res["placed"] = True
        res["order_id"] = str(order.id)

        filled = self._await_fill(order.id)
        if filled:
            try:
                place_native_trailing_stop(self.broker, symbol,
                                           trail_percent=trail_percent, trail_price=trail_price)
                res["trailing_stop"] = (f"{trail_percent}%" if trail_percent is not None
                                        else f"${trail_price}")
            except Exception as exc:
                log.warning("Trailing stop voor %s plaatsen mislukt: %s", symbol, exc)
                res["trailing_stop"] = f"MISLUKT: {exc}"
        else:
            log.warning("Order %s niet gevuld (markt dicht?). Trailing stop volgt later.", symbol)
            res["trailing_stop"] = "uitgesteld (niet gevuld)"

        self._open_symbols.add(symbol)
        self._open_count += 1
        self.db.add_trade({
            "symbol": symbol, "side": "buy", "qty": qty, "price": price,
            "order_id": str(order.id), "mode": self.mode,
            "status": "filled" if filled else "pending", "signal_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return res

    def add_rung(self, symbol: str, add_qty: float, price: float, *,
                 trail_price: float | None = None) -> dict:
        """Koop een extra stukje bij (ladder) en zet de trailing stop opnieuw voor
        de HELE positie. De oude stop wordt eerst geannuleerd (anders dubbel verkopen)."""
        res = {"symbol": symbol, "added": add_qty, "placed": False, "mode": self.mode}
        order = self.broker.submit_market(symbol, add_qty, OrderSide.BUY)
        log.info("LADDER bijkopen %s +%s @ %.2f (%s).", symbol, add_qty, price, self.mode)
        res["placed"] = True
        if self._await_fill(order.id):
            self.broker.cancel_open_orders(symbol)               # oude trailing stop weg
            try:
                place_native_trailing_stop(self.broker, symbol, trail_price=trail_price)
                res["trailing_stop"] = f"${trail_price} (volledige positie)"
            except Exception as exc:
                log.warning("Trailing stop herzetten %s mislukt: %s", symbol, exc)
        self.db.add_trade({
            "symbol": symbol, "side": "buy", "qty": add_qty, "price": price,
            "order_id": str(order.id), "mode": self.mode, "status": "ladder_add",
            "signal_id": None, "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return res

    def _buy_with_trailing_stop(self, signal, qty, today, res) -> None:
        res.update(self.buy(signal.symbol, qty, signal.price,
                            trail_percent=self.limits.trail_percent))

    def _await_fill(self, order_id, *, tries: int = 8, delay: float = 1.0) -> bool:
        for _ in range(tries):
            try:
                o = self.broker.get_order(order_id)
            except Exception:
                return False
            status = getattr(getattr(o, "status", ""), "value", "")
            if status == "filled":
                return True
            if status in {"canceled", "rejected", "expired"}:
                return False
            time.sleep(delay)
        return False
