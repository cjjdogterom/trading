"""SQLite-opslag voor nieuws, sentiment, signalen en uitgevoerde trades."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id          TEXT PRIMARY KEY,        -- artikel-id van de bron
    symbol      TEXT NOT NULL,
    headline    TEXT,
    summary     TEXT,
    source      TEXT,
    url         TEXT,
    published   TEXT,                    -- ISO-tijd van publicatie
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news(symbol, published);

CREATE TABLE IF NOT EXISTS sentiment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    day         TEXT NOT NULL,           -- YYYY-MM-DD
    score       REAL NOT NULL,           -- -1.0 (bearish) .. +1.0 (bullish)
    label       TEXT NOT NULL,           -- bullish / bearish / neutral
    confidence  REAL,                    -- 0..1
    rationale   TEXT,
    n_articles  INTEGER,
    engine      TEXT,                    -- claude / lexicon
    created_at  TEXT NOT NULL,
    UNIQUE(symbol, day, engine)
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    day         TEXT NOT NULL,
    action      TEXT NOT NULL,           -- buy / sell / hold
    reason      TEXT,
    sentiment   REAL,
    trend       REAL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ladders (
    symbol        TEXT PRIMARY KEY,
    rungs_total   INTEGER NOT NULL,
    rungs_filled  INTEGER NOT NULL,
    last_fill     REAL NOT NULL,       -- koers van de laatste (bij)koop
    rung_qty      REAL NOT NULL,       -- aantal aandelen per stukje
    active        INTEGER NOT NULL,    -- 1 = nog actief opbouwen
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         REAL,
    price       REAL,
    order_id    TEXT,
    mode        TEXT,                    -- paper / live / dry-run
    status      TEXT,
    signal_id   INTEGER,
    created_at  TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str = "data/trading.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- news -----------------------------------------------------------

    def upsert_news(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """INSERT OR IGNORE INTO news
                   (id, symbol, headline, summary, source, url, published, fetched_at)
                   VALUES (:id,:symbol,:headline,:summary,:source,:url,:published,:fetched_at)""",
                rows,
            )
            return c.total_changes

    def news_since(self, symbol: str, since_iso: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM news WHERE symbol=? AND published>=? ORDER BY published DESC",
                (symbol, since_iso),
            ).fetchall()

    # ---- sentiment ------------------------------------------------------

    def upsert_sentiment(self, row: dict) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO sentiment
                   (symbol, day, score, label, confidence, rationale, n_articles, engine, created_at)
                   VALUES (:symbol,:day,:score,:label,:confidence,:rationale,:n_articles,:engine,:created_at)
                   ON CONFLICT(symbol, day, engine) DO UPDATE SET
                     score=excluded.score, label=excluded.label, confidence=excluded.confidence,
                     rationale=excluded.rationale, n_articles=excluded.n_articles,
                     created_at=excluded.created_at""",
                row,
            )

    def latest_sentiment(self) -> list[sqlite3.Row]:
        """Meest recente sentiment-rij per symbool."""
        with self._conn() as c:
            return c.execute(
                """SELECT s.* FROM sentiment s
                   JOIN (SELECT symbol, MAX(day) AS d FROM sentiment GROUP BY symbol) m
                     ON s.symbol=m.symbol AND s.day=m.d
                   ORDER BY s.score DESC""",
            ).fetchall()

    def sentiment_history(self, symbol: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM sentiment WHERE symbol=? ORDER BY day", (symbol,)
            ).fetchall()

    # ---- signals & trades ----------------------------------------------

    def add_signal(self, row: dict) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO signals (symbol, day, action, reason, sentiment, trend, created_at)
                   VALUES (:symbol,:day,:action,:reason,:sentiment,:trend,:created_at)""",
                row,
            )
            return cur.lastrowid

    def recent_signals(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def add_trade(self, row: dict) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO trades (symbol, side, qty, price, order_id, mode, status, signal_id, created_at)
                   VALUES (:symbol,:side,:qty,:price,:order_id,:mode,:status,:signal_id,:created_at)""",
                row,
            )
            return cur.lastrowid

    def recent_trades(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    # ---- ladders --------------------------------------------------------

    def get_ladder(self, symbol: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM ladders WHERE symbol=?", (symbol,)).fetchone()

    def upsert_ladder(self, row: dict) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO ladders (symbol, rungs_total, rungs_filled, last_fill, rung_qty, active, updated_at)
                   VALUES (:symbol,:rungs_total,:rungs_filled,:last_fill,:rung_qty,:active,:updated_at)
                   ON CONFLICT(symbol) DO UPDATE SET
                     rungs_total=excluded.rungs_total, rungs_filled=excluded.rungs_filled,
                     last_fill=excluded.last_fill, rung_qty=excluded.rung_qty,
                     active=excluded.active, updated_at=excluded.updated_at""",
                row,
            )

    def active_ladders(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM ladders WHERE active=1").fetchall()

    def deactivate_ladder(self, symbol: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE ladders SET active=0 WHERE symbol=?", (symbol,))
