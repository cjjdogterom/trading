"""Risicobeheer: positiegrootte, limieten en de dagverlies-kill-switch.

Dit is het belangrijkste bestand van de hele strategie. Een trader overleeft
niet door goede signalen maar door klein verlies te nemen en niet te veel te
riskeren per positie.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_position_pct: float = 5.0    # max % van je equity per positie
    max_open_positions: int = 8      # max aantal posities tegelijk
    max_daily_loss_pct: float = 3.0  # kill-switch: stop bij dit dagverlies
    trail_percent: float = 8.0       # trailing stop op elke entry
    min_confidence: float = 0.4      # negeer signalen met lage sentiment-zekerheid


def position_size(equity: float, price: float, max_position_pct: float) -> int:
    """Aantal aandelen zodat de positie ~max_position_pct van je equity is."""
    if price <= 0:
        return 0
    budget = equity * max_position_pct / 100.0
    return int(budget // price)


def daily_loss_pct(account) -> float:
    """Dagresultaat in % t.o.v. de equity bij de vorige slotkoers.

    Negatief = verlies. Alpaca's `last_equity` is de equity bij de vorige close.
    """
    equity = float(account.equity)
    last = float(getattr(account, "last_equity", 0) or 0)
    if last <= 0:
        return 0.0
    return (equity - last) / last * 100.0
