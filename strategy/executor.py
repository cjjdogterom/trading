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
        if dl <= -self.limits.max_daily_loss_pct:
            log.warning("🛑 KILL-SWITCH: dagverlies %.2f%% (limiet -%.2f%%). Geen nieuwe trades.",
                        dl, self.limits.max_daily_loss_pct)
            self._halted = True
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

    def _buy_with_trailing_stop(self, signal, qty, today, res) -> None:
        order = self.broker.submit_market(signal.symbol, qty, OrderSide.BUY)
        log.info("Order BUY %s x%s ingelegd (%s, order %s).",
                 signal.symbol, qty, self.mode, order.id)
        res["placed"] = True
        res["order_id"] = str(order.id)

        filled = self._await_fill(order.id)
        if filled:
            try:
                place_native_trailing_stop(
                    self.broker, signal.symbol, trail_percent=self.limits.trail_percent
                )
                res["trailing_stop"] = f"{self.limits.trail_percent}%"
            except Exception as exc:
                log.warning("Trailing stop voor %s plaatsen mislukt: %s", signal.symbol, exc)
                res["trailing_stop"] = f"MISLUKT: {exc}"
        else:
            log.warning("Order %s nog niet gevuld (markt dicht?). Trailing stop volgt later.",
                        signal.symbol)
            res["trailing_stop"] = "uitgesteld (order niet gevuld)"

        self._open_symbols.add(signal.symbol)
        self._open_count += 1
        self.db.add_trade({
            "symbol": signal.symbol, "side": "buy", "qty": qty,
            "price": signal.price, "order_id": str(order.id), "mode": self.mode,
            "status": "filled" if filled else "pending", "signal_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

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
