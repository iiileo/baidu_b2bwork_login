from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import (
    basic_auth_enabled,
    require_api_auth,
    require_basic,
    websocket_authorized,
)
from src.config import ensure_data_dir, settings
from src.gui_settings import load_gui_settings, read_vnc_password, save_gui_settings
from src.logbus import log_bus, setup_logging
from src.schemas import (
    CaptchaSubmitRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    QueueStatsResponse,
    SessionResponse,
)
from src.store import SessionStore
from src.vnc_proxy import router as vnc_router

WEB_DIR = Path(__file__).resolve().parent / "web"


class ConsoleSettingsUpdate(BaseModel):
    vnc_page_url: str | None = None
    vnc_password: str | None = None
    clear_vnc_password: bool = False
    default_webhook_url: str | None = None
    default_priority: int | None = Field(default=None, ge=1)


class BasicGateMiddleware(BaseHTTPMiddleware):
    """保护 /ui 静态资源：启用 Basic 时未登录则弹浏览器账号框。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/ui" or path.startswith("/ui/"):
            if basic_auth_enabled():
                try:
                    require_basic(request)
                except HTTPException as exc:
                    return Response(
                        content=exc.detail,
                        status_code=exc.status_code,
                        headers=exc.headers,
                    )
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    ensure_data_dir()
    app.state.store = SessionStore(settings.queue_db_path)
    log_bus.bind_loop(asyncio.get_running_loop())
    mode = "basic+bearer" if basic_auth_enabled() else "bearer-only"
    log_bus.emit(f"API 启动 db={settings.queue_db_path} auth={mode}")
    yield


app = FastAPI(
    title="B2BWork Login",
    version="0.3.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(BasicGateMiddleware)
app.include_router(vnc_router)


def _store() -> SessionStore:
    return app.state.store


def _public_challenge(challenge: dict | None) -> dict | None:
    if not challenge:
        return None
    ctype = challenge.get("type")
    out = {
        "type": ctype,
        "qr_content": challenge.get("qr_content"),
        "expires_at": challenge.get("expires_at"),
        "image_base64": None,
    }
    # 安全扫码只返回 qr_content；图片验证码才带 image_base64
    if ctype == "captcha_image":
        out["image_base64"] = challenge.get("image_base64")
        if not out["image_base64"]:
            path = challenge.get("image_path")
            if path and Path(path).exists():
                b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
                out["image_base64"] = f"data:image/png;base64,{b64}"
    return out


def _to_response(rec) -> SessionResponse:
    data = rec.to_public_dict()
    data["challenge"] = _public_challenge(rec.challenge)
    return SessionResponse(**data)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


@app.get("/docs.html")
def api_docs_page() -> FileResponse:
    path = WEB_DIR / "docs.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="docs.html missing")
    return FileResponse(path)


@app.get("/v1/console/bootstrap")
def console_bootstrap() -> dict[str, Any]:
    return {
        "basic_auth_required": basic_auth_enabled(),
        "default_vnc_page_url": settings.vnc_page_url,
        "api_token_optional": basic_auth_enabled(),
    }


@app.get("/v1/console/settings", dependencies=[Depends(require_api_auth)])
def get_console_settings() -> dict[str, Any]:
    return load_gui_settings()


@app.put("/v1/console/settings", dependencies=[Depends(require_api_auth)])
def put_console_settings(body: ConsoleSettingsUpdate) -> dict[str, Any]:
    updated = save_gui_settings(
        vnc_page_url=body.vnc_page_url,
        vnc_password=body.vnc_password,
        clear_vnc_password=body.clear_vnc_password,
        default_webhook_url=body.default_webhook_url,
        default_priority=body.default_priority,
    )
    log_bus.emit("控制台设置已更新")
    return updated


@app.get("/v1/console/vnc-embed-password", dependencies=[Depends(require_api_auth)])
def vnc_embed_password() -> dict[str, str]:
    """兼容旧逻辑；推荐走 /vnc 同源代理 + Basic，无需 VNC 密码。"""
    return {"password": read_vnc_password()}


@app.post(
    "/v1/sessions",
    response_model=CreateSessionResponse,
    status_code=202,
    dependencies=[Depends(require_api_auth)],
)
def create_session(
    body: CreateSessionRequest,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=255
    ),
) -> CreateSessionResponse:
    store = _store()
    rec, position, duplicate = store.create(
        username=body.username,
        password=body.password,
        webhook_url=body.webhook_url,
        priority=body.priority,
        client_ref=body.client_ref,
        silent=body.silent,
        idempotency_key=idempotency_key,
    )
    if duplicate:
        log_bus.emit(
            f"幂等重试复用会话 id={rec.id} username={rec.username} status={rec.status}"
        )
    else:
        log_bus.emit(
            f"会话已入队 id={rec.id} username={rec.username} "
            f"priority={rec.priority} silent={rec.silent} queue_position={position}"
        )
    return CreateSessionResponse(
        id=rec.id,
        status=rec.status,
        queue_position=position,
        duplicate=duplicate,
    )


@app.get(
    "/v1/sessions/{session_id}",
    response_model=SessionResponse,
    dependencies=[Depends(require_api_auth)],
)
def get_session(session_id: str) -> SessionResponse:
    rec = _store().get(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _to_response(rec)


@app.get("/v1/sessions/{session_id}/challenge", dependencies=[Depends(require_api_auth)])
def get_challenge(session_id: str) -> dict:
    rec = _store().get(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    if rec.status not in ("waiting_captcha", "waiting_security") or not rec.challenge:
        raise HTTPException(status_code=404, detail="no active challenge")
    challenge = _public_challenge(rec.challenge)
    return {"session_id": session_id, "status": rec.status, "challenge": challenge}


@app.post(
    "/v1/sessions/{session_id}/captcha",
    response_model=SessionResponse,
    dependencies=[Depends(require_api_auth)],
)
def submit_captcha(session_id: str, body: CaptchaSubmitRequest) -> SessionResponse:
    store = _store()
    try:
        rec = store.submit_captcha(session_id, body.code)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    log_bus.emit(f"人工验证码已提交 id={session_id}")
    return _to_response(rec)


@app.delete(
    "/v1/sessions/{session_id}",
    response_model=SessionResponse,
    dependencies=[Depends(require_api_auth)],
)
def cancel_session(session_id: str) -> SessionResponse:
    rec = _store().request_cancel(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    log_bus.emit(f"取消请求 id={session_id} status={rec.status}")
    return _to_response(rec)


@app.get(
    "/v1/queue",
    response_model=QueueStatsResponse,
    dependencies=[Depends(require_api_auth)],
)
def queue_stats() -> QueueStatsResponse:
    return QueueStatsResponse(**_store().queue_stats())


@app.websocket("/v1/logs")
async def wslog(ws: WebSocket) -> None:
    if not await websocket_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    q = log_bus.subscribe()
    try:
        await ws.send_text("connected")
        while True:
            line = await q.get()
            await ws.send_text(line)
    except WebSocketDisconnect:
        pass
    finally:
        log_bus.unsubscribe(q)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


if WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
