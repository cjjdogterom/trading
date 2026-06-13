"""Logica-tests voor de trailing-stop engine met een nep-broker (geen netwerk)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpaca.trading.enums import OrderSide, PositionSide

from trailing_stop.broker import PositionInfo
from trailing_stop.engine import (
    EngineConfig,
    TrailingStopEngine,
    place_native_trailing_stop,
)


class _Order:
    _n = 0
    def __init__(self):
        _Order._n += 1
        self.id = f"ord-{_Order._n}"
        self.status = "new"


class FakeBroker:
    """Minimale nep-broker: vaste positie, instelbare koers, onthoudt orderacties."""

    def __init__(self, side=PositionSide.LONG, entry=100.0, qty=10.0, atr=2.0):
        self._pos = PositionInfo("TEST", qty, side, entry, entry)
        self.price = entry
        self._atr = atr
        self.position_open = True
        self.calls = []  # (actie, args)

    # data
    def is_market_open(self): return True
    def latest_price(self, symbol): return self.price
    def atr(self, symbol, period=14, timeframe=None): return self._atr

    # posities
    def get_position(self, symbol):
        return self._pos if self.position_open else None

    # orders
    def submit_stop(self, symbol, qty, side, stop_price, **kw):
        self.calls.append(("submit", round(stop_price, 2), side))
        return _Order()

    def replace_stop(self, order_id, new_stop_price):
        self.calls.append(("replace", round(new_stop_price, 2)))
        return _Order()

    def submit_native_trailing_stop(self, symbol, qty, side, **kw):
        self.calls.append(("native", side, kw))
        return _Order()

    def get_order(self, oid):
        return _Order()


def _engine(broker, **cfg_kw):
    cfg = EngineConfig(symbol="TEST", poll_seconds=1, **cfg_kw)
    eng = TrailingStopEngine(broker, cfg)
    # initiële stop opzetten zoals run() zou doen
    eng.extreme = broker.price
    eng.stop_level = round(eng._desired_stop(eng.extreme), 2)
    eng.stop_order_id = "init"
    return eng


def feed(eng, broker, prices):
    for px in prices:
        broker.price = px
        eng._tick()


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


# --------------------------------------------------------------------------

def test_long_percent_ratchets_up_only():
    b = FakeBroker(side=PositionSide.LONG, entry=100.0)
    eng = _engine(b, trail_percent=10.0)
    assert approx(eng.stop_level, 90.0)          # 100 - 10%
    feed(eng, b, [110])                          # top 110 -> 99
    assert approx(eng.stop_level, 99.0)
    feed(eng, b, [105])                          # zakt -> stop blijft 99
    assert approx(eng.stop_level, 99.0)
    feed(eng, b, [120])                          # nieuwe top -> 108
    assert approx(eng.stop_level, 108.0)
    feed(eng, b, [115, 90, 130])                 # alleen 130 verbetert -> 117
    assert approx(eng.stop_level, 117.0)
    print("✓ long percent ratchet (alleen omhoog)")


def test_short_percent_ratchets_down_only():
    b = FakeBroker(side=PositionSide.SHORT, entry=100.0)
    eng = _engine(b, trail_percent=10.0)
    assert approx(eng.stop_level, 110.0)         # 100 + 10%
    feed(eng, b, [90])                           # bodem 90 -> 99
    assert approx(eng.stop_level, 99.0)
    feed(eng, b, [95])                           # omhoog -> stop blijft 99
    assert approx(eng.stop_level, 99.0)
    feed(eng, b, [80])                           # nieuwe bodem -> 88
    assert approx(eng.stop_level, 88.0)
    print("✓ short percent ratchet (alleen omlaag)")


def test_long_fixed_amount():
    b = FakeBroker(side=PositionSide.LONG, entry=50.0)
    eng = _engine(b, trail_amount=1.5)
    assert approx(eng.stop_level, 48.5)
    feed(eng, b, [55])
    assert approx(eng.stop_level, 53.5)
    print("✓ long vast bedrag")


def test_atr_multiplier():
    b = FakeBroker(side=PositionSide.LONG, entry=100.0, atr=2.0)
    eng = _engine(b, atr_mult=3.0)               # trail = 3 x 2.0 = 6.0
    assert approx(eng.stop_level, 94.0)
    feed(eng, b, [108])
    assert approx(eng.stop_level, 102.0)
    print("✓ ATR-multiplier trail")


def test_activation_gate():
    b = FakeBroker(side=PositionSide.LONG, entry=100.0)
    eng = _engine(b, trail_percent=5.0, activation_percent=5.0)
    assert not eng.activated
    feed(eng, b, [103])                          # < 105 -> nog niet actief
    assert not eng.activated
    assert approx(eng.stop_level, 95.0)          # ongewijzigd
    feed(eng, b, [106])                          # >= 105 -> actief, ratchet
    assert eng.activated
    assert approx(eng.stop_level, 100.7)         # 106 - 5%
    print("✓ activatiedrempel")


def test_position_closed_stops_engine():
    b = FakeBroker(side=PositionSide.LONG, entry=100.0)
    eng = _engine(b, trail_percent=10.0)
    eng._running = True
    b.position_open = False
    eng._tick()
    assert eng._running is False
    print("✓ engine stopt als positie gesloten is")


def test_native_long_uses_sell():
    b = FakeBroker(side=PositionSide.LONG)
    place_native_trailing_stop(b, "TEST", trail_percent=3.0)
    action, side, kw = b.calls[-1]
    assert action == "native" and side == OrderSide.SELL
    assert kw["trail_percent"] == 3.0
    print("✓ native long -> SELL trailing stop")


def test_native_short_uses_buy():
    b = FakeBroker(side=PositionSide.SHORT)
    place_native_trailing_stop(b, "TEST", trail_price=1.0)
    action, side, kw = b.calls[-1]
    assert action == "native" and side == OrderSide.BUY
    print("✓ native short -> BUY trailing stop")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nAlle {len(fns)} tests geslaagd.")
