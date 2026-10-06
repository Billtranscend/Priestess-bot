"""Persistent, credential-free work queue. No NoneBot imports."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def daily_slot(now: datetime) -> str:
    local = now.astimezone(TZ)
    last = local.replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    if last > local:
        last -= timedelta(days=1)
    return last.isoformat()


class Queue:
    def __init__(self, path: Path):
        self.path = path
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError("unsafe queue path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        if path.stat().st_uid != os.getuid():
            raise RuntimeError("queue owner mismatch")
        path.chmod(0o600)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pending (
                    user_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL,
                    reason TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS results (
                    user_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                    updated REAL NOT NULL, records INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _enqueue(db, user_id, reason):
        db.execute("""INSERT INTO pending(user_id,generation,reason) VALUES (?,1,?)
            ON CONFLICT(user_id) DO UPDATE SET generation=generation+1,
            reason=excluded.reason, attempts=0, due=0""", (user_id, reason))

    def enqueue(self, user_id: int, reason="bind"):
        with self.connection() as db:
            self._enqueue(db, user_id, reason)

    def initialize_day(self, now: datetime):
        # Installation does not request all existing accounts immediately.
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO meta VALUES ('day',?)", (daily_slot(now),))

    def day_due(self, now: datetime) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='day'").fetchone()
        return row is not None and row[0] != daily_slot(now)

    def enqueue_day(self, now: datetime, users: list[int]) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key='day'").fetchone()
            if row and row[0] == daily_slot(now):
                return 0
            for uid in users:
                self._enqueue(db, uid, "daily")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('day',?)", (daily_slot(now),))
            return len(users)

    def next(self):
        with self.connection() as db:
            return db.execute("""SELECT user_id,generation,reason,attempts FROM pending
                WHERE due<=? ORDER BY CASE reason WHEN 'bind' THEN 0 ELSE 1 END, rowid LIMIT 1""",
                (time.time(),)).fetchone()

    def waiting(self, user_id: int, reason: str = "bind") -> bool:
        """A sync of this kind is queued or running for the user and has not failed yet."""
        with self.connection() as db:
            return db.execute("SELECT 1 FROM pending WHERE user_id=? AND reason=? AND attempts=0",
                              (user_id, reason)).fetchone() is not None

    def finish(self, item, status: str, records=0, retry=False):
        uid, generation, _, attempts = item
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?)",
                       (uid, status, time.time(), records))
            if retry and attempts < 2:
                db.execute("""UPDATE pending SET attempts=attempts+1,due=?
                    WHERE user_id=? AND generation=?""",
                    (time.time() + (300 if attempts == 0 else 1800), uid, generation))
            else:
                db.execute("DELETE FROM pending WHERE user_id=? AND generation=?", (uid, generation))


def record_values(character_id, record):
    return dict(
        character_id=character_id, item_type=record.item_type,
        pool_id=record.poolId, pool_name=record.poolName,
        char_id=record.item_id, char_name=record.item_name,
        rarity=record.rarity, is_new=record.isNew, is_free=record.is_free_pull,
        gacha_ts=record.gacha_ts_sec, pos=record.seq_id_int,
    )
