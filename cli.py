#!/usr/bin/env python3
"""Command-line interface voor de Alpaca trailing-stop tool.

Voorbeelden:
    python cli.py status
    python cli.py buy AAPL --qty 10                 # paper-positie openen om te testen
    python cli.py native AAPL --trail-percent 3      # server-side trailing stop
    python cli.py engine AAPL --trail-percent 3 --poll 5
    python cli.py engine AAPL --atr-mult 3 --activation-percent 2

Paper is standaard. Echt geld vereist --live én een live-key (AK...).
"""

from __future__ import annotations

import argparse
import logging
import sys

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import os

from trailing_stop import (
    Broker,
    EngineConfig,
    TrailingStopEngine,
    load_settings,
    place_native_trailing_stop,
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _broker(args) -> Broker:
    settings = load_settings(force_live=True if args.live else None)
    broker = Broker(settings)
    banner = "🟢 PAPER" if settings.paper else "🔴 LIVE — ECHT GELD"
    logging.getLogger("trailing_stop").info("Account: %s", banner)
    return broker


# --------------------------------------------------------------------------
# commando's
# --------------------------------------------------------------------------

def cmd_status(args) -> int:
    broker = _broker(args)
    clock = broker.trading.get_clock()
    acct = broker.trading.get_account()
    print(f"Markt open: {clock.is_open}  (volgende open: {clock.next_open})")
    print(f"Buying power: ${float(acct.buying_power):,.2f}  | equity: ${float(acct.equity):,.2f}")
    positions = broker.trading.get_all_positions()
    if not positions:
        print("Geen open posities.")
    for p in positions:
        pl = float(p.unrealized_pl)
        print(f"  {p.symbol:6} {p.side.value:5} qty={p.qty:>8}  "
              f"entry={float(p.avg_entry_price):>8.2f}  nu={float(p.current_price):>8.2f}  "
              f"P/L=${pl:+.2f}")
    return 0


def cmd_buy(args) -> int:
    broker = _broker(args)
    side = OrderSide.SELL if args.side == "sell" else OrderSide.BUY
    order = broker.trading.submit_order(MarketOrderRequest(
        symbol=args.symbol, qty=args.qty, side=side, time_in_force=TimeInForce.DAY,
    ))
    print(f"Market {side.value} {args.symbol} x{args.qty} ingelegd (order {order.id}).")
    return 0


def cmd_native(args) -> int:
    broker = _broker(args)
    place_native_trailing_stop(
        broker, args.symbol,
        trail_percent=args.trail_percent,
        trail_price=args.trail_price,
        qty=args.qty,
    )
    return 0


def cmd_run(args) -> int:
    from strategy import RiskLimits
    from daily_run import run_daily

    allow_live = args.i_understand_live_risk and os.getenv("ALLOW_LIVE_AUTOTRADE", "").lower() in {"1", "true", "yes"}
    limits = RiskLimits(
        max_position_pct=args.max_position_pct,
        max_open_positions=args.max_positions,
        max_daily_loss_pct=args.max_daily_loss,
        trail_percent=args.trail_percent,
        min_confidence=args.min_confidence,
    )
    if args.execute and args.live and not allow_live:
        print("⛔ Live auto-trading vereist BEIDE: env ALLOW_LIVE_AUTOTRADE=true "
              "én de vlag --i-understand-live-risk. Afgebroken.")
        return 1

    results = run_daily(
        execute=args.execute,
        allow_live=allow_live,
        force_live=True if args.live else None,
        limits=limits,
    )
    print("\nSamenvatting:")
    for r in results:
        tag = "🟢 ORDER" if r.get("placed") else "  "
        print(f"  {tag} {r['symbol']:6} {r.get('action',''):6} {r.get('reason','')}")
    return 0


def cmd_dashboard(args) -> int:
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    app = os.path.join(here, "dashboard", "app.py")
    print("Dashboard start op http://localhost:8501  (stop met Ctrl-C)")
    return subprocess.call([sys.executable, "-m", "streamlit", "run", app])


def cmd_engine(args) -> int:
    broker = _broker(args)
    cfg = EngineConfig(
        symbol=args.symbol,
        trail_percent=args.trail_percent,
        trail_amount=args.trail_amount,
        atr_mult=args.atr_mult,
        atr_period=args.atr_period,
        qty=args.qty,
        activation_percent=args.activation_percent,
        poll_seconds=args.poll,
        dry_run=args.dry_run,
    )
    TrailingStopEngine(broker, cfg).run()
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Alpaca trailing-stop tool")
    p.add_argument("--live", action="store_true",
                   help="Forceer LIVE trading (echt geld). Vereist een AK-key.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug-logging.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="Account, markt en posities tonen.")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("buy", help="Market-order om (op paper) een positie te openen.")
    sp.add_argument("symbol")
    sp.add_argument("--qty", type=float, required=True)
    sp.add_argument("--side", choices=["buy", "sell"], default="buy",
                    help="buy = long openen, sell = short openen.")
    sp.set_defaults(func=cmd_buy)

    sp = sub.add_parser("native", help="Alpaca's ingebouwde trailing-stop order.")
    sp.add_argument("symbol")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--trail-percent", type=float, help="Trail als %, bv. 3")
    g.add_argument("--trail-price", type=float, help="Trail als $-bedrag, bv. 1.50")
    sp.add_argument("--qty", type=float, default=None,
                    help="Deel van de positie (default: hele positie).")
    sp.set_defaults(func=cmd_native)

    sp = sub.add_parser("run", help="Dagelijkse run: nieuws -> sentiment -> signalen -> (optioneel) traden.")
    sp.add_argument("--execute", action="store_true",
                    help="Plaats orders (anders alleen advies). Op het account van --live/.env.")
    sp.add_argument("--i-understand-live-risk", action="store_true",
                    help="Vereist (samen met env ALLOW_LIVE_AUTOTRADE=true) voor LIVE auto-trading.")
    sp.add_argument("--max-position-pct", type=float, default=5.0)
    sp.add_argument("--max-positions", type=int, default=8)
    sp.add_argument("--max-daily-loss", type=float, default=3.0, help="Kill-switch in %% dagverlies.")
    sp.add_argument("--trail-percent", type=float, default=8.0, help="Trailing stop op elke entry.")
    sp.add_argument("--min-confidence", type=float, default=0.4)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("dashboard", help="Open het Streamlit-dashboard.")
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("engine", help="Managed ratchet-engine (blijft draaien).")
    sp.add_argument("symbol")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--trail-percent", type=float, help="Trail als %, bv. 3")
    g.add_argument("--trail-amount", type=float, help="Trail als vast $-bedrag")
    g.add_argument("--atr-mult", type=float, help="Trail = N x ATR")
    sp.add_argument("--atr-period", type=int, default=14)
    sp.add_argument("--activation-percent", type=float, default=None,
                    help="Begin pas met ratchen na X%% winst t.o.v. entry.")
    sp.add_argument("--qty", type=float, default=None)
    sp.add_argument("--poll", type=float, default=5.0, help="Seconden tussen checks.")
    sp.add_argument("--dry-run", action="store_true",
                    help="Niets naar Alpaca sturen, alleen loggen.")
    sp.set_defaults(func=cmd_engine)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # nette foutmelding i.p.v. stacktrace
        logging.getLogger("trailing_stop").error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
