from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ClaimedTask:
    id: int
    data: str
    weight: int
    attempts: int
    claimed_at: float


class SqlImq:
    """SQLite 任务队列：claim 不删除，ack 删除，release 清占用并退避。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL,
                    weight INTEGER NOT NULL DEFAULT 1,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_at REAL,
                    next_run_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_claim "
                "ON tasks(next_run_at, weight DESC, id ASC)"
            )
            conn.commit()

    def enqueue(self, data: str, weight: int = 1) -> int:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(data, weight, attempts, claimed_at, next_run_at, created_at) "
                "VALUES (?, ?, 0, NULL, ?, ?)",
                (data, max(1, weight), now, now),
            )
            conn.commit()
            return self.size(conn)

    def delete_by_username_substr(self, username: str) -> int:
        """按 data 文本子串匹配删除（与文档一致，非精确字段匹配）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM tasks WHERE data LIKE ?",
                (f"%{username}%",),
            )
            conn.commit()
            return cur.rowcount

    def size(self, conn: sqlite3.Connection | None = None) -> int:
        own = conn is None
        if own:
            conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()
            return int(row["c"])
        finally:
            if own:
                conn.close()

    def claim(
        self,
        *,
        max_attempts: int,
        stale_seconds: int,
    ) -> ClaimedTask | None:
        now = time.time()
        stale_before = now - stale_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, data, weight, attempts, claimed_at, next_run_at
                FROM tasks
                WHERE attempts < ?
                  AND next_run_at <= ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                ORDER BY weight DESC, id ASC
                LIMIT 1
                """,
                (max_attempts, now, stale_before),
            ).fetchone()
            if row is None:
                conn.commit()
                return None

            task_id = int(row["id"])
            attempts = int(row["attempts"]) + 1
            conn.execute(
                "UPDATE tasks SET attempts = ?, claimed_at = ? WHERE id = ?",
                (attempts, now, task_id),
            )
            conn.commit()
            return ClaimedTask(
                id=task_id,
                data=str(row["data"]),
                weight=int(row["weight"]),
                attempts=attempts,
                claimed_at=now,
            )

    def ack(self, task_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()

    def release(self, task_id: int, delay_seconds: float) -> None:
        next_run = time.time() + max(0.0, delay_seconds)
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET claimed_at = NULL, next_run_at = ? WHERE id = ?",
                (next_run, task_id),
            )
            conn.commit()

    def list_exhausted(self, max_attempts: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, data, weight, attempts FROM tasks WHERE attempts >= ?",
                (max_attempts,),
            ).fetchall()
            return [dict(r) for r in rows]
