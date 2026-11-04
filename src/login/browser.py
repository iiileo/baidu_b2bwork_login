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


_LAUNCH_ARGS = [
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
]


# 与 entrypoint Xvfb 默认分辨率一致；VNC 下浏览器窗口默认铺满
_VNC_SCREEN = (1280, 800)


def _launch_args() -> list[str]:
    args = list(_LAUNCH_ARGS)
    locale = (settings.browser_locale or "zh-CN").strip() or "zh-CN"
    args.append(f"--lang={locale}")
    if settings.enable_vnc:
        w, h = _VNC_SCREEN
        args.append(f"--window-size={w},{h}")
        args.append("--window-position=0,0")
        args.append("--force-device-scale-factor=1")
    return args


def _context_kwargs() -> dict[str, Any]:
    locale = (settings.browser_locale or "zh-CN").strip() or "zh-CN"
    w, h = _VNC_SCREEN if settings.enable_vnc else (1280, 800)
    return {
        "locale": locale,
        "timezone_id": "Asia/Shanghai",
        "extra_http_headers": {
            "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9,en;q=0.8",
        },
        "device_scale_factor": 1.0,
        "viewport": {"width": w, "height": h},
    }


class BrowserSession:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._lock = threading.Lock()
        self._force_closed = False
        self._browser_pid: int | None = None
        self._stop_thread: threading.Thread | None = None

    def _join_pending_stop(self, timeout: float = 5.0) -> None:
        t = self._stop_thread
        if t is not None and t.is_alive():
            log_bus.emit(f"等待上次 Playwright.stop 结束 timeout={timeout}s")
            t.join(timeout=timeout)
        self._stop_thread = None

    def _stop_playwright_async(self, pw: Playwright, *, join_timeout: float = 2.0) -> None:
        """在后台线程 stop；同一进程内绝不能重叠两次 sync_playwright().start()。"""

        def _stop() -> None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

        t = threading.Thread(target=_stop, name="pw-stop", daemon=True)
        self._stop_thread = t
        t.start()
        t.join(timeout=join_timeout)

    def _discard_browser_refs(self) -> None:
        with self._lock:
            self.page = None
            self.context = None
            self.browser = None
            self._browser_pid = None

    def _close_browser_only(self) -> None:
        """只关 browser/context/page，保留 Playwright 驱动（避免二次 start 报假 asyncio 错）。"""
        pid = self._browser_pid
        page, context, browser = self.page, self.context, self.browser
        self._discard_browser_refs()

        def _close_obj(obj: Any, label: str) -> None:
            if obj is None:
                return
            try:
                obj.close()
            except Exception as exc:  # noqa: BLE001
                log_bus.emit(f"关闭{label}忽略: {exc}")

        # close 可能阻塞，放线程里限时
        def _run() -> None:
            _close_obj(page, "page")
            _close_obj(context, "context")
            _close_obj(browser, "browser")

        t = threading.Thread(target=_run, name="pw-browser-close", daemon=True)
        t.start()
        t.join(timeout=2.0)
        if t.is_alive():
            log_bus.emit("关闭 browser 超时，改 SIGKILL")
            _kill_pid(pid)
            _kill_chromium_descendants(os.getpid())
        else:
            _kill_pid(pid)

    def _ensure_playwright(self) -> Playwright:
        self._join_pending_stop()
        if self._pw is not None:
            return self._pw
        log_bus.emit("浏览器启动 · sync_playwright().start()")
        self._pw = sync_playwright().start()
        return self._pw

    def _drop_playwright(self) -> None:
        """彻底丢掉驱动（仅在驱动已坏、或进程退出时）。必须先停干净再允许下次 start。"""
        self._join_pending_stop()
        pw = self._pw
        self._pw = None
        if pw is not None:
            log_bus.emit("浏览器启动 · 停止旧 Playwright 驱动")
            self._stop_playwright_async(pw, join_timeout=5.0)
            self._join_pending_stop(timeout=5.0)

    def start(self) -> Page:
        last_err: Exception | None = None
        headless = settings.playwright_headless
        if settings.enable_vnc:
            headless = False
            os.environ.setdefault("DISPLAY", settings.display)

        for i in range(1, settings.playwright_create_retry + 1):
            try:
                log_bus.emit(f"浏览器启动开始 attempt={i} headless={headless}")
                self._close_browser_only()
                _kill_chromium_descendants(os.getpid())
                self._force_closed = False

                # 同一进程只应 start 一次；复用驱动。若上次被判定损坏才 drop 后重建。
                if i > 1:
                    self._drop_playwright()
                pw = self._ensure_playwright()

                launcher = getattr(pw, settings.playwright_browser)
                log_bus.emit(
                    f"浏览器启动 · launch({settings.playwright_browser}) "
                    f"locale={settings.browser_locale} "
                    f"screen={_VNC_SCREEN[0]}x{_VNC_SCREEN[1]}"
                )
                self.browser = launcher.launch(
                    headless=headless,
                    args=_launch_args(),
                    env={
                        **os.environ,
                        "LANG": "zh_CN.UTF-8",
                        "LANGUAGE": "zh_CN:zh",
                        "LC_ALL": "zh_CN.UTF-8",
                    },
                )
                self._browser_pid = _pid_of_browser(self.browser)
                log_bus.emit(f"浏览器启动 · new_context pid={self._browser_pid}")
                self.context = self.browser.new_context(**_context_kwargs())
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
                msg = str(exc)
                log_bus.emit(f"创建浏览器会话失败（第 {i} 次）: {exc}")
                # 这条文案常被二次 start 触发，并不真是跑在 asyncio 里
                if "asyncio loop" in msg.lower() or "async api" in msg.lower():
                    log_bus.emit(
                        "检测到 Playwright 驱动未停干净（误报 asyncio）；"
                        "将停驱动后重试"
                    )
                    self._force_closed = True
                    self.force_close(keep_driver=False)
                else:
                    self._force_closed = True
                    self.force_close(keep_driver=True)
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
            self.context = self.browser.new_context(**_context_kwargs())
            self.context.set_default_timeout(10_000)
            self.context.set_default_navigation_timeout(8_000)
            self.page = self.context.new_page()
            log_bus.emit("浏览器复用成功")
            return self.page
        except Exception as exc:  # noqa: BLE001
            log_bus.emit(f"复用浏览器失败，重建: {exc}")
            return self.start()

    def force_close(self, *, keep_driver: bool = True) -> None:
        """打断卡住的 Playwright：先 SIGKILL 浏览器。

        keep_driver=True（默认）：保留 sync Playwright 驱动，避免二次 start 触发
        「Sync API inside the asyncio loop」误报。
        keep_driver=False：驱动已坏时才停干净，供重试重建。
        """
        self._force_closed = True
        pid = self._browser_pid
        self._browser_pid = None
        with self._lock:
            self.page = None
            self.context = None
            self.browser = None
            pw = self._pw
            if not keep_driver:
                self._pw = None
        log_bus.emit(
            f"强制杀浏览器进程 pid={pid} keep_driver={keep_driver}"
        )
        _kill_pid(pid)
        _kill_chromium_descendants(os.getpid())
        if not keep_driver and pw is not None:
            self._stop_playwright_async(pw, join_timeout=3.0)

    def close(self) -> None:
        """完整关闭（worker 退出 / 重置会话时）：关 browser 并 stop 驱动。"""
        self._close_browser_only()
        self._drop_playwright()
        self._force_closed = False


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
