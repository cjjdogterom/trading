"""Welke symbolen volgen we? Bewerkbaar in watchlist.json."""

from __future__ import annotations

import json
from pathlib import Path

_DEFAULT = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "JPM", "SPY"]
_PATH = Path("watchlist.json")


def load_watchlist() -> list[str]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text())
            syms = [s.strip().upper() for s in data if str(s).strip()]
            if syms:
                return syms
        except Exception:
            pass
    # eerste keer: schrijf de default zodat de gebruiker hem kan aanpassen
    _PATH.write_text(json.dumps(_DEFAULT, indent=2))
    return list(_DEFAULT)
