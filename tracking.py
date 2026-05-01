"""SQLite tracking store: who is tracking what, with the last seen forecast."""
import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple


class TrackingDB:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA journal_mode=WAL;")
        return c

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    chat_id    INTEGER NOT NULL,
                    airport    TEXT    NOT NULL,
                    last_c     REAL,
                    last_f     REAL,
                    last_check TEXT,
                    created_at TEXT    NOT NULL,
                    UNIQUE(user_id, airport)
                )
                """
            )
            # Migration: add last_bucket column on existing DBs (idempotent).
            cols = {row[1] for row in c.execute("PRAGMA table_info(tracking)")}
            if "last_bucket" not in cols:
                c.execute("ALTER TABLE tracking ADD COLUMN last_bucket TEXT")

    def add(self, user_id: int, chat_id: int, airport: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO tracking "
                "(user_id, chat_id, airport, created_at) VALUES (?,?,?,?)",
                (user_id, chat_id, airport.upper(), datetime.utcnow().isoformat()),
            )
            return cur.rowcount > 0

    def remove(self, user_id: int, airport: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM tracking WHERE user_id=? AND airport=?",
                (user_id, airport.upper()),
            )
            return cur.rowcount > 0

    def list_user(
        self, user_id: int
    ) -> List[Tuple[str, Optional[float], Optional[float], Optional[str]]]:
        with self._conn() as c:
            return c.execute(
                "SELECT airport, last_c, last_f, last_check "
                "FROM tracking WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()

    def list_all(
        self,
    ) -> List[Tuple[int, int, int, str, Optional[float], Optional[float], Optional[str]]]:
        with self._conn() as c:
            return c.execute(
                "SELECT id, user_id, chat_id, airport, last_c, last_f, last_bucket "
                "FROM tracking"
            ).fetchall()

    def update_last(
        self,
        row_id: int,
        temp_c: float,
        temp_f: float,
        bucket: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE tracking "
                "SET last_c=?, last_f=?, last_bucket=?, last_check=? WHERE id=?",
                (temp_c, temp_f, bucket, datetime.utcnow().isoformat(), row_id),
            )
