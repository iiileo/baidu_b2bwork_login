from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from fastapi import HTTPException

from .sessions import SessionRecord, SessionStore as BaseSessionStore


ACTIVE_STATUSES = ("queued", "running", "waiting_captcha", "waiting_security")


class ActiveSessionExistsError(HTTPException):
    def __init__(self, session_id: str, status: str) -> None:
        super().__init__(
            status_code=409,
            detail={
                "code": "active_session_exists",
                "message": "该用户名已有未完成会话",
                "session_id": session_id,
                "status": status,
            },
        )


class IdempotencyKeyConflictError(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            detail={
                "code": "idempotency_key_reused",
                "message": "相同的 Idempotency-Key 已用于不同的请求",
            },
        )


class SessionStore(BaseSessionStore):
    """SessionStore with atomic username exclusivity and idempotent creates."""

    def _init_db(self) -> None:
        super()._init_db()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")
            }
            if "idempotency_key" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN idempotency_key TEXT")
            if "request_hash" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN request_hash TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_idempotency "
                "ON sessions(username, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )
            conn.commit()

    @staticmethod
    def _request_hash(
        *,
        username: str,
        password: str,
        webhook_url: str | None,
        priority: int,
        client_ref: str | None,
        silent: bool,
    ) -> str:
        payload = {
            "username": username,
            "password": password,
            "webhook_url": webhook_url,
            "priority": max(1, priority),
            "client_ref": client_ref,
            "silent": silent,
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _queue_position(conn, record: SessionRecord) -> int:
        if record.status != "queued":
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM sessions
            WHERE status = 'queued'
              AND (
                priority > ?
                OR (priority = ? AND (created_at < ? OR (created_at = ? AND id <= ?)))
              )
            """,
            (
                record.priority,
                record.priority,
                record.created_at,
                record.created_at,
                record.id,
            ),
        ).fetchone()
        return int(row["c"])

    def create(
        self,
        *,
        username: str,
        password: str,
        webhook_url: str | None,
        priority: int,
        client_ref: str | None,
        silent: bool = False,
        idempotency_key: str | None = None,
    ) -> tuple[SessionRecord, int, bool]:
        """Create atomically, reject concurrent active usernames, replay keyed retries."""
        key = idempotency_key.strip() if idempotency_key is not None else None
        if key == "":
            raise HTTPException(status_code=422, detail="Idempotency-Key 不能为空")
        request_hash = self._request_hash(
            username=username,
            password=password,
            webhook_url=webhook_url,
            priority=priority,
            client_ref=client_ref,
            silent=silent,
        )
        now = time.time()

        with self._connect() as conn:
            # This write lock serializes the check-and-insert across API workers/processes.
            conn.execute("BEGIN IMMEDIATE")

            if key is not None:
                prior = conn.execute(
                    "SELECT * FROM sessions WHERE username = ? AND idempotency_key = ?",
                    (username, key),
                ).fetchone()
                if prior is not None:
                    if prior["request_hash"] != request_hash:
                        raise IdempotencyKeyConflictError()
                    record = self._row_to_record(prior)
                    position = self._queue_position(conn, record)
                    conn.commit()
                    return record, position, True

            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            active = conn.execute(
                f"SELECT id, status FROM sessions WHERE username = ? "
                f"AND status IN ({placeholders}) ORDER BY created_at ASC LIMIT 1",
                (username, *ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                raise ActiveSessionExistsError(str(active["id"]), str(active["status"]))

            session_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO sessions(
                    id, username, password, priority, client_ref, webhook_url,
                    silent, status, cancel_requested, attempts, claimed_at, next_run_at,
                    created_at, updated_at, idempotency_key, request_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    username,
                    password,
                    max(1, priority),
                    client_ref,
                    webhook_url,
                    1 if silent else 0,
                    now,
                    now,
                    now,
                    key,
                    request_hash if key is not None else None,
                ),
            )
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            assert row is not None
            record = self._row_to_record(row)
            position = self._queue_position(conn, record)
            conn.commit()
            return record, position, False
