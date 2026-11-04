from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from src.config import data_path, settings
from src.logbus import log_bus
from src.login.browser import BrowserSession, cookies_from_context
from src.login.exceptions import BrowserUnavailableError, LoginFinished
from src.login.qrcode_util import decode_qr_from_bytes
from src.store.sessions import SessionStore
from src.webhook import send_webhook

BLANK_URLS = {
    "about:blank",
    "about:newtab",
    "about:sessionrestore",
    "about:home",
    "data:,",
    "",
}

SESSION_ERR_HINTS = (
    "target closed",
    "browser has been closed",
    "browser has closed",
    "connection refused",
    "session closed",
    "has been closed",
    "timed out receiving message",
    "no such window",
    "invalid session",
)

# 页面跳转中 evaluate 失败是正常竞态，不是浏览器挂死
NAV_RACE_HINTS = (
    "execution context was destroyed",
    "most likely because of a navigation",
    "frame was detached",
    "navigating frame was detached",
)

CancelCheck = Callable[[], bool]
HumanInputPop = Callable[[], dict[str, Any] | None]
SetWaiting = Callable[[str, dict[str, Any]], None]
SetRunning = Callable[[], None]


class _StepProgress:
    """记录当前子步骤，供心跳日志输出。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.step = "init"
        self.detail = ""
        self.started_at = time.time()

    def set(self, step: str, detail: str = "") -> None:
        with self._lock:
            self.step = step
            self.detail = detail
            self.started_at = time.time()
        extra = f" · {detail}" if detail else ""
        log_bus.emit(f"进度 → {step}{extra}")

    def snapshot(self) -> tuple[str, str, float]:
        with self._lock:
            return self.step, self.detail, time.time() - self.started_at


class _CancelWatchdog:
    """后台：心跳日志 + 取消/阶段超时强杀浏览器。"""

    def __init__(
        self,
        cancel_check: CancelCheck,
        session: BrowserSession,
        *,
        progress: _StepProgress,
        phase_deadline_holder: list[float] | None = None,
        interval: float = 0.5,
        heartbeat_every: float = 2.0,
    ) -> None:
        self._cancel_check = cancel_check
        self._session = session
        self._progress = progress
        self._phase_deadline_holder = phase_deadline_holder
        self._interval = interval
        self._heartbeat_every = heartbeat_every
        self._stop = threading.Event()
        self.forced = False
        self.timed_out = False
        self._last_hb = 0.0
        self._thread = threading.Thread(target=self._run, name="cancel-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def set_phase_deadline(self, ts: float) -> None:
        if self._phase_deadline_holder is not None:
            self._phase_deadline_holder[0] = ts

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                now = time.time()
                if now - self._last_hb >= self._heartbeat_every:
                    self._last_hb = now
                    step, detail, elapsed = self._progress.snapshot()
                    remain = None
                    if self._phase_deadline_holder:
                        remain = max(0.0, self._phase_deadline_holder[0] - now)
                    extra = f" · {detail}" if detail else ""
                    rem = f" phase剩余={remain:.0f}s" if remain is not None else ""
                    log_bus.emit(
                        f"心跳 当前={step}{extra} 已停留={elapsed:.1f}s{rem}"
                    )

                if self._cancel_check():
                    self.forced = True
                    log_bus.emit("取消看门狗：SIGKILL 浏览器以打断阻塞操作")
                    self._session.force_close()
                    return
                holder = self._phase_deadline_holder
                if holder and now > holder[0]:
                    self.timed_out = True
                    self.forced = True
                    step, detail, elapsed = self._progress.snapshot()
                    log_bus.emit(
                        f"超时看门狗：阶段超时 step={step} detail={detail!r} "
                        f"停留={elapsed:.1f}s，SIGKILL 浏览器"
                    )
                    self._session.force_close()
                    return
            except Exception as exc:  # noqa: BLE001
                log_bus.emit(f"看门狗异常: {exc}")
                return


def login(
    *,
    store: SessionStore,
    session_id: str,
    username: str,
    password: str,
    webhook_url: str | None,
    client_ref: str | None,
    silent: bool = False,
    browser_session: BrowserSession | None = None,
) -> None:
    """执行登录；通过 LoginFinished 抛出终态，或抛 BrowserUnavailableError 供重试。"""
    deadline = time.time() + settings.login_total_timeout
    owns_session = browser_session is None
    session = browser_session or BrowserSession()
    watchdog: _CancelWatchdog | None = None

    def cancel_check() -> bool:
        return store.is_cancel_requested(session_id)

    def pop_input() -> dict[str, Any] | None:
        return store.pop_human_input(session_id)

    def set_running() -> None:
        store.mark_running(session_id)

    def set_waiting(status: str, challenge: dict[str, Any]) -> None:
        store.set_status(session_id, status, challenge=challenge, clear_human_input=True)
        event = (
            "session.waiting_captcha"
            if status == "waiting_captcha"
            else "session.waiting_security"
        )
        send_webhook(
            webhook_url,
            event,
            {
                "session_id": session_id,
                "status": status,
                "username": username,
                "client_ref": client_ref,
                "challenge": {
                    "type": challenge.get("type"),
                    "qr_content": challenge.get("qr_content"),
                    "expires_at": challenge.get("expires_at"),
                    # 仅图片验证码提供图；安全扫码以 qr_content 为准
                    "image_available": challenge.get("type") == "captcha_image"
                    and bool(challenge.get("image_path")),
                },
            },
        )

    # 打开/填表/点击阶段硬限（含启动浏览器；假死靠看门狗 SIGKILL，超时走任务重试）
    phase_deadline_holder = [time.time() + max(20, settings.login_phase_timeout)]
    progress = _StepProgress()
    try:
        _raise_if_cancelled(cancel_check)
        # 看门狗必须先于浏览器启动：启动 chromium / goto 都可能假死
        watchdog = _CancelWatchdog(
            cancel_check,
            session,
            progress=progress,
            phase_deadline_holder=phase_deadline_holder,
        )
        watchdog.start()
        progress.set("0.准备浏览器")
        page = session.new_page() if browser_session is not None else session.start()
        _raise_if_cancelled(cancel_check)
        _open_and_fill(
            page, username, password, cancel_check=cancel_check, progress=progress
        )
        _click_login(page, cancel_check=cancel_check, progress=progress)
        # 状态轮询：单次 Playwright 调用卡死超过该窗口则强杀；每轮成功会续期
        phase_deadline_holder[0] = time.time() + 60
        progress.set("4.状态轮询")
        _poll_status(
            page=page,
            context=session.context,
            session_id=session_id,
            username=username,
            silent=silent,
            deadline_holder=[deadline],
            phase_deadline_holder=phase_deadline_holder,
            cancel_check=cancel_check,
            pop_input=pop_input,
            set_waiting=set_waiting,
            set_running=set_running,
            progress=progress,
        )
    except LoginFinished:
        raise
    except BrowserUnavailableError:
        # 阶段看门狗超时：可恢复（冷启动慢等），交给 worker _fail_or_retry
        if watchdog and watchdog.timed_out:
            raise BrowserUnavailableError(
                "登录操作超时（浏览器无响应）"
            ) from None
        if watchdog and watchdog.forced:
            raise LoginFinished(
                cancelled=True, error_code="cancelled", error_message="已取消"
            ) from None
        raise
    except PlaywrightError as exc:
        if watchdog and watchdog.timed_out:
            raise BrowserUnavailableError(
                "登录操作超时（浏览器无响应）"
            ) from exc
        if watchdog and watchdog.forced:
            raise LoginFinished(
                cancelled=True, error_code="cancelled", error_message="已取消"
            ) from exc
        if cancel_check():
            raise LoginFinished(
                cancelled=True, error_code="cancelled", error_message="已取消"
            ) from exc
        if _is_session_error(exc):
            raise BrowserUnavailableError(str(exc)) from exc
        raise
    finally:
        # 先清理页面，再停看门狗：close 若卡死仍可由取消/超时 SIGKILL
        try:
            if owns_session:
                session.close()
            elif session._force_closed:  # noqa: SLF001
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
            elif session.page is not None:
                page_to_close = session.page
                session.page = None

                def _close_page() -> None:
                    try:
                        page_to_close.close()
                    except Exception:  # noqa: BLE001
                        pass

                t = threading.Thread(target=_close_page, name="page-close", daemon=True)
                t.start()
                t.join(timeout=2.0)
                if t.is_alive():
                    log_bus.emit("关闭页面超时，强制杀浏览器")
                    try:
                        session.force_close()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if watchdog is not None:
                watchdog.stop()


def _is_session_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if _is_navigation_race(exc):
        return False
    return any(h in msg for h in SESSION_ERR_HINTS)


def _is_navigation_race(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in NAV_RACE_HINTS)


def _is_blank_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u in BLANK_URLS or u.startswith("about:")


def _ensure_alive(page: Page) -> None:
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            url = page.url
            if _is_blank_url(url):
                raise BrowserUnavailableError(f"页面变为空白页: {url!r}")
            _ = page.evaluate("() => document.readyState")
            return
        except BrowserUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_navigation_race(exc):
                time.sleep(0.15)
                continue
            if _is_session_error(exc):
                raise BrowserUnavailableError(str(exc)) from exc
            raise BrowserUnavailableError(f"会话检测失败: {exc}") from exc
    raise BrowserUnavailableError(f"会话检测失败(持续跳转): {last_exc}")


def _raise_if_cancelled(cancel_check: CancelCheck) -> None:
    if cancel_check():
        raise LoginFinished(cancelled=True, error_code="cancelled", error_message="已取消")


def _interruptible_sleep(seconds: float, cancel_check: CancelCheck, *, step: float = 0.25) -> None:
    """可被取消打断的 sleep，避免长 sleep 期间取消按钮无响应。"""
    end = time.time() + max(0.0, seconds)
    while True:
        _raise_if_cancelled(cancel_check)
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


def _open_login_page(
    page: Page, *, cancel_check: CancelCheck, progress: _StepProgress
) -> None:
    progress.set("1.打开登录页", settings.login_url)
    last_err: Exception | None = None
    for attempt in range(1, 4):
        _raise_if_cancelled(cancel_check)
        progress.set("1.goto", f"attempt={attempt} url={settings.login_url}")
        try:
            # commit 比 domcontentloaded 更早返回；timeout 仅作期望值，假死靠看门狗杀进程
            t0 = time.time()
            page.goto(settings.login_url, wait_until="commit", timeout=6_000)
            log_bus.emit(f"步骤1 goto返回 attempt={attempt} 耗时={(time.time()-t0)*1000:.0f}ms url={page.url}")
        except PlaywrightTimeoutError as exc:
            last_err = exc
            log_bus.emit(f"步骤1 goto超时 attempt={attempt} err={exc}")
            try:
                page.evaluate("() => window.stop()")
            except Exception:  # noqa: BLE001
                pass
        except PlaywrightError as exc:
            if _is_session_error(exc):
                raise BrowserUnavailableError(str(exc)) from exc
            last_err = exc
            log_bus.emit(f"步骤1 goto失败 attempt={attempt} err={exc}")

        progress.set("1.等待表单", f"attempt={attempt}")
        end = time.time() + 6
        while time.time() < end:
            _raise_if_cancelled(cancel_check)
            try:
                url = page.url
                if _is_blank_url(url):
                    progress.set("1.等待表单", "blank url")
                    _interruptible_sleep(0.2, cancel_check)
                    continue
                ready = page.evaluate(
                    """() => {
                      const form = document.querySelector('#submit-form');
                      const account = document.querySelector('#uc-common-account');
                      return {
                        ready: document.readyState,
                        hasForm: !!(form || account),
                        bodyLen: (document.body && document.body.innerText)
                          ? document.body.innerText.length : 0,
                      };
                    }"""
                )
                progress.set(
                    "1.等待表单",
                    f"ready={ready.get('ready')} hasForm={ready.get('hasForm')} "
                    f"bodyLen={ready.get('bodyLen')} url={url[:80]}",
                )
                if ready.get("hasForm") or (
                    ready.get("ready") in ("interactive", "complete")
                    and int(ready.get("bodyLen") or 0) >= 40
                ):
                    try:
                        progress.set("1.wait_for_selector", "#uc-common-account")
                        page.wait_for_selector(
                            "#submit-form, #uc-common-account",
                            timeout=4_000,
                            state="attached",
                        )
                    except PlaywrightTimeoutError as exc:
                        last_err = exc
                        log_bus.emit(f"步骤1 等账号框超时: {exc}")
                        break
                    progress.set("1.登录页就绪", url[:120])
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if _is_navigation_race(exc):
                    # 首页→登录页跳转瞬间 evaluate 必失败，继续轮询即可
                    progress.set("1.等待表单", "页面跳转中，重探DOM")
                else:
                    log_bus.emit(f"步骤1 探测DOM异常: {exc}")
                    if _is_session_error(exc):
                        raise BrowserUnavailableError(str(exc)) from exc
            _interruptible_sleep(0.2, cancel_check)
        log_bus.emit(f"步骤1 第 {attempt} 次打开仍未就绪，重试 last_err={last_err}")
        try:
            progress.set("1.重置about:blank")
            page.goto("about:blank", wait_until="commit", timeout=2_000)
        except Exception:  # noqa: BLE001
            pass
    raise BrowserUnavailableError(f"三次打开登录页均为空页或未就绪: {last_err}")


def _human_move(page: Page, cancel_check: CancelCheck) -> None:
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        for _ in range(2):
            _raise_if_cancelled(cancel_check)
            x = random.randint(20, max(21, viewport["width"] - 20))
            y = random.randint(20, max(21, viewport["height"] - 20))
            page.mouse.move(x, y, steps=random.randint(3, 8))
            _interruptible_sleep(random.uniform(0.03, 0.12), cancel_check)
    except LoginFinished:
        raise
    except Exception:  # noqa: BLE001
        pass


def _fill_credentials(
    page: Page,
    username: str,
    password: str,
    *,
    cancel_check: CancelCheck,
    progress: _StepProgress,
) -> None:
    progress.set("2.等待账号输入框")
    page.wait_for_selector("#uc-common-account", timeout=6_000)
    _raise_if_cancelled(cancel_check)
    progress.set("2.模拟鼠标移动")
    _human_move(page, cancel_check)
    _interruptible_sleep(0.15, cancel_check)
    progress.set("2.填写账号", username)
    account = page.locator("#uc-common-account")
    account.click(timeout=3_000)
    account.fill(username, timeout=3_000)
    log_bus.emit("步骤2 账号已填入")
    _raise_if_cancelled(cancel_check)
    progress.set("2.填写密码")
    pwd = page.locator("#ucsl-password-edit")
    pwd.click(timeout=3_000)
    pwd.fill(password, timeout=3_000)
    log_bus.emit("步骤2 密码已填入")
    _interruptible_sleep(0.15, cancel_check)


def _open_and_fill(
    page: Page,
    username: str,
    password: str,
    *,
    cancel_check: CancelCheck,
    progress: _StepProgress,
) -> None:
    last_err: Exception | None = None
    for attempt in range(1, 4):
        _raise_if_cancelled(cancel_check)
        try:
            _open_login_page(page, cancel_check=cancel_check, progress=progress)
            _fill_credentials(
                page, username, password, cancel_check=cancel_check, progress=progress
            )
            progress.set("2.账号密码填写完成")
            return
        except LoginFinished:
            raise
        except BrowserUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_session_error(exc):
                raise BrowserUnavailableError(str(exc)) from exc
            last_err = exc
            log_bus.emit(f"步骤2 填表失败（第 {attempt} 次）: {exc}")
    raise BrowserUnavailableError(f"表单元素不可用: {last_err}")


def _click_login(
    page: Page, *, cancel_check: CancelCheck, progress: _StepProgress
) -> None:
    progress.set("3.点击前存活检查")
    _ensure_alive(page)
    _raise_if_cancelled(cancel_check)
    progress.set("3.点击登录按钮")
    before = page.url or ""
    page.locator("#submit-form").click(timeout=3_000)
    log_bus.emit("步骤3 已点击登录，等待跳转/挑战")
    progress.set("3.等待跳转或挑战")
    # 轮询等待跳转/挑战出现，替代固定 sleep(5)，并可响应取消
    deadline = time.time() + 8
    while time.time() < deadline:
        _raise_if_cancelled(cancel_check)
        try:
            url = page.url or ""
            if url != before and not _is_blank_url(url):
                break
            if _captcha_visible(page) or _slider_visible(page):
                break
            if "aq.baidu.com" in url:
                break
            if _error_node_text(page):
                break
        except Exception:  # noqa: BLE001
            pass
        _interruptible_sleep(0.3, cancel_check)


def _page_looks_502(page: Page) -> bool:
    try:
        title = (page.title() or "").lower()
        if "502" in title:
            return True
        # 只取短文本，避免每次拉全量 HTML
        snippet = page.evaluate(
            """() => {
              const t = document.body ? (document.body.innerText || '') : '';
              return t.slice(0, 800).toLowerCase();
            }"""
        )
    except Exception:  # noqa: BLE001
        return False
    blob = title + "\n" + (snippet or "")
    return any(
        n in blob
        for n in (
            "502 bad gateway",
            "error 502",
            "http error 502",
        )
    )


def _error_node_text(page: Page) -> str | None:
    """只在错误节点可见且有非空文案时返回，避免隐藏节点误判失败。"""
    selectors = (
        "p.error.uc-account-error.visible-show",
        "p.uc-account-error.visible-show",
        ".uc-account-error.visible-show",
    )
    for sel in selectors:
        loc = page.locator(sel)
        try:
            n = loc.count()
            if n == 0:
                continue
            for i in range(min(n, 3)):
                node = loc.nth(i)
                if not node.is_visible():
                    continue
                text = (node.inner_text(timeout=500) or "").strip()
                if text:
                    return text
        except Exception:  # noqa: BLE001
            continue
    return None


def _captcha_visible(page: Page) -> bool:
    """以可见验证码控件为准，避免源码里隐藏文案导致假阳性卡死。"""
    try:
        img = page.locator("#token-img")
        if img.count() > 0 and img.first.is_visible():
            return True
        token = page.locator("#uc-common-token")
        if token.count() > 0 and token.first.is_visible():
            return True
        hint = page.locator("text=请填写验证码")
        if hint.count() > 0 and hint.first.is_visible():
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _slider_visible(page: Page) -> bool:
    try:
        hint = page.locator("text=请完成下方验证后继续操作")
        if hint.count() > 0 and hint.first.is_visible():
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _still_on_login_form(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
        if "aq.baidu.com" in url:
            return False
        if "b2bwork.baidu.com/dashboard" in url or "b2bwork.baidu.com/ai-agent" in url:
            return False
        form = page.locator("#submit-form")
        return form.count() > 0 and form.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _save_challenge_image(session_id: str, raw_png: bytes) -> Path:
    path = data_path("challenges", f"{session_id}.png")
    path.write_bytes(raw_png)
    return path


def _capture_captcha_challenge(page: Page, session_id: str) -> dict[str, Any]:
    img = page.locator("#token-img")
    img.wait_for(state="visible", timeout=5000)
    box = img.bounding_box()
    if not box:
        raise RuntimeError("验证码图片不可见")
    full = page.screenshot(full_page=False)
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(full))
    cropped = image.crop(
        (
            int(box["x"]),
            int(box["y"]),
            int(box["x"] + box["width"]),
            int(box["y"] + box["height"]),
        )
    )
    buf = BytesIO()
    cropped.save(buf, format="PNG")
    path = _save_challenge_image(session_id, buf.getvalue())
    expires = time.time() + settings.captcha_wait_seconds
    return {
        "type": "captcha_image",
        "qr_content": None,
        "image_path": str(path),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
    }


def _qr_content_from_dom(page: Page) -> str | None:
    """优先从页面直接取扫码内容，避免截图识别。"""
    try:
        raw = page.evaluate(
            r"""() => {
              const pick = (v) => (typeof v === 'string' && v.trim().length > 8) ? v.trim() : null;
              for (const el of document.querySelectorAll('[data-qr],[data-qrcode],[data-url],[data-content]')) {
                for (const k of ['data-qr','data-qrcode','data-url','data-content']) {
                  const v = pick(el.getAttribute(k));
                  if (v) return v;
                }
              }
              const html = document.documentElement ? document.documentElement.innerHTML : '';
              const m = html.match(/https?:\/\/[-a-zA-Z0-9@:%._+~#=]{8,}\/[^"'\s<>]{8,}/);
              if (m && /qr|scan|passport|wappass|aq\.baidu/i.test(m[0])) return m[0];
              return null;
            }"""
        )
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def _screenshot_qr_element(page: Page) -> bytes | None:
    """只截二维码元素，避免全页截图 + 全图识别过慢。"""
    selectors = (
        "img[src*='qr']",
        "img[src*='QR']",
        "img.qrcode",
        "img[class*='qr']",
        "canvas.qrcode",
        "canvas[class*='qr']",
        "#qrcode canvas",
        "#qrcode img",
        ".qrcode canvas",
        ".qrcode img",
        "canvas",
    )
    for sel in selectors:
        loc = page.locator(sel)
        try:
            n = loc.count()
            if n == 0:
                continue
            limit = min(n, 4)
            for i in range(limit):
                target = loc.nth(i)
                if not target.is_visible():
                    continue
                box = target.bounding_box()
                if not box:
                    continue
                # 二维码通常接近正方形且有一定尺寸
                if box["width"] < 60 or box["height"] < 60:
                    continue
                ratio = box["width"] / max(box["height"], 1)
                if ratio < 0.6 or ratio > 1.5:
                    continue
                png = target.screenshot(type="png", timeout=3_000)
                if png and len(png) > 200:
                    return png
        except Exception:  # noqa: BLE001
            continue
    return None


def _capture_security_challenge(page: Page, session_id: str) -> dict[str, Any]:
    t0 = time.time()
    qr_content = _qr_content_from_dom(page)
    png = _screenshot_qr_element(page)
    if png is None:
        # 最后兜底：只截视口中心区域，不扫整页
        try:
            vp = page.viewport_size or {"width": 1280, "height": 720}
            cw = min(420, int(vp["width"]))
            ch = min(420, int(vp["height"]))
            clip = {
                "x": max(0, (vp["width"] - cw) / 2),
                "y": max(0, (vp["height"] - ch) / 2 - 40),
                "width": cw,
                "height": ch,
            }
            png = page.screenshot(type="png", clip=clip, timeout=3_000)
        except Exception:  # noqa: BLE001
            png = page.screenshot(type="png", full_page=False, timeout=5_000)

    path = _save_challenge_image(session_id, png or b"")
    if not qr_content and png:
        qr_content = decode_qr_from_bytes(png)
    expires = time.time() + settings.security_wait_seconds
    log_bus.emit(
        f"安全验证二维码已保存 decoded={'yes' if qr_content else 'no'} "
        f"path={path} cost={(time.time()-t0)*1000:.0f}ms"
    )
    return {
        "type": "security_qr",
        "qr_content": qr_content,
        "image_path": str(path),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
    }


def _wait_human_captcha(
    page: Page,
    *,
    session_id: str,
    cancel_check: CancelCheck,
    pop_input: HumanInputPop,
    set_waiting: SetWaiting,
    set_running: SetRunning,
    deadline_holder: list[float],
    phase_deadline_holder: list[float] | None = None,
) -> None:
    challenge = _capture_captcha_challenge(page, session_id)
    set_waiting("waiting_captcha", challenge)
    log_bus.emit("步骤4 等待人工验证码")
    wait_deadline = time.time() + settings.captcha_wait_seconds
    deadline_holder[0] = max(deadline_holder[0], wait_deadline)
    if phase_deadline_holder is not None:
        phase_deadline_holder[0] = max(phase_deadline_holder[0], wait_deadline + 15)

    while time.time() < wait_deadline:
        _raise_if_cancelled(cancel_check)
        _ensure_alive(page)
        human = pop_input()
        if human and human.get("code"):
            code = str(human["code"]).strip()
            log_bus.emit("步骤4 收到人工验证码，提交")
            set_running()
            token = page.locator("#uc-common-token")
            token.click()
            token.fill("")
            token.type(code, delay=40)
            page.locator(".uc-token-confirm-btn").click()
            _interruptible_sleep(1.5, cancel_check)
            return
        _interruptible_sleep(0.8, cancel_check)
    raise LoginFinished(
        error_code="captcha_timeout",
        error_message="验证码人工等待超时",
    )


def _wait_security_scan(
    page: Page,
    *,
    session_id: str,
    cancel_check: CancelCheck,
    set_waiting: SetWaiting,
    set_running: SetRunning,
    deadline_holder: list[float],
    phase_deadline_holder: list[float] | None = None,
) -> None:
    challenge = _capture_security_challenge(page, session_id)
    set_waiting("waiting_security", challenge)
    log_bus.emit("步骤4 等待安全扫码")
    wait_deadline = time.time() + settings.security_wait_seconds
    deadline_holder[0] = max(deadline_holder[0], wait_deadline)
    if phase_deadline_holder is not None:
        phase_deadline_holder[0] = max(phase_deadline_holder[0], wait_deadline + 15)
    last_refresh = 0.0

    while time.time() < wait_deadline:
        _raise_if_cancelled(cancel_check)
        _ensure_alive(page)
        url = page.url or ""
        if "aq.baidu.com" not in url:
            log_bus.emit("步骤4 已离开安全验证页")
            set_running()
            return
        # 周期性刷新挑战图（二维码可能过期）
        if time.time() - last_refresh > 30:
            try:
                challenge = _capture_security_challenge(page, session_id)
                set_waiting("waiting_security", challenge)
                last_refresh = time.time()
            except Exception as exc:  # noqa: BLE001
                log_bus.emit(f"刷新二维码失败: {exc}")
        _interruptible_sleep(1.5, cancel_check)
    raise LoginFinished(
        error_code="security_timeout",
        error_message="安全验证扫码超时",
    )


def _raise_if_silent_human_required(silent: bool, *, code: str, message: str) -> None:
    if silent:
        log_bus.emit(f"步骤6 静默模式跳过人工介入 code={code}")
        raise LoginFinished(error_code=code, error_message=message)


def _poll_status(
    *,
    page: Page,
    context: Any,
    session_id: str,
    username: str,
    silent: bool,
    deadline_holder: list[float],
    cancel_check: CancelCheck,
    pop_input: HumanInputPop,
    set_waiting: SetWaiting,
    set_running: SetRunning,
    phase_deadline_holder: list[float] | None = None,
    progress: _StepProgress | None = None,
) -> None:
    gateway502 = 0
    unknown = 0
    login_stuck = 0
    reclick_count = 0

    for round_i in range(1, settings.login_status_max_rounds + 1):
        _raise_if_cancelled(cancel_check)
        if time.time() > deadline_holder[0]:
            raise LoginFinished(error_code="timeout", error_message="登录超时")
        # 轮询阶段每轮续期，避免正常等待被阶段看门狗误杀；卡死时由 CDP 无响应+续期停止体现
        if phase_deadline_holder is not None:
            phase_deadline_holder[0] = max(
                phase_deadline_holder[0], time.time() + 45
            )
        if progress is not None:
            progress.set("4.状态轮询", f"round={round_i}")

        _ensure_alive(page)
        _interruptible_sleep(0.6, cancel_check)

        if _page_looks_502(page):
            gateway502 += 1
            if gateway502 > 5:
                raise RuntimeError("登录跳转502")
            log_bus.emit(f"检测到 502，刷新（{gateway502}/5）")
            page.reload(wait_until="domcontentloaded")
            _interruptible_sleep(2, cancel_check)
            continue

        url = page.url or ""
        err_text = _error_node_text(page)
        has_captcha = _captcha_visible(page)
        has_slider = _slider_visible(page)
        on_login = _still_on_login_form(page)
        log_bus.emit(
            f"步骤4 状态轮询 round={round_i} url={url} "
            f"err={err_text!r} captcha={has_captcha} slider={has_slider} "
            f"on_login={on_login} unknown={unknown} stuck={login_stuck}"
        )

        if "b2bwork.baidu.com/dashboard" in url or "b2bwork.baidu.com/ai-agent" in url:
            _interruptible_sleep(1.2, cancel_check)
            cookies = cookies_from_context(context)
            log_bus.emit(
                f"步骤6 登录成功 cookie_count={len(cookies)} url={url}"
            )
            raise LoginFinished(ok=True, cookies=cookies)

        if err_text:
            if "用户名密码错误" in err_text:
                log_bus.emit("步骤6 登录失败 code=bad_credentials")
                raise LoginFinished(
                    error_code="bad_credentials",
                    error_message="用户名密码错误",
                )
            if "登录超时" in err_text:
                log_bus.emit("步骤4 页面提示登录超时，重新点击登录")
                _interruptible_sleep(1, cancel_check)
                page.locator("#submit-form").click()
                continue
            log_bus.emit(f"步骤6 登录失败 code=other_page_error detail={err_text[:200]!r}")
            raise LoginFinished(
                error_code="other_page_error",
                error_message=err_text[:200],
            )

        if has_captcha:
            _raise_if_silent_human_required(
                silent,
                code="captcha_required",
                message="静默模式：需要图片验证码，已跳过",
            )
            log_bus.emit("步骤4 判定：需要图片验证码，进入人工等待")
            _wait_human_captcha(
                page,
                session_id=session_id,
                cancel_check=cancel_check,
                pop_input=pop_input,
                set_waiting=set_waiting,
                set_running=set_running,
                deadline_holder=deadline_holder,
                phase_deadline_holder=phase_deadline_holder,
            )
            unknown = 0
            login_stuck = 0
            continue

        if "aq.baidu.com" in url:
            _raise_if_silent_human_required(
                silent,
                code="security_required",
                message="静默模式：需要安全扫码，已跳过",
            )
            log_bus.emit("步骤4 判定：安全验证页，进入扫码等待")
            _wait_security_scan(
                page,
                session_id=session_id,
                cancel_check=cancel_check,
                set_waiting=set_waiting,
                set_running=set_running,
                deadline_holder=deadline_holder,
                phase_deadline_holder=phase_deadline_holder,
            )
            unknown = 0
            login_stuck = 0
            continue

        if has_slider:
            log_bus.emit("步骤6 登录失败 code=slider_required")
            raise LoginFinished(
                error_code="slider_required",
                error_message="需要滑块验证" if not silent else "静默模式：需要滑块验证，已跳过",
            )

        # 仍停在登录表单：可能点击未生效或页面无响应；尽快失败避免队列堵死
        if on_login:
            login_stuck += 1
            if login_stuck == 6 and reclick_count < 1:
                log_bus.emit("步骤4 仍停在登录页，重新点击登录")
                try:
                    page.locator("#submit-form").click(timeout=3_000)
                    reclick_count += 1
                except Exception as exc:  # noqa: BLE001
                    log_bus.emit(f"重新点击登录失败: {exc}")
            if login_stuck >= 12:
                log_bus.emit("步骤6 登录失败 code=login_page_stuck")
                raise LoginFinished(
                    error_code="login_page_stuck",
                    error_message="卡在登录页无响应",
                )
            continue

        login_stuck = 0
        unknown += 1
        log_bus.emit(f"步骤4 中间态，继续等待 ({unknown}/{settings.login_unknown_limit})")
        if unknown >= settings.login_unknown_limit:
            log_bus.emit("步骤6 登录失败 code=unknown_state")
            raise LoginFinished(
                error_code="unknown_state",
                error_message="登录状态未知",
            )

    log_bus.emit("步骤6 登录失败 code=timeout")
    raise LoginFinished(error_code="timeout", error_message="登录超时")
