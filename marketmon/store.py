"""SQLite state: items, clusters, lag samples, poll log, events, alerts, control flags."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .models import Item, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source TEXT NOT NULL, id TEXT NOT NULL,
    title TEXT, url TEXT, published_at TEXT, precision TEXT, first_seen_at TEXT NOT NULL,
    source_rank INTEGER, symbol TEXT, isin TEXT, entity TEXT, category TEXT,
    matched_symbol TEXT, heavyweight INTEGER, label TEXT, priority INTEGER, drop_reason TEXT,
    cluster_id INTEGER, role TEXT, simhash TEXT, baseline INTEGER, raw TEXT, impact TEXT,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS items_seen ON items(first_seen_at);
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, lead_source TEXT, lead_id TEXT,
    title TEXT, url TEXT, symbol TEXT, label TEXT, priority INTEGER,
    members INTEGER DEFAULT 1, followups INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lag (
    source TEXT, item_id TEXT, published_at TEXT, first_seen_at TEXT, lag_s REAL, day TEXT
);
CREATE INDEX IF NOT EXISTS lag_day ON lag(day);
CREATE TABLE IF NOT EXISTS polls (
    at TEXT, source TEXT, endpoint TEXT, status TEXT, http INTEGER, elapsed_ms REAL,
    n_items INTEGER, n_new INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS polls_at ON polls(at);
CREATE TABLE IF NOT EXISTS events (at TEXT, source TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, priority INTEGER, source TEXT,
    cluster_id INTEGER, headline TEXT, url TEXT, latency_s REAL, delivered TEXT
);
CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ticks (symbol TEXT, at TEXT, last REAL);
CREATE INDEX IF NOT EXISTS ticks_sym_at ON ticks(symbol, at);
"""


def _iso(dt: datetime | None) -> str | None:
    """Always UTC, so stored timestamps sort and compare correctly as strings."""
    return dt.astimezone(timezone.utc).isoformat() if dt else None


class Store:
    """Thin wrapper over one sqlite3 connection. Safe to share with the web thread."""

    def __init__(self, path: str = "marketmon.db"):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        if "impact" not in cols:      # databases created before the index-impact tag existed
            self.db.execute("ALTER TABLE items ADD COLUMN impact TEXT")

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    # ------------------------------------------------------------------ items
    def known(self, source: str, item_id: str) -> bool:
        return bool(self.q("SELECT 1 FROM items WHERE source=? AND id=?", (source, item_id)))

    def add_item(self, it: Item, *, cls, cluster_id: int, role: str, simhash: int, baseline: bool) -> None:
        self.x("""INSERT OR IGNORE INTO items VALUES
                  (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            it.source, it.id, it.title, it.url, _iso(it.published_at), it.published_precision,
            _iso(it.first_seen_at), it.source_rank, it.symbol, it.isin, it.entity, it.category,
            cls.symbol, int(cls.heavyweight), cls.label, cls.priority, cls.drop_reason,
            cluster_id, role, format(simhash, "016x"), int(baseline),
            json.dumps(it.raw, default=str, ensure_ascii=False)[:20000], getattr(cls, "impact", None)))

    def recent_items(self, hours: float) -> list[sqlite3.Row]:
        since = _iso(now_utc() - timedelta(hours=hours))
        return self.q("SELECT * FROM items WHERE first_seen_at >= ? ORDER BY first_seen_at", (since,))

    # --------------------------------------------------------------- clusters
    def new_cluster(self, it: Item, cls) -> int:
        return self.x("""INSERT INTO clusters (created_at, lead_source, lead_id, title, url, symbol, label, priority)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (_iso(it.first_seen_at), it.source, it.id, it.title, it.url, cls.symbol, cls.label, cls.priority))

    def join_cluster(self, cluster_id: int, role: str) -> None:
        col = "followups" if role == "followup" else "members"
        self.x(f"UPDATE clusters SET {col} = {col} + 1 WHERE id=?", (cluster_id,))

    # ---------------------------------------------------- instrumentation
    def add_lag(self, it: Item) -> None:
        from .models import IST
        self.x("INSERT INTO lag VALUES (?,?,?,?,?,?)", (
            it.source, it.id, _iso(it.published_at), _iso(it.first_seen_at), it.lag_seconds,
            it.first_seen_at.astimezone(IST).date().isoformat()))

    def add_poll(self, source: str, endpoint: str, status: str, http: int | None,
                 elapsed_ms: float | None, n_items: int, n_new: int, error: str | None) -> None:
        self.x("INSERT INTO polls VALUES (?,?,?,?,?,?,?,?,?)", (
            _iso(now_utc()), source, endpoint, status, http, elapsed_ms, n_items, n_new, error))

    def add_event(self, source: str, kind: str, detail: str = "") -> None:
        self.x("INSERT INTO events VALUES (?,?,?,?)", (_iso(now_utc()), source, kind, detail[:500]))

    def add_alert(self, priority: int, source: str, cluster_id: int, headline: str, url: str,
                  latency_s: float | None, delivered: str) -> None:
        self.x("INSERT INTO alerts (at, priority, source, cluster_id, headline, url, latency_s, delivered)"
               " VALUES (?,?,?,?,?,?,?,?)",
               (_iso(now_utc()), priority, source, cluster_id, headline, url, latency_s, delivered))

    # ------------------------------------------------------------------ ticks
    def add_ticks(self, rows: list[tuple[str, str, float]]) -> None:
        with self.lock:
            self.db.executemany("INSERT INTO ticks VALUES (?,?,?)", rows)

    def ticks_since(self, symbols: list[str], since: str) -> dict[str, list[list]]:
        if not symbols:
            return {}
        marks = ",".join("?" * len(symbols))
        out: dict[str, list[list]] = {}
        for r in self.q(f"SELECT symbol, at, last FROM ticks WHERE at >= ? AND symbol IN ({marks}) ORDER BY at",
                        (since, *symbols)):
            out.setdefault(r["symbol"], []).append([r["at"], r["last"]])
        return out

    def prune_ticks(self, days: int = 3) -> None:
        self.x("DELETE FROM ticks WHERE at < ?", (_iso(now_utc() - timedelta(days=days)),))

    # ---------------------------------------------------------------- control
    def get_flag(self, key: str) -> bool:
        r = self.q("SELECT value FROM control WHERE key=?", (key,))
        return bool(r) and r[0]["value"] == "1"

    def set_flag(self, key: str, on: bool) -> None:
        self.x("INSERT INTO control VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, "1" if on else "0"))
