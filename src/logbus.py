from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any


class LogBus:
    """广播实时日志给 WebSocket 订阅者；连接后才推送，无历史回放。"""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def emit(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        logging.getLogger("b2bwork").info(message)
        loop = self._loop
        if loop is None or not self._subscribers:
            return
        for q in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(self._put_nowait, q, line)
            except RuntimeError:
                pass

    @staticmethod
    def _put_nowait(q: asyncio.Queue[str], line: str) -> None:
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass


log_bus = LogBus()


class BusHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # 避免与 log_bus.emit 互相递归：只转发非 b2bwork 或手工调用时不走这里
        if record.name == "b2bwork":
            return
        try:
            log_bus.emit(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
