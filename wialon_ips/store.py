"""Durable outbox (SQLite).

Every position is written here before it is sent to Wialon, so nothing is lost
on restart or while Wialon / a unit is unavailable. (imei, gps_time) is unique,
which makes re-reading overlapping Jimi history harmless.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone

from .protocol import Position

PENDING, SENT, REJECTED, EXPIRED = "pending", "sent", "rejected", "expired"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    imei        TEXT    NOT NULL,
    gps_time    INTEGER NOT NULL,
    payload     TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  INTEGER NOT NULL,
    sent_at     INTEGER,
    UNIQUE (imei, gps_time)
);
CREATE INDEX IF NOT EXISTS messages_pending ON messages (status, imei, gps_time);
CREATE TABLE IF NOT EXISTS cursors (
    imei       TEXT PRIMARY KEY,
    last_time  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS allowed_imeis (
    imei      TEXT PRIMARY KEY,
    source    TEXT NOT NULL,
    added_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _encode(pos: Position) -> str:
    d = asdict(pos)
    d["time"] = int(pos.time.timestamp())
    return json.dumps(d)


def _decode(payload: str) -> Position:
    d = json.loads(payload)
    d["time"] = datetime.fromtimestamp(d["time"], timezone.utc)
    return Position(**d)


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        # every IMEI ever seen through the Jimi API (cursors) is one of our Tags
        self.db.execute("INSERT OR IGNORE INTO allowed_imeis(imei, source, added_at) "
                        "SELECT imei, 'jimi', ? FROM cursors", (int(time.time()),))

    def close(self) -> None:
        self.db.close()

    # ---------- key/value ----------

    def get_json(self, key: str) -> dict | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_json(self, key: str, value: dict) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))

    # ---------- cursors ----------

    def cursor(self, imei: str) -> datetime | None:
        row = self.db.execute("SELECT last_time FROM cursors WHERE imei=?", (imei,)).fetchone()
        return datetime.fromtimestamp(row[0], timezone.utc) if row else None

    def set_cursor(self, imei: str, t: datetime) -> None:
        self.db.execute("INSERT INTO cursors(imei, last_time) VALUES(?, ?) "
                        "ON CONFLICT(imei) DO UPDATE SET last_time=max(last_time, excluded.last_time)",
                        (imei, int(t.timestamp())))

    # ---------- allowlist ----------
    # Wialon IPS logs in by unique ID across the whole Wialon hosting, so an IMEI that
    # is not ours could land in another customer's unit. Only allowed IMEIs are sent.

    def allow(self, imeis, source: str) -> int:
        before = self.db.total_changes
        now = int(time.time())
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO allowed_imeis(imei, source, added_at) "
                                "VALUES(?, ?, ?)", [(i, source, now) for i in imeis])
        return self.db.total_changes - before

    def disallow(self, imei: str) -> bool:
        return self.db.execute("DELETE FROM allowed_imeis WHERE imei=?", (imei,)).rowcount > 0

    def is_allowed(self, imei: str) -> bool:
        return self.db.execute("SELECT 1 FROM allowed_imeis WHERE imei=?", (imei,)).fetchone() is not None

    def allowed(self) -> list[dict]:
        return [{"imei": i, "source": s, "added_at": datetime.fromtimestamp(t, timezone.utc).isoformat()}
                for i, s, t in self.db.execute(
                    "SELECT imei, source, added_at FROM allowed_imeis ORDER BY imei")]

    # ---------- messages ----------

    def enqueue(self, imei: str, positions: list[Position]) -> int:
        """Insert positions, ignoring ones already stored. Returns how many were new."""
        now = int(time.time())
        with self.db:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO messages(imei, gps_time, payload, created_at) VALUES(?,?,?,?)",
                [(imei, int(p.time.timestamp()), _encode(p), now) for p in positions])
            return self.db.total_changes - before

    def imeis_with_pending(self, allowed_only: bool = False) -> list[str]:
        sql = "SELECT DISTINCT imei FROM messages WHERE status=?"
        if allowed_only:
            sql += " AND imei IN (SELECT imei FROM allowed_imeis)"
        return [r[0] for r in self.db.execute(sql, (PENDING,))]

    def pending(self, imei: str, limit: int) -> list[tuple[int, Position]]:
        rows = self.db.execute(
            "SELECT id, payload FROM messages WHERE status=? AND imei=? ORDER BY gps_time LIMIT ?",
            (PENDING, imei, limit)).fetchall()
        return [(i, _decode(p)) for i, p in rows]

    def mark_sent(self, ids: list[int]) -> None:
        now = int(time.time())
        with self.db:
            self.db.executemany("UPDATE messages SET status=?, sent_at=?, attempts=attempts+1, "
                                "last_error=NULL WHERE id=?", [(SENT, now, i) for i in ids])

    def mark_rejected(self, msg_id: int, reason: str) -> None:
        self.db.execute("UPDATE messages SET status=?, attempts=attempts+1, last_error=? WHERE id=?",
                        (REJECTED, reason, msg_id))

    def note_failure(self, imei: str, reason: str) -> None:
        self.db.execute("UPDATE messages SET attempts=attempts+1, last_error=? "
                        "WHERE status=? AND imei=?", (reason, PENDING, imei))

    def prune(self, keep_sent_days: int, max_pending_days: int) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM messages WHERE status IN (?, ?) AND created_at < ?",
                            (SENT, EXPIRED, now - keep_sent_days * 86400))
            self.db.execute("UPDATE messages SET status=?, last_error='too old, gave up' "
                            "WHERE status=? AND created_at < ?",
                            (EXPIRED, PENDING, now - max_pending_days * 86400))

    def stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for imei, status, n in self.db.execute(
                "SELECT imei, status, count(*) FROM messages GROUP BY imei, status"):
            out.setdefault(imei, {})[status] = n
        return out

    def recent_errors(self, imei: str, limit: int = 5) -> list[dict]:
        return [{"gps_time": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                 "status": s, "error": e}
                for t, s, e in self.db.execute(
                    "SELECT gps_time, status, last_error FROM messages "
                    "WHERE imei=? AND last_error IS NOT NULL ORDER BY gps_time DESC LIMIT ?",
                    (imei, limit))]
