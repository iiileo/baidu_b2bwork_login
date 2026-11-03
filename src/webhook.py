from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx

from src.config import settings
from src.logbus import log_bus


def _sign(timestamp: str, body: str) -> str:
    msg = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(
        settings.webhook_secret.encode("utf-8"),
        msg,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def send_webhook(
    webhook_url: str | None,
    event_type: str,
    payload: dict[str, Any],
    *,
    max_tries: int = 3,
    timeout: float = 15.0,
) -> None:
    if not webhook_url:
        log_bus.emit(f"webhook 跳过（无 URL） type={event_type}")
        return

    body_obj = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": event_type,
        **payload,
    }
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    delays = [0, 2, 4]
    last_error: Exception | None = None

    for i in range(max_tries):
        if delays[i]:
            time.sleep(delays[i])
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Id": body_obj["id"],
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature": _sign(ts, body),
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(webhook_url, content=body.encode("utf-8"), headers=headers)
            if 200 <= resp.status_code < 300:
                log_bus.emit(f"webhook 成功 type={event_type} status={resp.status_code}")
                return
            last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            log_bus.emit(f"webhook 非 2xx: {last_error}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log_bus.emit(f"webhook 失败: {exc}")

    log_bus.emit(f"webhook 最终失败 type={event_type}: {last_error}")
