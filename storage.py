"""SQLite persistence for tracked games, per-user preferences, and bankroll.

Stdlib `sqlite3` is plenty fast for a personal bot. The whole DB is one file
that you can back up by copying.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List


SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
    chat_id          INTEGER PRIMARY KEY,
    min_edge_pct     REAL    NOT NULL,
    min_confidence   INTEGER NOT NULL,
    value_alerts     INTEGER NOT NULL DEFAULT 0,
    bankroll         REAL    NOT NULL DEFAULT 1000.0,
    kelly_fraction   REAL    NOT NULL DEFAULT 0.25
);

CREATE TABLE IF NOT EXISTS tracked_games (
    chat_id          INTEGER NOT NULL,
    event_slug       TEXT    NOT NULL,
    last_seen_prices TEXT    NOT NULL DEFAULT '{}',
    threshold_pp     REAL    NOT NULL DEFAULT 5.0,
    created_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (chat_id, event_slug)
);

CREATE TABLE IF NOT EXISTS sent_alerts (
    chat_id     INTEGER NOT NULL,
    alert_key   TEXT    NOT NULL,
    sent_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (chat_id, alert_key)
);
"""


@dataclass
class UserPrefs:
    chat_id: int
    min_edge_pct: float
    min_confidence: int
    value_alerts: bool
    bankroll: float
    kelly_fraction: float


@dataclass
class TrackedGame:
    chat_id: int
    event_slug: str
    last_seen_prices: Dict[str, float]
    threshold_pp: float


class Store:
    """Thread-safe wrapper around a single SQLite file."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            con = sqlite3.connect(self.path)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            finally:
                con.close()

    # ----- prefs --------------------------------------------------------- #

    def get_prefs(self, chat_id: int, *, default_edge: float, default_conf: int) -> UserPrefs:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM user_prefs WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row:
                return UserPrefs(
                    chat_id=row["chat_id"],
                    min_edge_pct=row["min_edge_pct"],
                    min_confidence=row["min_confidence"],
                    value_alerts=bool(row["value_alerts"]),
                    bankroll=float(row["bankroll"]),
                    kelly_fraction=float(row["kelly_fraction"]),
                )
            c.execute(
                "INSERT INTO user_prefs (chat_id, min_edge_pct, min_confidence) VALUES (?,?,?)",
                (chat_id, default_edge, default_conf),
            )
            return UserPrefs(chat_id, default_edge, default_conf, False, 1000.0, 0.25)

    def set_pref(self, chat_id: int, **fields) -> None:
        if not fields:
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO user_prefs (chat_id, min_edge_pct, min_confidence) VALUES (?,?,?)",
                (chat_id, 4.0, 55),
            )
            keys = ", ".join(f"{k} = ?" for k in fields)
            values = list(fields.values()) + [chat_id]
            c.execute(f"UPDATE user_prefs SET {keys} WHERE chat_id = ?", values)

    def all_value_alert_users(self) -> List[UserPrefs]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM user_prefs WHERE value_alerts = 1"
            ).fetchall()
            return [
                UserPrefs(
                    r["chat_id"], r["min_edge_pct"], r["min_confidence"],
                    True, float(r["bankroll"]), float(r["kelly_fraction"]),
                )
                for r in rows
            ]

    # ----- tracked games ------------------------------------------------- #

    def track(self, chat_id: int, event_slug: str, threshold_pp: float = 5.0) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO tracked_games "
                "(chat_id, event_slug, last_seen_prices, threshold_pp) "
                "VALUES (?, ?, COALESCE("
                "    (SELECT last_seen_prices FROM tracked_games WHERE chat_id=? AND event_slug=?),"
                "    '{}'"
                "), ?)",
                (chat_id, event_slug, chat_id, event_slug, threshold_pp),
            )

    def untrack(self, chat_id: int, event_slug: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM tracked_games WHERE chat_id = ? AND event_slug = ?",
                (chat_id, event_slug),
            )
            return cur.rowcount > 0

    def list_tracked(self, chat_id: int) -> List[TrackedGame]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM tracked_games WHERE chat_id = ?", (chat_id,)
            ).fetchall()
            return [
                TrackedGame(
                    chat_id=r["chat_id"],
                    event_slug=r["event_slug"],
                    last_seen_prices=json.loads(r["last_seen_prices"] or "{}"),
                    threshold_pp=r["threshold_pp"],
                )
                for r in rows
            ]

    def all_tracked(self) -> List[TrackedGame]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM tracked_games").fetchall()
            return [
                TrackedGame(
                    chat_id=r["chat_id"],
                    event_slug=r["event_slug"],
                    last_seen_prices=json.loads(r["last_seen_prices"] or "{}"),
                    threshold_pp=r["threshold_pp"],
                )
                for r in rows
            ]

    def update_tracked_prices(self, chat_id: int, event_slug: str, prices: Dict[str, float]) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE tracked_games SET last_seen_prices = ? WHERE chat_id = ? AND event_slug = ?",
                (json.dumps(prices), chat_id, event_slug),
            )

    # ----- alert dedup --------------------------------------------------- #

    def already_sent(self, chat_id: int, alert_key: str, *, ttl_seconds: int = 6 * 3600) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT sent_at FROM sent_alerts WHERE chat_id = ? AND alert_key = ?",
                (chat_id, alert_key),
            ).fetchone()
            if not row:
                return False
            cur = c.execute("SELECT strftime('%s','now') AS now").fetchone()
            try:
                age = int(cur["now"]) - int(row["sent_at"])
            except (TypeError, ValueError):
                age = 0
            return age < ttl_seconds

    def mark_sent(self, chat_id: int, alert_key: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO sent_alerts (chat_id, alert_key) VALUES (?, ?)",
                (chat_id, alert_key),
            )
