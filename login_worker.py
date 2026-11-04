from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from typing import Any

from src.config import ensure_data_dir, settings
from src.logbus import log_bus, setup_logging
from src.login import BrowserUnavailableError, LoginFinished, login
from src.store import SessionStore
from src.webhook import send_webhook

_store: SessionStore | None = None
_current_id: str | None = None
_stopping = False


def _retry_delay(attempts: int) -> float:
    return float(min(settings.login_retry_delay * attempts, 60))


def _emit_finished(rec: Any, status: str) -> None:
    send_webhook(
        rec.webhook_url,
        "session.finished",
        {
            "session_id": rec.id,
            "status": status,
            "username": rec.username,
            "client_ref": rec.client_ref,
            "result": rec.result,
            "error": rec.error,
        },
    )


def _cleanup_exhausted(store: SessionStore) -> None:
    for rec in store.list_exhausted(settings.login_max_attempts):
        finished = store.finish(
            rec.id,
            status="failed",
            error={
                "code": "login_error",
                "message": "登录失败：重试次数已用尽",
            },
        )
        if finished:
            _emit_finished(finished, "failed")
            log_bus.emit(f"清理僵尸会话 id={rec.id}")


def _purge_challenge_file(session_id: str) -> None:
    path = Path(settings.data_dir).expanduser().resolve() / "challenges" / f"{session_id}.png"
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stopping
    log_bus.emit(f"收到退出信号 {signum}")
    _stopping = True
    if _store is not None and _current_id is not None:
        try:
            _store.release_for_retry(_current_id, delay_seconds=0)
        except Exception as exc:  # noqa: BLE001
            log_bus.emit(f"释放会话失败: {exc}")


def process_once(store: SessionStore) -> bool:
    global _current_id

    claimed = store.claim(
        max_attempts=settings.login_max_attempts,
        stale_seconds=settings.login_session_stale,
    )
    if claimed is None:
        return False

    _current_id = claimed.id
    try:
        if claimed.cancel_requested or not claimed.password:
            finished = store.finish(
                claimed.id,
                status="cancelled",
                error={"code": "cancelled", "message": "已取消"},
            )
            if finished:
                _emit_finished(finished, "cancelled")
            return True

        log_bus.emit(
            f"开始处理会话 id={claimed.id} username={claimed.username} "
            f"silent={claimed.silent} "
            f"第 {claimed.attempts}/{settings.login_max_attempts} 次"
        )

        try:
            login(
                store=store,
                session_id=claimed.id,
                username=claimed.username,
                password=claimed.password,
                webhook_url=claimed.webhook_url,
                client_ref=claimed.client_ref,
                silent=claimed.silent,
            )
            # login 正常不应落到这里（应 LoginFinished）
            finished = store.finish(
                claimed.id,
                status="failed",
                error={"code": "unknown_state", "message": "登录无结果"},
            )
            if finished:
                _emit_finished(finished, "failed")
        except LoginFinished as done:
            if done.cancelled:
                log_bus.emit(
                    f"步骤7 结果=cancelled id={claimed.id} "
                    f"message={done.error_message or '已取消'}"
                )
                finished = store.finish(
                    claimed.id,
                    status="cancelled",
                    error={"code": "cancelled", "message": done.error_message or "已取消"},
                )
                if finished:
                    _emit_finished(finished, "cancelled")
            elif done.ok:
                n = len(done.cookies or [])
                log_bus.emit(f"步骤7 结果=succeeded id={claimed.id} cookies={n}")
                finished = store.finish(
                    claimed.id,
                    status="succeeded",
                    result={"cookies": done.cookies or []},
                )
                if finished:
                    _emit_finished(finished, "succeeded")
            else:
                code = done.error_code or "login_error"
                msg = done.error_message or "登录失败"
                log_bus.emit(
                    f"步骤7 结果=failed id={claimed.id} code={code} message={msg}"
                )
                finished = store.finish(
                    claimed.id,
                    status="failed",
                    error={"code": code, "message": msg},
                )
                if finished:
                    _emit_finished(finished, "failed")
            _purge_challenge_file(claimed.id)
            if finished:
                log_bus.emit(
                    f"会话结束 id={claimed.id} status={finished.status} "
                    f"error={finished.error} result_keys="
                    f"{list((finished.result or {}).keys())}"
                )
        except BrowserUnavailableError as exc:
            log_bus.emit(f"浏览器不可用: {exc}")
            _fail_or_retry(store, claimed, browser=True, exc=exc)
        except Exception as exc:  # noqa: BLE001
            log_bus.emit(f"登录异常: {exc}")
            _fail_or_retry(store, claimed, browser=False, exc=exc)
    finally:
        _current_id = None
    return True


def _fail_or_retry(
    store: SessionStore,
    claimed: Any,
    *,
    browser: bool,
    exc: Exception,
) -> None:
    # 取消优先：异常路径也不得把已取消会话重新入队（否则永远 claim 不到）
    if store.is_cancel_requested(claimed.id):
        finished = store.finish(
            claimed.id,
            status="cancelled",
            error={"code": "cancelled", "message": "已取消"},
        )
        if finished:
            _emit_finished(finished, "cancelled")
        _purge_challenge_file(claimed.id)
        log_bus.emit(f"异常路径收尾取消 id={claimed.id}")
        return
    if claimed.attempts >= settings.login_max_attempts:
        code = "browser_error" if browser else "login_error"
        finished = store.finish(
            claimed.id,
            status="failed",
            error={"code": code, "message": str(exc)},
        )
        if finished:
            _emit_finished(finished, "failed")
        _purge_challenge_file(claimed.id)
        log_bus.emit(f"重试用尽 id={claimed.id}")
        return
    delay = _retry_delay(claimed.attempts)
    released = store.release_for_retry(claimed.id, delay_seconds=delay)
    if not released:
        finished = store.get(claimed.id)
        if finished and finished.status == "cancelled":
            _emit_finished(finished, "cancelled")
            _purge_challenge_file(claimed.id)
        log_bus.emit(f"释放时发现已取消 id={claimed.id}")
        return
    log_bus.emit(f"会话释放重试 id={claimed.id} delay={delay}s")


def main() -> None:
    global _store
    setup_logging()
    ensure_data_dir()
    store = SessionStore(settings.queue_db_path)
    _store = store

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log_bus.emit(
        f"login_worker 启动 db={settings.queue_db_path} "
        f"browser={settings.playwright_browser} headless={settings.playwright_headless} "
        f"vnc={settings.enable_vnc}"
    )
    _cleanup_exhausted(store)
    store.purge_expired(settings.session_ttl_days)

    while not _stopping:
        try:
            for zombie in store.finish_cancel_requested():
                _emit_finished(zombie, "cancelled")
                _purge_challenge_file(zombie.id)
                log_bus.emit(f"收尾已取消的排队会话 id={zombie.id}")
            got = process_once(store)
        except Exception as exc:  # noqa: BLE001
            log_bus.emit(f"worker 循环异常: {exc}")
            got = False
        if not got:
            time.sleep(settings.login_poll_interval)

    log_bus.emit("login_worker 已退出")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
