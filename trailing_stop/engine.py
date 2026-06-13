"""Trailing-stop logica: native helper + managed ratchet-engine."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from alpaca.trading.enums import OrderSide, PositionSide

from .broker import Broker, PositionInfo

log = logging.getLogger("trailing_stop")


# --------------------------------------------------------------------------
# Hulp: positie + uitstap-richting bepalen
# --------------------------------------------------------------------------

def _exit_side(position_side: PositionSide) -> OrderSide:
    """Een LONG positie sluit je met SELL, een SHORT met BUY."""
    return OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY


def _resolve_position(broker: Broker, symbol: str, qty: float | None) -> PositionInfo:
    pos = broker.get_position(symbol)
    if pos is None:
        raise RuntimeError(
            f"Geen open positie in {symbol}. Een trailing stop beschermt een "
            f"bestaande positie — open eerst een positie of geef het juiste symbool."
        )
    if qty is not None:
        if qty > pos.qty:
            raise RuntimeError(
                f"Gevraagde qty {qty} > open positie {pos.qty} in {symbol}."
            )
        pos.qty = qty
    return pos


# --------------------------------------------------------------------------
# 1) Native trailing stop (server-side, set & forget)
# --------------------------------------------------------------------------

def place_native_trailing_stop(
    broker: Broker,
    symbol: str,
    *,
    trail_percent: float | None = None,
    trail_price: float | None = None,
    qty: float | None = None,
):
    """Plaats Alpaca's ingebouwde trailing-stop order voor een bestaande positie.

    Geef precies één van trail_percent of trail_price op.
    """
    if (trail_percent is None) == (trail_price is None):
        raise ValueError("Geef precies één van trail_percent of trail_price op.")

    pos = _resolve_position(broker, symbol, qty)
    side = _exit_side(pos.side)
    order = broker.submit_native_trailing_stop(
        symbol=symbol,
        qty=pos.qty,
        side=side,
        trail_percent=trail_percent,
        trail_price=trail_price,
    )
    trail_desc = (
        f"{trail_percent}%" if trail_percent is not None else f"${trail_price}"
    )
    log.info(
        "Native trailing stop geplaatst: %s %s x%s, trail %s (order %s)",
        side.value, symbol, pos.qty, trail_desc, order.id,
    )
    return order


# --------------------------------------------------------------------------
# 2) Managed ratchet-engine (client-side, flexibel + vangnet)
# --------------------------------------------------------------------------

@dataclass
class EngineConfig:
    symbol: str
    # Trail: geef precies één van deze drie.
    trail_percent: float | None = None   # bv. 3.0  -> 3% onder de top
    trail_amount: float | None = None    # bv. 1.50 -> $1.50 onder de top
    atr_mult: float | None = None        # bv. 3.0  -> 3 x ATR onder de top
    atr_period: int = 14

    qty: float | None = None             # None = volledige positie
    activation_percent: float | None = None  # pas ratchen na X% winst t.o.v. entry
    poll_seconds: float = 5.0
    state_dir: str = ".state"
    dry_run: bool = False                # niets naar Alpaca sturen, alleen loggen


class TrailingStopEngine:
    """Houdt een rustende stop-order aan op Alpaca en schuift die mee met de koers.

    Werking (long): bewaar de hoogste koers sinds start (high-water). Het
    gewenste stop-niveau is `top - trail`. Zodra dat hoger ligt dan de huidige
    stop, vervangen we de order naar boven (ratchet). De stop gaat NOOIT omlaag.
    Voor short spiegelt alles: laagste koers, stop = `bodem + trail`, alleen omlaag.

    De echte stop-order staat altijd op Alpaca, dus ook als dit script crasht
    blijf je beschermd op het laatst bekende niveau.
    """

    def __init__(self, broker: Broker, cfg: EngineConfig):
        self.broker = broker
        self.cfg = cfg
        self._validate()

        self.pos: PositionInfo = _resolve_position(broker, cfg.symbol, cfg.qty)
        self.is_long = self.pos.side == PositionSide.LONG
        self.exit_side = _exit_side(self.pos.side)

        # Trail uitgedrukt als vast bedrag (dollars). Percent wordt per koers herrekend.
        self._fixed_amount: float | None = None
        if cfg.trail_amount is not None:
            self._fixed_amount = cfg.trail_amount
        elif cfg.atr_mult is not None:
            atr = broker.atr(cfg.symbol, period=cfg.atr_period)
            self._fixed_amount = cfg.atr_mult * atr
            log.info("ATR(%d)=%.4f -> trail = %.2f x ATR = $%.2f",
                     cfg.atr_period, atr, cfg.atr_mult, self._fixed_amount)

        self.extreme: float | None = None      # high-water (long) / low-water (short)
        self.stop_level: float | None = None
        self.stop_order_id = None
        self.activated = cfg.activation_percent is None  # geen drempel = direct actief
        self._running = False

        self.state_path = Path(cfg.state_dir) / f"{cfg.symbol}.json"

    # ---- validatie ------------------------------------------------------

    def _validate(self) -> None:
        given = [
            self.cfg.trail_percent is not None,
            self.cfg.trail_amount is not None,
            self.cfg.atr_mult is not None,
        ]
        if sum(given) != 1:
            raise ValueError(
                "Geef precies één van trail_percent / trail_amount / atr_mult op."
            )
        if self.cfg.poll_seconds < 1:
            raise ValueError("poll_seconds moet >= 1 zijn (rate limits).")

    # ---- stop-niveau berekenen -----------------------------------------

    def _trail_distance(self, ref_price: float) -> float:
        if self.cfg.trail_percent is not None:
            return ref_price * self.cfg.trail_percent / 100.0
        return float(self._fixed_amount)

    def _desired_stop(self, extreme: float) -> float:
        dist = self._trail_distance(extreme)
        return extreme - dist if self.is_long else extreme + dist

    def _is_improvement(self, new_stop: float) -> bool:
        if self.stop_level is None:
            return True
        # Long: stop mag alleen omhoog. Short: alleen omlaag.
        return new_stop > self.stop_level if self.is_long else new_stop < self.stop_level

    def _check_activation(self, price: float) -> None:
        if self.activated or self.cfg.activation_percent is None:
            return
        entry = self.pos.avg_entry_price
        target = (
            entry * (1 + self.cfg.activation_percent / 100.0)
            if self.is_long
            else entry * (1 - self.cfg.activation_percent / 100.0)
        )
        reached = price >= target if self.is_long else price <= target
        if reached:
            self.activated = True
            log.info("Activatiedrempel bereikt (koers %.2f vs %.2f). Ratchet aan.",
                     price, target)

    # ---- state op schijf -----------------------------------------------

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "symbol": self.cfg.symbol,
            "side": self.pos.side.value,
            "extreme": self.extreme,
            "stop_level": self.stop_level,
            "stop_order_id": str(self.stop_order_id) if self.stop_order_id else None,
            "activated": self.activated,
        }, indent=2))

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
        except Exception:
            return
        if data.get("side") != self.pos.side.value:
            return  # andere richting; negeer oude state
        oid = data.get("stop_order_id")
        if not oid:
            return
        try:
            order = self.broker.get_order(oid)
        except Exception:
            return
        status = getattr(order, "status", "")
        status = getattr(status, "value", status)  # OrderStatus-enum -> "accepted"
        if str(status).lower() in {"new", "accepted", "held", "pending_new", "partially_filled"}:
            self.extreme = data.get("extreme")
            self.stop_level = data.get("stop_level")
            self.stop_order_id = oid
            self.activated = data.get("activated", self.activated)
            log.info("Hervat bestaande stop-order %s op niveau %.2f.",
                     oid, self.stop_level or 0.0)

    # ---- hoofdlus -------------------------------------------------------

    def run(self) -> None:
        self._running = True
        if not self.broker.is_market_open():
            log.warning("Markt is gesloten. Engine draait; stop triggert pas bij opening.")

        self._load_state()
        price = self.broker.latest_price(self.cfg.symbol)
        self.extreme = self.extreme if self.extreme is not None else price

        # Initiële stop plaatsen als die er nog niet is.
        if self.stop_order_id is None:
            self.stop_level = self._desired_stop(self.extreme)
            self._place_initial_stop()
            self._save_state()

        log.info(
            "Engine actief: %s %s x%s | richting=%s | koers=%.2f | stop=%.2f | poll=%ss",
            self.exit_side.value, self.cfg.symbol, self.pos.qty,
            self.pos.side.value, price, self.stop_level, self.cfg.poll_seconds,
        )

        try:
            while self._running:
                time.sleep(self.cfg.poll_seconds)
                self._tick()
        except KeyboardInterrupt:
            log.info("Onderbroken (Ctrl-C). De rustende stop-order BLIJFT staan op %.2f.",
                     self.stop_level or 0.0)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    def _tick(self) -> None:
        # 1) Positie nog open? Anders is de stop geraakt of handmatig gesloten.
        pos = self.broker.get_position(self.cfg.symbol)
        if pos is None:
            log.info("Positie %s is gesloten — stop waarschijnlijk getriggerd. Klaar.",
                     self.cfg.symbol)
            self._running = False
            return

        price = self.broker.latest_price(self.cfg.symbol)
        self._check_activation(price)

        # 2) High/low-water bijwerken.
        improved_extreme = (
            price > self.extreme if self.is_long else price < self.extreme
        )
        if improved_extreme:
            self.extreme = price

        # 3) Voor activatie niet ratchen, alleen extreme bijhouden.
        if not self.activated:
            log.debug("Wacht op activatie | koers=%.2f extreme=%.2f", price, self.extreme)
            return

        # 4) Gewenste stop berekenen en evt. order omhoog/omlaag schuiven.
        new_stop = round(self._desired_stop(self.extreme), 2)
        if self._is_improvement(new_stop):
            self._move_stop(new_stop)
            self._save_state()
        else:
            log.debug("Koers=%.2f extreme=%.2f stop blijft %.2f", price, self.extreme, self.stop_level)

    # ---- orderacties ----------------------------------------------------

    def _place_initial_stop(self) -> None:
        if self.cfg.dry_run:
            log.info("[dry-run] Zou initiële stop plaatsen op %.2f", self.stop_level)
            self.stop_order_id = "DRYRUN"
            return
        order = self.broker.submit_stop(
            self.cfg.symbol, self.pos.qty, self.exit_side, self.stop_level
        )
        self.stop_order_id = order.id
        log.info("Initiële stop-order geplaatst op %.2f (order %s).",
                 self.stop_level, order.id)

    def _move_stop(self, new_stop: float) -> None:
        old = self.stop_level
        self.stop_level = new_stop
        if self.cfg.dry_run:
            log.info("[dry-run] Stop %.2f -> %.2f (extreme %.2f)", old or 0, new_stop, self.extreme)
            return
        try:
            order = self.broker.replace_stop(self.stop_order_id, new_stop)
            self.stop_order_id = order.id  # replace levert (mogelijk) nieuwe id op
            log.info("Stop verschoven %.2f -> %.2f (extreme %.2f).", old or 0, new_stop, self.extreme)
        except Exception as exc:
            # Replace kan falen als de order net (deels) gevuld is. Niet fataal.
            log.warning("Stop verschuiven naar %.2f mislukt: %s", new_stop, exc)
            self.stop_level = old
