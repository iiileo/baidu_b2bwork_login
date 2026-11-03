from __future__ import annotations

import asyncio
from typing import Iterable

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from src.auth import basic_auth_enabled, require_basic, websocket_authorized
from src.config import settings

router = APIRouter(tags=["vnc-proxy"])

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _upstream_http(path: str, query: str) -> str:
    base = settings.vnc_upstream.rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    return url


def _upstream_ws(path: str, query: str) -> str:
    http_url = _upstream_http(path, query)
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://") :]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://") :]
    return http_url


def _filter_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers:
        if key.lower() in _HOP_BY_HOP:
            continue
        if key.lower() == "authorization":
            # 不把 Basic 凭据转发给本地 websockify
            continue
        out[key] = value
    return out


@router.api_route("/vnc", methods=["GET", "HEAD"])
@router.api_route("/vnc/{full_path:path}", methods=["GET", "HEAD", "POST", "OPTIONS"])
async def proxy_vnc_http(request: Request, full_path: str = "") -> Response:
    require_basic(request)
    if full_path in ("", "/"):
        full_path = "vnc.html"
    url = _upstream_http(full_path, request.url.query)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            upstream = await client.request(
                request.method,
                url,
                headers=_filter_headers(request.headers.items()),
                content=await request.body(),
            )
    except httpx.HTTPError as exc:
        return Response(content=f"VNC upstream unavailable: {exc}", status_code=502)

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-encoding"
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


@router.websocket("/vnc/{full_path:path}")
@router.websocket("/vnc")
async def proxy_vnc_ws(websocket: WebSocket, full_path: str = "websockify") -> None:
    if basic_auth_enabled() and not await websocket_authorized(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    path = full_path or "websockify"
    url = _upstream_ws(path, websocket.url.query)

    try:
        import websockets
    except ImportError:
        await websocket.send_text("websockets package missing")
        await websocket.close(code=1011)
        return

    try:
        async with websockets.connect(
            url,
            open_timeout=10,
            max_size=8 * 1024 * 1024,
            subprotocols=websocket.scope.get("subprotocols") or None,
        ) as upstream:
            async def client_to_upstream() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                except WebSocketDisconnect:
                    pass

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:  # noqa: BLE001
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                _ = task.exception() if not task.cancelled() else None
    except Exception:  # noqa: BLE001
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
