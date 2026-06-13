"""Streamlit-dashboard: account, equity-curve, posities, sentiment en trades.

Start met:  python cli.py dashboard
Of direct:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

# Zorg dat de projectmodules importeerbaar zijn als streamlit dit bestand draait.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from alpaca.trading.requests import GetPortfolioHistoryRequest

from trailing_stop import Broker, load_settings
from storage import DB

st.set_page_config(page_title="Trading dashboard", page_icon="📈", layout="wide")


@st.cache_resource
def _broker() -> Broker:
    return Broker(load_settings())


@st.cache_resource
def _db() -> DB:
    return DB()


_SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "snapshot.json")


@st.cache_data(ttl=60)
def _snapshot() -> dict:
    """Door de dagelijkse GitHub Action gecommitte data (voor Streamlit Cloud)."""
    try:
        with open(_SNAPSHOT) as f:
            return json.load(f)
    except Exception:
        return {}


@st.cache_data(ttl=60)
def _account():
    a = _broker().trading.get_account()
    return {
        "equity": float(a.equity),
        "last_equity": float(getattr(a, "last_equity", 0) or 0),
        "buying_power": float(a.buying_power),
        "cash": float(a.cash),
    }


@st.cache_data(ttl=60)
def _positions() -> pd.DataFrame:
    pos = _broker().trading.get_all_positions()
    rows = [{
        "Symbool": p.symbol, "Kant": p.side.value, "Aantal": float(p.qty),
        "Entry": float(p.avg_entry_price), "Nu": float(p.current_price),
        "Marktwaarde": float(p.market_value),
        "P/L $": float(p.unrealized_pl), "P/L %": float(p.unrealized_plpc) * 100,
    } for p in pos]
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def _equity_curve() -> pd.DataFrame:
    ph = _broker().trading.get_portfolio_history(
        GetPortfolioHistoryRequest(period="3M", timeframe="1D")
    )
    ts = [datetime.fromtimestamp(t, tz=timezone.utc) for t in (ph.timestamp or [])]
    eq = ph.equity or []
    df = pd.DataFrame({"datum": ts, "equity": eq})
    return df[df["equity"] > 0]


@st.cache_data(ttl=60)
def _sentiment() -> pd.DataFrame:
    rows = [dict(r) for r in _db().latest_sentiment()] or _snapshot().get("sentiment", [])
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def _signals() -> pd.DataFrame:
    rows = [dict(r) for r in _db().recent_signals(40)] or _snapshot().get("signals", [])
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def _trades() -> pd.DataFrame:
    rows = [dict(r) for r in _db().recent_trades(60)] or _snapshot().get("trades", [])
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------

settings = load_settings()
badge = "🟢 PAPER" if settings.paper else "🔴 LIVE"
st.title("📈 Trading dashboard")
st.caption(f"Account: {badge}  ·  data ververst elke 60s  ·  {datetime.now():%Y-%m-%d %H:%M}")

if st.button("🔄 Ververs nu"):
    st.cache_data.clear()
    st.rerun()

# ---- Topmetrics ----
try:
    acct = _account()
    daily = (acct["equity"] - acct["last_equity"]) / acct["last_equity"] * 100 if acct["last_equity"] else 0.0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${acct['equity']:,.0f}")
    c2.metric("Dagresultaat", f"{daily:+.2f}%")
    c3.metric("Buying power", f"${acct['buying_power']:,.0f}")
    c4.metric("Cash", f"${acct['cash']:,.0f}")
except Exception as exc:
    st.error(f"Account ophalen mislukt: {exc}")

# ---- Equity-curve ----
st.subheader("Equity-curve (3 maanden)")
try:
    df = _equity_curve()
    if df.empty:
        st.info("Nog geen equity-historie.")
    else:
        fig = go.Figure(go.Scatter(x=df["datum"], y=df["equity"], mode="lines",
                                    line=dict(color="#2e7d32", width=2), fill="tozeroy",
                                    fillcolor="rgba(46,125,50,0.08)"))
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="Equity ($)")
        st.plotly_chart(fig, use_container_width=True)
except Exception as exc:
    st.error(f"Equity-curve mislukt: {exc}")

# ---- Posities + sentiment naast elkaar ----
left, right = st.columns(2)

with left:
    st.subheader("Open posities")
    try:
        pos = _positions()
        if pos.empty:
            st.info("Geen open posities.")
        else:
            st.dataframe(
                pos.style.format({
                    "Entry": "{:.2f}", "Nu": "{:.2f}", "Marktwaarde": "${:,.0f}",
                    "P/L $": "${:+,.2f}", "P/L %": "{:+.2f}%",
                }).map(lambda v: "color: #2e7d32" if isinstance(v, (int, float)) and v > 0
                       else "color: #c62828" if isinstance(v, (int, float)) and v < 0 else "",
                       subset=["P/L $", "P/L %"]),
                use_container_width=True, hide_index=True,
            )
    except Exception as exc:
        st.error(f"Posities mislukt: {exc}")

with right:
    st.subheader("Sentiment (laatste per symbool)")
    sent = _sentiment()
    if sent.empty:
        st.info("Nog geen sentiment. Draai `python cli.py run`.")
    else:
        emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
        view = pd.DataFrame({
            "": sent["label"].map(lambda l: emoji.get(l, "⚪")),
            "Symbool": sent["symbol"],
            "Score": sent["score"],
            "Label": sent["label"],
            "Zekerheid": sent["confidence"],
            "Toelichting": sent["rationale"],
        })
        st.dataframe(view.style.format({"Score": "{:+.2f}", "Zekerheid": "{:.0%}"}),
                     use_container_width=True, hide_index=True)

# ---- Signalen + trades ----
st.subheader("Recente signalen")
sig = _signals()
if sig.empty:
    st.info("Nog geen signalen.")
else:
    st.dataframe(sig[["day", "symbol", "action", "trend", "sentiment", "reason"]],
                 use_container_width=True, hide_index=True)

st.subheader("Uitgevoerde trades")
tr = _trades()
if tr.empty:
    st.info("Nog geen trades vastgelegd (advisory-modus plaatst geen orders).")
else:
    st.dataframe(tr[["created_at", "symbol", "side", "qty", "price", "mode", "status"]],
                 use_container_width=True, hide_index=True)
