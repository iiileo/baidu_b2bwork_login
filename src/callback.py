from __future__ import annotations

import time
from typing import Any

import httpx

from src.logbus import log_bus


def send_callback(
    callback_url: str | None,
    api_key: str,
    payload: dict[str, Any],
    *,
    max_tries: int = 3,
    timeout: float = 15.0,
) -> None:
    """向 callback_url POST JSON；失败重试最多 3 次，间隔 2s、4s。不改变登录结论。"""
    if not callback_url:
        log_bus.emit("步骤7 无 callback_url，跳过回调")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    delays = [0, 2, 4]
    last_error: Exception | None = None
    for i in range(max_tries):
        if delays[i]:
            time.sleep(delays[i])
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(callback_url, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                log_bus.emit(f"步骤7 回调发送完成 status={resp.status_code}")
                return
            last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            log_bus.emit(f"步骤7 回调非 2xx: {last_error}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log_bus.emit(f"步骤7 回调失败: {exc}")

    log_bus.emit(f"步骤7 回调最终失败: {last_error}")
