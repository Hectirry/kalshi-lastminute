"""SQLite: esquema y escritor por lotes.

Tolerancia a reinicios: WAL + claves únicas + INSERT OR IGNORE. Un reinicio
nunca duplica mercados, liquidaciones ni operaciones (una por mercado y
estrategia).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker        TEXT PRIMARY KEY,
    asset         TEXT NOT NULL DEFAULT 'BTC',
    event_ticker  TEXT,
    open_ms       INTEGER NOT NULL,
    close_ms      INTEGER NOT NULL,
    floor_strike  REAL,
    strike_type   TEXT,
    rules_primary TEXT,
    raw_json      TEXT,
    first_seen_ms INTEGER NOT NULL
);

-- Ticks crudos por exchange y el índice compuesto (source='composite', 1/s).
CREATE TABLE IF NOT EXISTS ticks_index (
    id         INTEGER PRIMARY KEY,
    ts_ms      INTEGER NOT NULL,   -- reloj local al recibir
    asset      TEXT NOT NULL DEFAULT 'BTC',
    source     TEXT NOT NULL,
    bid        REAL,
    ask        REAL,
    mid        REAL NOT NULL,
    exch_ts_ms INTEGER             -- reloj del exchange, si lo envía
);

-- Mensajes crudos del canal cfbenchmarks_value de Kalshi (índices oficiales). El valor
-- también va a ticks_index con source='oficial'.
CREATE TABLE IF NOT EXISTS brti_raw (
    id      INTEGER PRIMARY KEY,
    ts_ms   INTEGER NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id            INTEGER PRIMARY KEY,
    ts_ms         INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    secs_to_close REAL NOT NULL,
    origin        TEXT NOT NULL,   -- 'rest' | 'ws'
    yes_bids      TEXT NOT NULL,   -- JSON [[precio, cantidad], ...] ascendente; en la
                                   -- ventana final por WS, solo los mejores niveles
    no_bids       TEXT NOT NULL,
    best_yes_bid  REAL,
    best_yes_ask  REAL,            -- = 1 - mejor bid NO
    yes_ask_size  REAL,
    best_no_bid   REAL,
    best_no_ask   REAL,            -- = 1 - mejor bid YES
    no_ask_size   REAL
);
CREATE INDEX IF NOT EXISTS ix_ob_ticker_ts ON orderbook_snapshots(ticker, ts_ms);

-- Incluye las decisiones de NO entrar (action='skip') y su razón.
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    ts_ms         INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    action        TEXT NOT NULL,   -- 'enter' | 'skip'
    side          TEXT,            -- 'yes' | 'no'
    reason        TEXT,
    model_prob    REAL,
    yes_ask       REAL,
    no_ask        REAL,
    edge_net      REAL,
    secs_to_close REAL,
    index_value   REAL,
    strike        REAL,
    context_json  TEXT
);
CREATE INDEX IF NOT EXISTS ix_dec_ticker ON decisions(ticker, strategy);

CREATE TABLE IF NOT EXISTS paper_trades (
    id                  INTEGER PRIMARY KEY,
    decision_id         INTEGER REFERENCES decisions(id),
    ticker              TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    side                TEXT NOT NULL,
    decided_ms          INTEGER NOT NULL,
    fill_ms             INTEGER,
    status              TEXT NOT NULL,  -- 'pending' | 'filled' | 'partial' | 'missed'
    requested_contracts REAL NOT NULL,
    filled_contracts    REAL NOT NULL DEFAULT 0,
    decision_price      REAL NOT NULL,
    fill_price          REAL,
    fee                 REAL NOT NULL DEFAULT 0,
    pnl                 REAL,           -- NULL hasta la liquidación
    UNIQUE (ticker, strategy)           -- una sola posición por mercado
);

CREATE TABLE IF NOT EXISTS settlements (
    ticker           TEXT PRIMARY KEY,
    close_ms         INTEGER NOT NULL,
    result           TEXT NOT NULL,     -- 'yes' | 'no'
    expiration_value REAL,
    floor_strike     REAL,
    recorded_ms      INTEGER NOT NULL,
    est_avg          REAL,              -- nuestro promedio del índice en la ventana final
    est_n            INTEGER,           -- segundos con dato dentro de la ventana
    raw_json         TEXT
);
"""

# Columnas añadidas después de la primera versión del esquema.
MIGRATIONS = {
    "ticks_index": {"asset": "TEXT NOT NULL DEFAULT 'BTC'"},
    "markets": {"asset": "TEXT NOT NULL DEFAULT 'BTC'"},
    "decisions": {"asset": "TEXT"},
    "paper_trades": {"asset": "TEXT", "model_prob": "REAL", "edge_net": "REAL",
                     "miss_reason": "TEXT", "latency_ms": "INTEGER"},
}

_INSERTS = {
    "decisions": (
        "INSERT INTO decisions (ts_ms, ticker, strategy, action, side, reason, model_prob, "
        "yes_ask, no_ask, edge_net, secs_to_close, index_value, strike, context_json, asset) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    ),
    "ticks_index": (
        "INSERT INTO ticks_index (ts_ms, asset, source, bid, ask, mid, exch_ts_ms) VALUES (?,?,?,?,?,?,?)"
    ),
    "orderbook_snapshots": (
        "INSERT INTO orderbook_snapshots (ts_ms, ticker, secs_to_close, origin, yes_bids, "
        "no_bids, best_yes_bid, best_yes_ask, yes_ask_size, best_no_bid, best_no_ask, "
        "no_ask_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    ),
    "brti_raw": "INSERT INTO brti_raw (ts_ms, payload) VALUES (?,?)",
    "markets": (
        "INSERT OR IGNORE INTO markets (ticker, asset, event_ticker, open_ms, close_ms, "
        "floor_strike, strike_type, rules_primary, raw_json, first_seen_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)"
    ),
    "settlements": (
        "INSERT OR IGNORE INTO settlements (ticker, close_ms, result, expiration_value, "
        "floor_strike, recorded_ms, est_avg, est_n, raw_json) VALUES (?,?,?,?,?,?,?,?,?)"
    ),
}


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # Migración: las bases creadas cuando solo se grababa BTC no tienen `asset`.
    for table, columns in MIGRATIONS.items():
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        for name, decl in columns.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.execute("DROP INDEX IF EXISTS ix_ticks_source_ts")
    conn.execute("DROP TABLE IF EXISTS orderbook_deltas")  # diseño descartado: demasiado volumen
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ticks_asset_source_ts "
                 "ON ticks_index(asset, source, ts_ms)")
    conn.commit()
    return conn


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    """Conexión de solo lectura para reportes: no migra ni bloquea al bot en marcha."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class Database:
    """Acumula filas en memoria y las confirma por lotes con flush()."""

    def __init__(self, path: Path | str) -> None:
        self.conn = connect(path)
        self._pending: dict[str, list[tuple]] = {t: [] for t in _INSERTS}

    def add(self, table: str, row: tuple) -> None:
        self._pending[table].append(row)

    def flush(self) -> int:
        n = 0
        with self.conn:  # una transacción por lote: o entra todo o nada
            for table, rows in self._pending.items():
                if rows:
                    self.conn.executemany(_INSERTS[table], rows)
                    n += len(rows)
                    rows.clear()
        return n

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.flush()
        self.conn.close()
