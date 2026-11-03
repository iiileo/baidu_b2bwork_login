from __future__ import annotations

import os
import time
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from src.config import settings
from src.logbus import log_bus
from src.login.exceptions import BrowserUnavailableError


class BrowserSession:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self) -> Page:
        last_err: Exception | None = None
        headless = settings.playwright_headless
        if settings.enable_vnc:
            headless = False
            os.environ.setdefault("DISPLAY", settings.display)

        for i in range(1, settings.playwright_create_retry + 1):
            try:
                self.close()
                self._pw = sync_playwright().start()
                launcher = getattr(self._pw, settings.playwright_browser)
                self.browser = launcher.launch(
                    headless=headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                self.context = self.browser.new_context(locale="zh-CN")
                self.context.set_default_timeout(settings.playwright_default_timeout_ms)
                self.context.set_default_navigation_timeout(
                    settings.playwright_navigation_timeout_ms
                )
                self.page = self.context.new_page()
                _ = self.page.url
                log_bus.emit(
                    f"浏览器会话已创建（第 {i} 次） "
                    f"browser={settings.playwright_browser} headless={headless}"
                )
                return self.page
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log_bus.emit(f"创建浏览器会话失败（第 {i} 次）: {exc}")
                self.close()
                if i < settings.playwright_create_retry:
                    time.sleep(settings.login_retry_delay)
        raise BrowserUnavailableError(f"无法创建浏览器会话: {last_err}")

    def close(self) -> None:
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
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None


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
