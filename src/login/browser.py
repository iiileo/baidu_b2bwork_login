from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from src.config import settings
from src.logbus import log_bus
from src.login.exceptions import BrowserUnavailableError


def _pid_of_browser(browser: Browser | None) -> int | None:
    if browser is None:
        return None
    try:
        impl = getattr(browser, "_impl_obj", None)
        proc = getattr(impl, "_process", None) if impl is not None else None
        if proc is not None and getattr(proc, "pid", None):
            return int(proc.pid)
    except Exception:  # noqa: BLE001
        pass
    return None


def _kill_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:  # noqa: BLE001
        pass


def _kill_chromium_descendants(_root_pid: int | None = None) -> None:
    """容器内按 cmdline 强杀残留 chromium（CDP 假死时 close 无效）。"""
    try:
        proc_dirs = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:  # noqa: BLE001
        return
    me = os.getpid()
    targets: list[int] = []
    for spid in proc_dirs:
        try:
            pid = int(spid)
            if pid == me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", errors="ignore").lower()
            if "chrom" in cmd or "headless_shell" in cmd:
                targets.append(pid)
        except Exception:  # noqa: BLE001
            continue
    for pid in targets:
        _kill_pid(pid)


class BrowserSession:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._lock = threading.Lock()
        self._force_closed = False
        self._browser_pid: int | None = None

    def start(self) -> Page:
        last_err: Exception | None = None
        headless = settings.playwright_headless
        if settings.enable_vnc:
            headless = False
            os.environ.setdefault("DISPLAY", settings.display)

        for i in range(1, settings.playwright_create_retry + 1):
            try:
                log_bus.emit(f"浏览器启动开始 attempt={i} headless={headless}")
                self.close()
                # 清掉残留 chrome，避免假死后无法再 launch
                _kill_chromium_descendants(os.getpid())
                self._force_closed = False
                log_bus.emit("浏览器启动 · sync_playwright().start()")
                self._pw = sync_playwright().start()
                launcher = getattr(self._pw, settings.playwright_browser)
                log_bus.emit(f"浏览器启动 · launch({settings.playwright_browser})")
                self.browser = launcher.launch(
                    headless=headless,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--no-first-run",
                        "--disable-hang-monitor",
                        "--disable-renderer-backgrounding",
                        "--disable-features=TranslateUI",
                        "--renderer-process-limit=2",
                    ],
                )
                self._browser_pid = _pid_of_browser(self.browser)
                log_bus.emit(f"浏览器启动 · new_context pid={self._browser_pid}")
                self.context = self.browser.new_context(
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 720},
                )
                self.context.set_default_timeout(10_000)
                self.context.set_default_navigation_timeout(8_000)
                log_bus.emit("浏览器启动 · new_page")
                self.page = self.context.new_page()
                _ = self.page.url
                log_bus.emit(
                    f"浏览器会话已创建（第 {i} 次） "
                    f"browser={settings.playwright_browser} headless={headless} "
                    f"pid={self._browser_pid}"
                )
                return self.page
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log_bus.emit(f"创建浏览器会话失败（第 {i} 次）: {exc}")
                self._force_closed = True
                self.force_close()
                self._force_closed = False
                if i < settings.playwright_create_retry:
                    time.sleep(settings.login_retry_delay)
        raise BrowserUnavailableError(f"无法创建浏览器会话: {last_err}")

    def new_page(self) -> Page:
        """复用已启动的 browser，只换 context/page。"""
        if self.browser is None or self._force_closed:
            log_bus.emit(
                f"浏览器复用跳过 → 完整重建 force_closed={self._force_closed} "
                f"browser={self.browser is not None}"
            )
            return self.start()
        try:
            log_bus.emit("浏览器复用 · 关闭旧 page/context")
            if self.page is not None:
                try:
                    self.page.close()
                except Exception:  # noqa: BLE001
                    pass
                self.page = None
            if self.context is not None:
                try:
                    self.context.close()
                except Exception:  # noqa: BLE001
                    pass
                self.context = None
            log_bus.emit("浏览器复用 · new_context + new_page")
            self.context = self.browser.new_context(
                locale="zh-CN",
                viewport={"width": 1280, "height": 720},
            )
            self.context.set_default_timeout(10_000)
            self.context.set_default_navigation_timeout(8_000)
            self.page = self.context.new_page()
            log_bus.emit("浏览器复用成功")
            return self.page
        except Exception as exc:  # noqa: BLE001
            log_bus.emit(f"复用浏览器失败，重建: {exc}")
            return self.start()

    def force_close(self) -> None:
        """打断卡住的 Playwright：必须先 SIGKILL，绝不能先调 close（CDP 假死时 close 会二次卡死）。"""
        self._force_closed = True
        pid = self._browser_pid
        self._browser_pid = None
        with self._lock:
            self.page = None
            self.context = None
            self.browser = None
            pw = self._pw
            self._pw = None
        log_bus.emit(f"强制杀浏览器进程 pid={pid}")
        _kill_pid(pid)
        _kill_chromium_descendants(os.getpid())
        # 不再调用 page/browser.close()：在假死连接上会永久阻塞看门狗线程
        if pw is not None:
            # stop 也可能卡住，放到短超时线程里
            def _stop() -> None:
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass

            t = threading.Thread(target=_stop, name="pw-stop", daemon=True)
            t.start()
            t.join(timeout=1.5)

    def close(self) -> None:
        if self._force_closed:
            # 已被强杀，只做清理标记
            self.page = None
            self.context = None
            self.browser = None
            self._pw = None
            self._browser_pid = None
            return
        pid = self._browser_pid
        with self._lock:
            for obj, attr in (
                (self.page, "page"),
                (self.context, "context"),
                (self.browser, "browser"),
            ):
                if obj is None:
                    continue
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
            pw = self._pw
            self._pw = None
            self._browser_pid = None
        if pw is not None:
            def _stop() -> None:
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass

            t = threading.Thread(target=_stop, name="pw-stop", daemon=True)
            t.start()
            t.join(timeout=2.0)
        _kill_pid(pid)


def cookies_from_context(context: BrowserContext) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for c in context.cookies():
        item: dict[str, Any] = {
            "name": c.get("name"),
            "value": c.get("value"),
            "path": c.get("path", "/"),
            "domain": c.get("domain"),
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
        }
        expires = c.get("expires")
        if expires is not None and expires > 0:
            item["expiry"] = int(expires)
        result.append(item)
    return result
