"""SQLite persistence: observations, runs, FX cache, and per-key state."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    product_id TEXT NOT NULL,
    group_id TEXT NOT NULL,          -- component id or bundle id
    is_bundle INTEGER NOT NULL DEFAULT 0,
    source_id TEXT NOT NULL,
    seller TEXT,
    url TEXT,
    ok INTEGER NOT NULL,
    error TEXT,
    sticker REAL,
    currency TEXT,
    landed REAL,
    landed_no_refund REAL,
    in_stock INTEGER,                -- 1/0/NULL
    fx_rate REAL,
    method TEXT,
    title TEXT,
    sku TEXT
);
CREATE INDEX IF NOT EXISTS obs_lookup ON observations(product_id, group_id, source_id, ts);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL,
    ts_start TEXT NOT NULL,
    ts_end TEXT,
    sources_total INTEGER,
    sources_ok INTEGER
);
CREATE TABLE IF NOT EXISTS fx (
    date TEXT NOT NULL,
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (date, base, quote)
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated TEXT NOT NULL
);
"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")

    def close(self):
        self.db.close()

    # ------------------------------------------------------------- observations
    def add_observation(self, **row: Any) -> int:
        row.setdefault("ts", utcnow())
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        cur = self.db.execute(f"INSERT INTO observations ({cols}) VALUES ({qs})", list(row.values()))
        self.db.commit()
        return int(cur.lastrowid)

    def observation(self, obs_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()

    def latest_per_source(self, product_id: str, group_id: str, ok_only: bool = True) -> dict[str, sqlite3.Row]:
        """Latest observation for each source of a group (component/bundle)."""
        q = """
        SELECT o.* FROM observations o
        JOIN (SELECT source_id, MAX(id) AS mid FROM observations
              WHERE product_id=? AND group_id=? {okf} GROUP BY source_id) m
        ON o.id = m.mid
        """.format(okf="AND ok=1" if ok_only else "")
        return {r["source_id"]: r for r in self.db.execute(q, (product_id, group_id))}

    def last_source_run(self, product_id: str, group_id: str, source_id: str) -> dt.datetime | None:
        r = self.db.execute(
            "SELECT MAX(ts) AS ts FROM observations WHERE product_id=? AND group_id=? AND source_id=?",
            (product_id, group_id, source_id),
        ).fetchone()
        return dt.datetime.fromisoformat(r["ts"]) if r and r["ts"] else None

    def previous_ok(self, product_id: str, group_id: str, source_id: str, before_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM observations WHERE product_id=? AND group_id=? AND source_id=? AND ok=1 AND id<? "
            "ORDER BY id DESC LIMIT 1",
            (product_id, group_id, source_id, before_id),
        ).fetchone()

    def history(self, product_id: str, group_id: str, days: int = 90) -> list[sqlite3.Row]:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        return list(
            self.db.execute(
                "SELECT ts, source_id, seller, sticker, currency, landed, in_stock FROM observations "
                "WHERE product_id=? AND group_id=? AND ok=1 AND ts>=? ORDER BY ts",
                (product_id, group_id, since),
            )
        )

    def consecutive_failures(self, product_id: str, group_id: str, source_id: str) -> int:
        n = 0
        for r in self.db.execute(
            "SELECT ok FROM observations WHERE product_id=? AND group_id=? AND source_id=? ORDER BY id DESC LIMIT 20",
            (product_id, group_id, source_id),
        ):
            if r["ok"]:
                break
            n += 1
        return n

    # --------------------------------------------------------------------- runs
    def start_run(self, product_id: str) -> int:
        cur = self.db.execute("INSERT INTO runs (product_id, ts_start) VALUES (?, ?)", (product_id, utcnow()))
        self.db.commit()
        return int(cur.lastrowid)

    def last_run(self, product_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs WHERE product_id=? AND ts_end IS NOT NULL ORDER BY id DESC LIMIT 1", (product_id,)
        ).fetchone()

    def end_run(self, run_id: int, total: int, ok: int):
        self.db.execute(
            "UPDATE runs SET ts_end=?, sources_total=?, sources_ok=? WHERE id=?", (utcnow(), total, ok, run_id)
        )
        self.db.commit()

    # ----------------------------------------------------------------------- fx
    def get_fx(self, date: str, base: str, quote: str) -> float | None:
        r = self.db.execute("SELECT rate FROM fx WHERE date=? AND base=? AND quote=?", (date, base, quote)).fetchone()
        return float(r["rate"]) if r else None

    def latest_fx(self, base: str, quote: str) -> float | None:
        r = self.db.execute(
            "SELECT rate FROM fx WHERE base=? AND quote=? ORDER BY date DESC LIMIT 1", (base, quote)
        ).fetchone()
        return float(r["rate"]) if r else None

    def put_fx(self, date: str, base: str, quote: str, rate: float):
        self.db.execute("INSERT OR REPLACE INTO fx VALUES (?,?,?,?)", (date, base, quote, rate))
        self.db.commit()

    # -------------------------------------------------------------------- state
    def get_state(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_state(self, key: str, value):
        self.db.execute(
            "INSERT OR REPLACE INTO state (key, value, updated) VALUES (?,?,?)", (key, json.dumps(value), utcnow())
        )
        self.db.commit()
