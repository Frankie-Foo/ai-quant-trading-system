"""Restart-safe storage for minute bars and one NBBO sample per symbol-second."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from execution.alpaca_sip_stream import SipBar, SipEvent, SipQuote


class SipEventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sip_bars (
                    symbol TEXT NOT NULL,
                    ts_utc TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    trade_count INTEGER NOT NULL,
                    vwap REAL NOT NULL,
                    provenance TEXT NOT NULL,
                    received_at_utc TEXT NOT NULL,
                    PRIMARY KEY (symbol, ts_utc)
                );
                CREATE TABLE IF NOT EXISTS sip_quote_seconds (
                    symbol TEXT NOT NULL,
                    second_utc TEXT NOT NULL,
                    quote_ts_utc TEXT NOT NULL,
                    bid_price REAL NOT NULL,
                    bid_size INTEGER NOT NULL,
                    ask_price REAL NOT NULL,
                    ask_size INTEGER NOT NULL,
                    provenance TEXT NOT NULL,
                    received_at_utc TEXT NOT NULL,
                    PRIMARY KEY (symbol, second_utc)
                );
                CREATE INDEX IF NOT EXISTS ix_sip_bars_ts ON sip_bars(ts_utc);
                CREATE INDEX IF NOT EXISTS ix_sip_quotes_ts ON sip_quote_seconds(quote_ts_utc);
                """
            )

    def append(self, event: SipEvent) -> None:
        received = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if isinstance(event, SipBar):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO sip_bars (
                        symbol, ts_utc, open, high, low, close, volume, trade_count,
                        vwap, provenance, received_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.symbol,
                        event.ts_utc.isoformat(),
                        event.open,
                        event.high,
                        event.low,
                        event.close,
                        event.volume,
                        event.trade_count,
                        event.vwap,
                        event.provenance,
                        received,
                    ),
                )
            else:
                second = event.ts_utc.replace(microsecond=0).isoformat()
                connection.execute(
                    """
                    INSERT INTO sip_quote_seconds (
                        symbol, second_utc, quote_ts_utc, bid_price, bid_size,
                        ask_price, ask_size, provenance, received_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, second_utc) DO UPDATE SET
                        quote_ts_utc = excluded.quote_ts_utc,
                        bid_price = excluded.bid_price,
                        bid_size = excluded.bid_size,
                        ask_price = excluded.ask_price,
                        ask_size = excluded.ask_size,
                        provenance = excluded.provenance,
                        received_at_utc = excluded.received_at_utc
                    WHERE excluded.quote_ts_utc > sip_quote_seconds.quote_ts_utc
                    """,
                    (
                        event.symbol,
                        second,
                        event.ts_utc.isoformat(),
                        event.bid_price,
                        event.bid_size,
                        event.ask_price,
                        event.ask_size,
                        event.provenance,
                        received,
                    ),
                )
            connection.commit()

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            bars = int(connection.execute("SELECT COUNT(*) FROM sip_bars").fetchone()[0])
            quotes = int(
                connection.execute("SELECT COUNT(*) FROM sip_quote_seconds").fetchone()[0]
            )
        return {"bars": bars, "quote_seconds": quotes}

    def latest_quote(self, symbol: str) -> SipQuote | None:
        normalized = symbol.strip().upper()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT quote_ts_utc, bid_price, bid_size, ask_price, ask_size, provenance
                FROM sip_quote_seconds
                WHERE symbol = ?
                ORDER BY quote_ts_utc DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return SipQuote(
            symbol=normalized,
            ts_utc=datetime.fromisoformat(str(row[0])),
            bid_price=float(row[1]),
            bid_size=int(row[2]),
            ask_price=float(row[3]),
            ask_size=int(row[4]),
            provenance=str(row[5]),
        )

    def bars_for_symbol(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame:
        if start_utc.tzinfo is None or start_utc.utcoffset() != timedelta(0):
            raise ValueError("start_utc must be timezone-aware UTC")
        if end_utc.tzinfo is None or end_utc.utcoffset() != timedelta(0):
            raise ValueError("end_utc must be timezone-aware UTC")
        if end_utc <= start_utc:
            raise ValueError("end_utc must be after start_utc")
        normalized = symbol.strip().upper()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, ts_utc, open, high, low, close, vwap, volume, trade_count
                FROM sip_bars
                WHERE symbol = ? AND ts_utc >= ? AND ts_utc < ?
                ORDER BY ts_utc
                """,
                (normalized, start_utc.isoformat(), end_utc.isoformat()),
            ).fetchall()
        if not rows:
            return pl.DataFrame(
                schema={
                    "symbol": pl.String,
                    "ts_utc": pl.Datetime("us", "UTC"),
                    "open": pl.Float64,
                    "high": pl.Float64,
                    "low": pl.Float64,
                    "close": pl.Float64,
                    "vwap": pl.Float64,
                    "volume": pl.Int64,
                    "trade_count": pl.Int64,
                }
            )
        return pl.DataFrame(
            rows,
            schema=[
                "symbol",
                "ts_utc",
                "open",
                "high",
                "low",
                "close",
                "vwap",
                "volume",
                "trade_count",
            ],
            orient="row",
        ).with_columns(pl.col("ts_utc").str.to_datetime(time_zone="UTC"))
