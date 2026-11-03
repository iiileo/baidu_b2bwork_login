from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from src.config import data_path, settings
from src.logbus import log_bus
from src.login.browser import BrowserSession, cookies_from_context
from src.login.exceptions import BrowserUnavailableError, LoginFinished
from src.login.qrcode_util import decode_qr_from_image
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
    "execution context was destroyed",
    "timed out receiving message",
    "no such window",
    "invalid session",
)

CancelCheck = Callable[[], bool]
HumanInputPop = Callable[[], dict[str, Any] | None]
SetWaiting = Callable[[str, dict[str, Any]], None]
SetRunning = Callable[[], None]


def login(
    *,
    store: SessionStore,
    session_id: str,
    username: str,
    password: str,
    webhook_url: str | None,
    client_ref: str | None,
    silent: bool = False,
) -> None:
    """执行登录；通过 LoginFinished 抛出终态，或抛 BrowserUnavailableError 供重试。"""
    deadline = time.time() + settings.login_total_timeout
    session = BrowserSession()

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

    try:
        if cancel_check():
            raise LoginFinished(cancelled=True, error_code="cancelled", error_message="已取消")
        page = session.start()
        _open_and_fill(page, username, password)
        _click_login(page)
        _poll_status(
            page=page,
            context=session.context,
            session_id=session_id,
            username=username,
            silent=silent,
            deadline_holder=[deadline],
            cancel_check=cancel_check,
            pop_input=pop_input,
            set_waiting=set_waiting,
            set_running=set_running,
        )
    except LoginFinished:
        raise
    except BrowserUnavailableError:
        raise
    except PlaywrightError as exc:
        if _is_session_error(exc):
            raise BrowserUnavailableError(str(exc)) from exc
        raise
    finally:
        session.close()


def _is_session_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in SESSION_ERR_HINTS)


def _is_blank_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u in BLANK_URLS or u.startswith("about:")


def _ensure_alive(page: Page) -> None:
    try:
        url = page.url
        if _is_blank_url(url):
            raise BrowserUnavailableError(f"页面变为空白页: {url!r}")
        _ = page.evaluate("() => document.readyState")
    except BrowserUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_session_error(exc):
            raise BrowserUnavailableError(str(exc)) from exc
        raise BrowserUnavailableError(f"会话检测失败: {exc}") from exc


def _raise_if_cancelled(cancel_check: CancelCheck) -> None:
    if cancel_check():
        raise LoginFinished(cancelled=True, error_code="cancelled", error_message="已取消")


def _open_login_page(page: Page) -> None:
    log_bus.emit("步骤1 打开登录页面")
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            page.goto(settings.login_url, wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            last_err = exc
            try:
                page.evaluate("() => window.stop()")
            except Exception:  # noqa: BLE001
                pass
        except PlaywrightError as exc:
            if _is_session_error(exc):
                raise BrowserUnavailableError(str(exc)) from exc
            last_err = exc

        end = time.time() + 15
        while time.time() < end:
            try:
                url = page.url
                if _is_blank_url(url):
                    time.sleep(0.3)
                    continue
                ready = page.evaluate("() => document.readyState")
                source = page.content()
                if ready == "complete" and len(source) >= 100:
                    log_bus.emit(f"步骤1 登录页就绪 url={url}")
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if _is_session_error(exc):
                    raise BrowserUnavailableError(str(exc)) from exc
            time.sleep(0.3)
        log_bus.emit(f"步骤1 第 {attempt} 次打开仍未就绪，重试")
    raise BrowserUnavailableError(f"三次打开登录页均为空页或未就绪: {last_err}")


def _human_move(page: Page) -> None:
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        for _ in range(3):
            x = random.randint(20, max(21, viewport["width"] - 20))
            y = random.randint(20, max(21, viewport["height"] - 20))
            page.mouse.move(x, y, steps=random.randint(5, 15))
            time.sleep(random.uniform(0.05, 0.2))
    except Exception:  # noqa: BLE001
        pass


def _fill_credentials(page: Page, username: str, password: str) -> None:
    page.wait_for_selector("#submit-form", timeout=10_000)
    _human_move(page)
    time.sleep(1)
    account = page.locator("#uc-common-account")
    account.click()
    account.fill("")
    account.type(username, delay=random.randint(40, 90))
    time.sleep(1)
    pwd = page.locator("#ucsl-password-edit")
    pwd.click()
    pwd.fill("")
    pwd.type(password, delay=random.randint(40, 90))
    time.sleep(1)


def _open_and_fill(page: Page, username: str, password: str) -> None:
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            _open_login_page(page)
            _fill_credentials(page, username, password)
            log_bus.emit("步骤2 账号密码填写完成")
            return
        except BrowserUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_session_error(exc):
                raise BrowserUnavailableError(str(exc)) from exc
            last_err = exc
            log_bus.emit(f"步骤2 填表失败（第 {attempt} 次）: {exc}")
    raise BrowserUnavailableError(f"表单元素不可用: {last_err}")


def _click_login(page: Page) -> None:
    _ensure_alive(page)
    log_bus.emit("步骤3 点击登录")
    page.locator("#submit-form").click()
    time.sleep(5)


def _page_looks_502(page: Page) -> bool:
    try:
        title = (page.title() or "").lower()
        snippet = (page.content() or "")[:4000].lower()
    except Exception:  # noqa: BLE001
        return False
    blob = title + "\n" + snippet
    return any(
        n in blob
        for n in (
            "502 bad gateway",
            "error 502",
            "http error 502",
            "<h1>502",
            "<title>502",
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
                text = (node.inner_text(timeout=800) or "").strip()
                if text:
                    return text
        except Exception:  # noqa: BLE001
            continue
    return None


def _source_contains(page: Page, needle: str) -> bool:
    try:
        return needle in (page.content() or "")
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


def _capture_security_challenge(page: Page, session_id: str) -> dict[str, Any]:
    png: bytes | None = None
    # 尝试常见二维码 img
    for sel in ("img[src*='qr']", "img.qrcode", "canvas", "img"):
        loc = page.locator(sel)
        try:
            if loc.count() == 0:
                continue
            target = loc.first
            if not target.is_visible():
                continue
            box = target.bounding_box()
            if not box or box["width"] < 40 or box["height"] < 40:
                continue
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
            png = buf.getvalue()
            break
        except Exception:  # noqa: BLE001
            continue
    if png is None:
        png = page.screenshot(full_page=False)

    path = _save_challenge_image(session_id, png)
    qr_content = decode_qr_from_image(path)
    expires = time.time() + settings.security_wait_seconds
    log_bus.emit(
        f"安全验证二维码已保存 decoded={'yes' if qr_content else 'no'} path={path}"
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
) -> None:
    challenge = _capture_captcha_challenge(page, session_id)
    set_waiting("waiting_captcha", challenge)
    log_bus.emit("步骤4 等待人工验证码")
    wait_deadline = time.time() + settings.captcha_wait_seconds
    deadline_holder[0] = max(deadline_holder[0], wait_deadline)

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
            token.type(code, delay=50)
            page.locator(".uc-token-confirm-btn").click()
            time.sleep(2)
            return
        time.sleep(1)
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
) -> None:
    challenge = _capture_security_challenge(page, session_id)
    set_waiting("waiting_security", challenge)
    log_bus.emit("步骤4 等待安全扫码")
    wait_deadline = time.time() + settings.security_wait_seconds
    deadline_holder[0] = max(deadline_holder[0], wait_deadline)
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
        time.sleep(2)
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
) -> None:
    gateway502 = 0
    unknown = 0

    for round_i in range(1, settings.login_status_max_rounds + 1):
        _raise_if_cancelled(cancel_check)
        if time.time() > deadline_holder[0]:
            raise LoginFinished(error_code="timeout", error_message="登录超时")

        _ensure_alive(page)
        time.sleep(1)

        if _page_looks_502(page):
            gateway502 += 1
            if gateway502 > 5:
                raise RuntimeError("登录跳转502")
            log_bus.emit(f"检测到 502，刷新（{gateway502}/5）")
            page.reload(wait_until="domcontentloaded")
            time.sleep(3)
            continue

        url = page.url or ""
        err_text = _error_node_text(page)
        has_captcha = _source_contains(page, "请填写验证码")
        has_slider = _source_contains(page, "请完成下方验证后继续操作")
        log_bus.emit(
            f"步骤4 状态轮询 round={round_i} url={url} "
            f"err={err_text!r} captcha={has_captcha} slider={has_slider} "
            f"unknown={unknown}"
        )

        if "b2bwork.baidu.com/dashboard" in url or "b2bwork.baidu.com/ai-agent" in url:
            time.sleep(2)
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
                time.sleep(2)
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
            )
            unknown = 0
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
            )
            unknown = 0
            continue

        if has_slider:
            log_bus.emit("步骤6 登录失败 code=slider_required")
            raise LoginFinished(
                error_code="slider_required",
                error_message="需要滑块验证" if not silent else "静默模式：需要滑块验证，已跳过",
            )

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
