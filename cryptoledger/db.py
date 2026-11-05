"""SQLite storage. One file (data/cryptoledger.db) holds everything personal."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- User preferences (JSON values). Defaults live in settings.py.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Networks added from the UI (Etherscan-compatible EVM chains etc.).
CREATE TABLE IF NOT EXISTS custom_networks (
    id     TEXT PRIMARY KEY,
    config TEXT NOT NULL
);

-- Your addresses / accounts. `address` is empty for exchange and manual accounts.
CREATE TABLE IF NOT EXISTS wallets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL,
    network       TEXT NOT NULL,
    address       TEXT NOT NULL DEFAULT '',
    portfolio     TEXT NOT NULL DEFAULT 'Personal',
    platform      TEXT NOT NULL DEFAULT '',   -- MetaMask, Ledger, Binance... (wallets.csv)
    rw_slot       INTEGER,
    bollo_applied INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    notes         TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_sync_at  INTEGER,
    last_error    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS wallets_uniq ON wallets(network, address, label);

-- Adapter cursors (last block, last signature...), JSON per wallet.
CREATE TABLE IF NOT EXISTS sync_state (
    wallet_id INTEGER PRIMARY KEY REFERENCES wallets(id) ON DELETE CASCADE,
    state     TEXT NOT NULL
);

-- Every asset seen. Price mapping is editable from the Assets page.
CREATE TABLE IF NOT EXISTS assets (
    key            TEXT PRIMARY KEY,      -- e.g. ethereum:native, ethereum:0xa0b8..., binance:BTC
    network        TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    decimals       INTEGER,
    contract       TEXT,
    coingecko_id   TEXT,
    binance_symbol TEXT,
    fiat           TEXT,                  -- 'EUR', 'USD'... for fiat balances on exchanges
    status         TEXT NOT NULL DEFAULT 'unmapped',  -- known | mapped | unmapped | spam | ignored
    updated_at     INTEGER
);

-- Normalized ledger: one row per balance change of one asset in one wallet.
CREATE TABLE IF NOT EXISTS movements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id       INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    uid             TEXT NOT NULL,          -- adapter-made unique id, makes sync idempotent
    tx_hash         TEXT NOT NULL DEFAULT '',
    ts              INTEGER NOT NULL,       -- unix seconds, UTC
    asset_key       TEXT NOT NULL,
    amount          TEXT NOT NULL,          -- signed decimal: + received, - sent
    kind            TEXT NOT NULL,          -- in | out | fee
    counterparty    TEXT NOT NULL DEFAULT '',
    from_address    TEXT NOT NULL DEFAULT '',   -- sender of this leg, when the source tells
    to_address      TEXT NOT NULL DEFAULT '',   -- recipient of this leg
    failed          INTEGER NOT NULL DEFAULT 0,
    balance_after   TEXT,                   -- absolute balance after the tx, when the chain says it
    info            TEXT NOT NULL DEFAULT '',
    category        TEXT,
    category_source TEXT NOT NULL DEFAULT 'auto',   -- auto | user
    note            TEXT NOT NULL DEFAULT '',
    price_eur       TEXT,
    value_eur       TEXT,
    price_source    TEXT,
    UNIQUE (wallet_id, uid)
);
CREATE INDEX IF NOT EXISTS movements_wallet_ts ON movements(wallet_id, ts);
CREATE INDEX IF NOT EXISTS movements_hash ON movements(tx_hash);

-- Latest live balances per wallet.
CREATE TABLE IF NOT EXISTS current_balances (
    wallet_id  INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    asset_key  TEXT NOT NULL,
    amount     TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (wallet_id, asset_key)
);

-- Point-in-time balances: live sync results, scheduled year-end snapshots,
-- exchange daily snapshots, archive-node reads, manual entries.
CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    taken_at  INTEGER NOT NULL,
    asset_key TEXT NOT NULL,
    amount    TEXT NOT NULL,
    source    TEXT NOT NULL,     -- live | scheduled | exchange_daily | chain_state | manual
    note      TEXT NOT NULL DEFAULT '',
    UNIQUE (wallet_id, taken_at, asset_key, source)
);
CREATE INDEX IF NOT EXISTS snapshots_wallet_time ON snapshots(wallet_id, taken_at);

-- Computed balances and EUR values at each fiscal-year cutoff.
CREATE TABLE IF NOT EXISTS year_end (
    fiscal_year    INTEGER NOT NULL,
    wallet_id      INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    asset_key      TEXT NOT NULL,
    cutoff_ts      INTEGER NOT NULL,
    amount         TEXT NOT NULL,
    balance_source TEXT NOT NULL,
    price_eur      TEXT,
    price_source   TEXT,
    price_ts       INTEGER,
    value_eur      TEXT,
    flags          TEXT NOT NULL DEFAULT '[]',
    computed_at    INTEGER NOT NULL,
    PRIMARY KEY (fiscal_year, wallet_id, asset_key)
);

-- Price cache, keyed by price identity (cg:<id>, bn:<SYMBOL>, fiat:<CUR>).
CREATE TABLE IF NOT EXISTS prices (
    price_key  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    price_eur  TEXT NOT NULL,
    source     TEXT NOT NULL,
    price_ts   INTEGER,
    note       TEXT NOT NULL DEFAULT '',
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (price_key, ts)
);

-- ECB reference rates: units of `currency` per 1 EUR.
CREATE TABLE IF NOT EXISTS fx_rates (
    currency TEXT NOT NULL,
    day      TEXT NOT NULL,
    rate     TEXT NOT NULL,
    PRIMARY KEY (currency, day)
);

-- API calls made per service and UTC day (daily caps, Settings → Blockchain APIs or Price sources).
CREATE TABLE IF NOT EXISTS api_usage (
    service TEXT NOT NULL,
    day     TEXT NOT NULL,
    calls   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (service, day)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    wallet_id INTEGER,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""


class Database:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    # Columns added after the first release: (table, column, definition).
    ADDED_COLUMNS = (
        ("wallets", "platform", "TEXT NOT NULL DEFAULT ''"),
        ("movements", "from_address", "TEXT NOT NULL DEFAULT ''"),
        ("movements", "to_address", "TEXT NOT NULL DEFAULT ''"),
    )

    def _migrate(self) -> None:
        """Bring a database created by an older version up to SCHEMA_VERSION."""
        for table, column, definition in self.ADDED_COLUMNS:
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def get_meta(self, key: str) -> Optional[str]:
        return self.scalar("SELECT value FROM meta WHERE key=?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def close(self) -> None:
        with self._lock:
            self.conn.close()
