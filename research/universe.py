"""Het handelsuniversum: Nasdaq-100 (liquide, haalbaar, echte spreiding).

Dit is een momentopname — de Nasdaq-100 wijzigt ~jaarlijks. Pas aan via
`universe.json` (een JSON-lijst van tickers) als je een andere selectie wilt.
"""

from __future__ import annotations

import json
from pathlib import Path

# Benadering van de Nasdaq-100 (bewerkbaar via universe.json).
NASDAQ_100 = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "GOOG", "META", "AVGO", "TSLA", "COST",
    "NFLX", "ADBE", "PEP", "AMD", "CSCO", "TMUS", "INTC", "CMCSA", "QCOM", "INTU",
    "TXN", "AMGN", "HON", "AMAT", "ISRG", "BKNG", "VRTX", "ADP", "SBUX", "GILD",
    "MDLZ", "ADI", "REGN", "LRCX", "PANW", "MU", "KLAC", "SNPS", "CDNS", "MELI",
    "PYPL", "ASML", "ABNB", "CRWD", "MAR", "ORLY", "CTAS", "NXPI", "FTNT", "CHTR",
    "DASH", "ADSK", "WDAY", "PCAR", "ROP", "MRVL", "MNST", "AEP", "CPRT", "KDP",
    "KHC", "EXC", "CSX", "CCEP", "TTD", "FAST", "ROST", "ODFL", "EA", "DDOG",
    "VRSK", "CTSH", "XEL", "BKR", "GEHC", "LULU", "ANSS", "IDXX", "DXCM", "ZS",
    "TEAM", "ON", "CDW", "BIIB", "GFS", "MDB", "ILMN", "ARM", "MRNA", "SMCI",
    "DLTR", "SIRI", "ENPH",
]
_PATH = Path("universe.json")


def load_universe() -> list[str]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text())
            syms = [s.strip().upper() for s in data if str(s).strip()]
            if syms:
                return syms
        except Exception:
            pass
    _PATH.write_text(json.dumps(NASDAQ_100, indent=2))
    return list(NASDAQ_100)
