"""Trailing-stop tooling voor Alpaca.

Twee benaderingen:
  - native:  Alpaca's ingebouwde trailing-stop order (server-side, set & forget).
  - engine:  een eigen managed engine die een rustende stop-order omhoog ratchet.

Zie README.md voor gebruik.
"""

from .config import Settings, load_settings
from .broker import Broker
from .engine import TrailingStopEngine, EngineConfig, place_native_trailing_stop

__all__ = [
    "Settings",
    "load_settings",
    "Broker",
    "TrailingStopEngine",
    "EngineConfig",
    "place_native_trailing_stop",
]
