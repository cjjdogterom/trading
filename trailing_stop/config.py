"""Configuratie: laadt Alpaca-credentials en instellingen uit .env / omgeving."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret_key: str
    paper: bool
    data_feed: DataFeed

    @property
    def account_type(self) -> str:
        return "PAPER" if self.paper else "LIVE"


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings(*, force_live: bool | None = None) -> Settings:
    """Lees credentials uit .env / omgevingsvariabelen.

    force_live: None  -> volg ALPACA_PAPER uit .env
                False -> forceer paper (veilig)
                True  -> forceer live (echt geld)
    """
    load_dotenv()  # leest .env in de werkmap als die bestaat

    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY ontbreken. "
            "Maak een .env-bestand aan (zie .env.example)."
        )

    if force_live is None:
        paper = _as_bool(os.getenv("ALPACA_PAPER"), default=True)
    else:
        paper = not force_live

    feed_name = os.getenv("ALPACA_DATA_FEED", "iex").strip().lower()
    data_feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX

    # Veiligheidscheck: een PK-key is een paper-key. Live draaien met een
    # paper-key (of andersom) gaat sowieso falen bij Alpaca, maar we waarschuwen
    # vroeg zodat het duidelijk is.
    if not paper and api_key.upper().startswith("PK"):
        raise RuntimeError(
            "Je vraagt om LIVE trading maar de API-key begint met 'PK' "
            "(dat is een paper-key). Gebruik een live-key (AK...) of draai paper."
        )

    return Settings(
        api_key=api_key,
        secret_key=secret_key,
        paper=paper,
        data_feed=data_feed,
    )
