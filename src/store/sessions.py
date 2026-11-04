from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "running", "waiting_captcha", "waiting_security"})


@dataclass
class SessionRecord:
    id: str
    username: str
    password: str | None
    priority: int
    client_ref: str | None
    webhook_url: str | None
    silent: bool
    status: str
    cancel_requested: bool
    attempts: int
    claimed_at: float | None
    next_run_at: float
    challenge: dict[str, Any] | None
    human_input: dict[str, Any] | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: float
    updated_at: float
    finished_at: float | None

    def to_public_dict(self, *, include_challenge_inline: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "username": self.username,
            "client_ref": self.client_ref,
            "priority": self.priority,
            "silent": self.silent,
            "cancel_requested": self.cancel_requested,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
            "challenge": self.challenge if include_challenge_inline else None,
            "result": self.result,
            "error": self.error,
        }
        return out


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _new_id() -> str:
    return uuid.uuid4().hex


class SessionStore:
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
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    password TEXT,
                    priority INTEGER NOT NULL DEFAULT 1,
                    client_ref TEXT,
                    webhook_url TEXT,
                    silent INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_at REAL,
                    next_run_at REAL NOT NULL DEFAULT 0,
                    challenge_json TEXT,
                    human_input_json TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_claim "
                "ON sessions(status, next_run_at, priority DESC, created_at ASC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_username_status "
                "ON sessions(username, status)"
            )
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "silent" not in cols:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN silent INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SessionRecord:
        def loads(raw: str | None) -> dict[str, Any] | None:
            if not raw:
                return None
            return json.loads(raw)

        keys = row.keys()
        return SessionRecord(
            id=str(row["id"]),
            username=str(row["username"]),
            password=row["password"],
            priority=int(row["priority"]),
            client_ref=row["client_ref"],
            webhook_url=row["webhook_url"],
            silent=bool(row["silent"]) if "silent" in keys else False,
            status=str(row["status"]),
            cancel_requested=bool(row["cancel_requested"]),
            attempts=int(row["attempts"]),
            claimed_at=row["claimed_at"],
            next_run_at=float(row["next_run_at"]),
            challenge=loads(row["challenge_json"]),
            human_input=loads(row["human_input_json"]),
            result=loads(row["result_json"]),
            error=loads(row["error_json"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            finished_at=row["finished_at"],
        )

    def create(
        self,
        *,
        username: str,
        password: str,
        webhook_url: str | None,
        priority: int,
        client_ref: str | None,
        silent: bool = False,
    ) -> tuple[SessionRecord, int]:
        """取消同 username 的未终态会话后入队。返回 (record, queue_position)。"""
        now = time.time()
        sid = _new_id()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old_rows = conn.execute(
                "SELECT id, status FROM sessions WHERE username = ? AND status IN "
                "('queued','running','waiting_captcha','waiting_security')",
                (username,),
            ).fetchall()
            for old in old_rows:
                oid = str(old["id"])
                if old["status"] == "queued":
                    conn.execute(
                        "UPDATE sessions SET status='cancelled', cancel_requested=1, "
                        "password=NULL, error_json=?, finished_at=?, updated_at=? WHERE id=?",
                        (
                            json.dumps(
                                {"code": "cancelled", "message": "被新会话覆盖"},
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                            oid,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE sessions SET cancel_requested=1, updated_at=? WHERE id=?",
                        (now, oid),
                    )

            conn.execute(
                """
                INSERT INTO sessions(
                    id, username, password, priority, client_ref, webhook_url,
                    silent, status, cancel_requested, attempts, claimed_at, next_run_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, NULL, ?, ?, ?)
                """,
                (
                    sid,
                    username,
                    password,
                    max(1, priority),
                    client_ref,
                    webhook_url,
                    1 if silent else 0,
                    now,
                    now,
                    now,
                ),
            )
            ahead = conn.execute(
                """
                SELECT COUNT(*) AS c FROM sessions
                WHERE status = 'queued'
                  AND (
                    priority > ?
                    OR (priority = ? AND created_at <= ?)
                  )
                """,
                (max(1, priority), max(1, priority), now),
            ).fetchone()
            conn.commit()
            position = int(ahead["c"])
        record = self.get(sid)
        assert record is not None
        return record, position

    def get(self, session_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._row_to_record(row) if row else None

    def queue_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM sessions GROUP BY status"
            ).fetchall()
            current = conn.execute(
                """
                SELECT id FROM sessions
                WHERE status IN ('running','waiting_captcha','waiting_security')
                ORDER BY COALESCE(claimed_at, 0) DESC, updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        counts = {str(r["status"]): int(r["c"]) for r in rows}
        return {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "waiting": counts.get("waiting_captcha", 0)
            + counts.get("waiting_security", 0),
            "retrying": 0,
            "current_session_id": str(current["id"]) if current else None,
        }

    def claim(self, *, max_attempts: int, stale_seconds: int) -> SessionRecord | None:
        now = time.time()
        stale_before = now - stale_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM sessions
                WHERE status = 'queued'
                  AND attempts < ?
                  AND next_run_at <= ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                  AND cancel_requested = 0
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (max_attempts, now, stale_before),
            ).fetchone()
            if row is None:
                # 捡回 stale 的 running（进程崩溃）
                row = conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE status IN ('running','waiting_captcha','waiting_security')
                      AND attempts < ?
                      AND claimed_at IS NOT NULL AND claimed_at < ?
                      AND cancel_requested = 0
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    """,
                    (max_attempts, stale_before),
                ).fetchone()
            if row is None:
                conn.commit()
                return None

            sid = str(row["id"])
            attempts = int(row["attempts"]) + 1
            conn.execute(
                """
                UPDATE sessions
                SET status='running', attempts=?, claimed_at=?, updated_at=?,
                    challenge_json=NULL, human_input_json=NULL
                WHERE id=?
                """,
                (attempts, now, now, sid),
            )
            conn.commit()
        return self.get(sid)

    def set_status(
        self,
        session_id: str,
        status: str,
        *,
        challenge: dict[str, Any] | None = None,
        clear_challenge: bool = False,
        clear_human_input: bool = False,
    ) -> SessionRecord | None:
        """仅非终态且未取消时可改状态，防止取消后被 waiting/running「复活」。"""
        now = time.time()
        with self._connect() as conn:
            fields = ["status=?", "updated_at=?"]
            params: list[Any] = [status, now]
            if challenge is not None:
                fields.append("challenge_json=?")
                params.append(json.dumps(challenge, ensure_ascii=False))
            elif clear_challenge:
                fields.append("challenge_json=NULL")
            if clear_human_input:
                fields.append("human_input_json=NULL")
            params.append(session_id)
            cur = conn.execute(
                f"""
                UPDATE sessions SET {', '.join(fields)}
                WHERE id=?
                  AND status NOT IN ('succeeded','failed','cancelled')
                  AND cancel_requested = 0
                """,
                params,
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(session_id)

    def mark_running(self, session_id: str) -> None:
        self.set_status(
            session_id,
            "running",
            clear_challenge=True,
            clear_human_input=True,
        )

    def submit_captcha(self, session_id: str, code: str) -> SessionRecord:
        rec = self.get(session_id)
        if rec is None:
            raise KeyError("session not found")
        if rec.status != "waiting_captcha":
            raise ValueError(f"status is {rec.status}, expected waiting_captcha")
        now = time.time()
        payload = {"code": code, "submitted_at": now}
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET human_input_json=?, updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), now, session_id),
            )
            conn.commit()
        updated = self.get(session_id)
        assert updated is not None
        return updated

    def pop_human_input(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT human_input_json FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None or not row["human_input_json"]:
                conn.commit()
                return None
            data = json.loads(row["human_input_json"])
            conn.execute(
                "UPDATE sessions SET human_input_json=NULL, updated_at=? WHERE id=?",
                (time.time(), session_id),
            )
            conn.commit()
            return data

    def request_cancel(self, session_id: str) -> SessionRecord | None:
        rec = self.get(session_id)
        if rec is None:
            return None
        now = time.time()
        with self._connect() as conn:
            if rec.status == "queued":
                conn.execute(
                    """
                    UPDATE sessions
                    SET status='cancelled', cancel_requested=1, password=NULL,
                        error_json=?, finished_at=?, updated_at=?
                    WHERE id=? AND status='queued'
                    """,
                    (
                        json.dumps(
                            {"code": "cancelled", "message": "已取消"},
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                        session_id,
                    ),
                )
            elif rec.status not in TERMINAL:
                # 标记取消；worker 轮询到后结束。status 保持原值以便 UI 显示「取消中」
                conn.execute(
                    """
                    UPDATE sessions SET cancel_requested=1, updated_at=?
                    WHERE id=? AND status NOT IN ('succeeded','failed','cancelled')
                    """,
                    (now, session_id),
                )
            conn.commit()
        return self.get(session_id)

    def finish_cancel_requested(self, *, stale_running_seconds: float = 45.0) -> list[SessionRecord]:
        """收尾取消：queued 立即 cancelled；running/waiting 超时未退出也强制收尾。"""
        now = time.time()
        finished: list[SessionRecord] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM sessions
                WHERE cancel_requested = 1
                  AND (
                    status = 'queued'
                    OR (
                      status IN ('running','waiting_captcha','waiting_security')
                      AND claimed_at IS NOT NULL
                      AND claimed_at < ?
                    )
                  )
                """,
                (now - max(5.0, stale_running_seconds),),
            ).fetchall()
            ids = [str(r["id"]) for r in rows]
            for sid in ids:
                conn.execute(
                    """
                    UPDATE sessions
                    SET status='cancelled', password=NULL, error_json=?,
                        challenge_json=NULL, human_input_json=NULL,
                        finished_at=?, updated_at=?, claimed_at=NULL
                    WHERE id=? AND cancel_requested = 1
                      AND status NOT IN ('succeeded','failed','cancelled')
                    """,
                    (
                        json.dumps(
                            {"code": "cancelled", "message": "已取消"},
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                        sid,
                    ),
                )
            conn.commit()
        for sid in ids:
            rec = self.get(sid)
            if rec and rec.status == "cancelled":
                finished.append(rec)
        return finished

    def finish(
        self,
        session_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> SessionRecord | None:
        """写入终态；已是终态则不覆盖。成功/失败在已请求取消时也不写入。"""
        if status not in TERMINAL:
            raise ValueError(f"finish status must be terminal, got {status!r}")
        now = time.time()
        with self._connect() as conn:
            # 取消可以盖住进行中；succeeded/failed 不能盖住已取消或取消中
            if status == "cancelled":
                where = "id=? AND status NOT IN ('succeeded','failed','cancelled')"
                params: list[Any] = [
                    status,
                    json.dumps(result, ensure_ascii=False) if result else None,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    now,
                    now,
                    session_id,
                ]
            else:
                where = (
                    "id=? AND status NOT IN ('succeeded','failed','cancelled') "
                    "AND cancel_requested = 0"
                )
                params = [
                    status,
                    json.dumps(result, ensure_ascii=False) if result else None,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    now,
                    now,
                    session_id,
                ]
            cur = conn.execute(
                f"""
                UPDATE sessions
                SET status=?, result_json=?, error_json=?, password=NULL,
                    challenge_json=NULL, human_input_json=NULL,
                    finished_at=?, updated_at=?, claimed_at=NULL
                WHERE {where}
                """,
                params,
            )
            conn.commit()
            if cur.rowcount == 0:
                # 幂等：目标终态已是该 status 时返回现记录，避免误报失败
                rec = self.get(session_id)
                if rec and rec.status == status:
                    return rec
                return None
        return self.get(session_id)

    def release_for_retry(self, session_id: str, delay_seconds: float) -> bool:
        """释放回队列重试。若已请求取消则直接 cancelled，返回 False。"""
        rec = self.get(session_id)
        if rec is None:
            return False
        if rec.cancel_requested:
            self.finish(
                session_id,
                status="cancelled",
                error={"code": "cancelled", "message": "已取消"},
            )
            return False
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE sessions
                SET status='queued', claimed_at=NULL, next_run_at=?,
                    challenge_json=NULL, human_input_json=NULL, updated_at=?
                WHERE id=? AND status NOT IN ('succeeded','failed','cancelled')
                  AND cancel_requested = 0
                """,
                (now + max(0.0, delay_seconds), now, session_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def is_cancel_requested(self, session_id: str) -> bool:
        rec = self.get(session_id)
        return bool(rec and rec.cancel_requested)

    def list_exhausted(self, max_attempts: int) -> list[SessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE attempts >= ? AND status IN ('queued','running','waiting_captcha','waiting_security')
                """,
                (max_attempts,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def purge_expired(self, ttl_days: int) -> int:
        cutoff = time.time() - ttl_days * 86400
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount
