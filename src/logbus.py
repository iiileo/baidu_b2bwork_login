from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

from src.config import data_path


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class LogBus:
    """广播实时日志给 WebSocket 订阅者。

    - API 进程（已 bind_loop）：直接推 WS
    - worker 进程（无 event loop）：写入共享 live.log，由 API 尾随转发
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._file_lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def relay_path(self) -> Path:
        return data_path("live.log")

    def emit(self, message: str) -> None:
        line = f"[{_ts()}] {message}"
        logging.getLogger("b2bwork").info(message)
        if self._loop is None:
            # worker：无 WS，写入共享文件供 API 转发
            self._append_relay(line)
        else:
            self.broadcast(line)

    def broadcast(self, line: str) -> None:
        """仅推给 WebSocket 订阅者（不写 stdout / relay）。"""
        loop = self._loop
        if loop is None or not self._subscribers:
            return
        for q in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(self._put_nowait, q, line)
            except RuntimeError:
                pass

    def _append_relay(self, line: str) -> None:
        path = self.relay_path()
        try:
            with self._file_lock:
                if path.exists() and path.stat().st_size > 2_000_000:
                    path.write_text("", encoding="utf-8")
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
                    fp.flush()
        except Exception:  # noqa: BLE001
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
        # 避免与 log_bus.emit → b2bwork.info 互相递归
        if record.name == "b2bwork":
            return
        # 访问日志太吵，不进实时面板
        if record.name == "uvicorn.access":
            return
        try:
            msg = self.format(record)
            log_bus.emit(msg)
        except Exception:
            self.handleError(record)


async def relay_worker_logs(stop: asyncio.Event) -> None:
    """尾随 data/live.log，把 worker 写入的新行推到 WS。"""
    path = log_bus.relay_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    offset = path.stat().st_size  # 不回放历史
    while not stop.is_set():
        try:
            size = path.stat().st_size
            if size < offset:
                offset = 0
            if size > offset:
                with path.open("r", encoding="utf-8", errors="replace") as fp:
                    fp.seek(offset)
                    chunk = fp.read()
                    offset = fp.tell()
                for raw in chunk.splitlines():
                    line = raw.strip("\r")
                    if line:
                        log_bus.broadcast(line)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass


def setup_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    if not any(isinstance(h, BusHandler) for h in root.handlers):
        handler = BusHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
        root.addHandler(handler)
