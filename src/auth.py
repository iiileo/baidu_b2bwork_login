from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, Response, status, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import settings

_bearer = HTTPBearer(auto_error=False)

COOKIE_NAME = "bw_console_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600


def console_auth_enabled() -> bool:
    """控制台账号密码已配置时启用表单登录。"""
    return bool(settings.basic_auth_user and settings.basic_auth_password)


# 兼容旧名
basic_auth_enabled = console_auth_enabled


def _compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _session_secret() -> str:
    # 优先专用密钥；否则回退 webhook_secret / api_token
    return (
        settings.session_secret
        or settings.webhook_secret
        or settings.api_token
        or "dev-session-secret"
    )


def verify_console_credentials(username: str, password: str) -> bool:
    if not console_auth_enabled():
        return False
    return _compare(username, settings.basic_auth_user) and _compare(
        password, settings.basic_auth_password
    )


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def create_session_token(username: str, *, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + max(60, ttl),
        "v": 1,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(
        _session_secret().encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{body}.{_b64encode(sig)}"


def parse_session_token(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    if not body or not sig:
        return None
    expect = hmac.new(
        _session_secret().encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        got = _b64decode(sig)
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(expect, got):
        return None
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    exp = int(payload.get("exp") or 0)
    if exp < int(time.time()):
        return None
    user = str(payload.get("u") or "")
    if not user or not console_auth_enabled():
        return None
    if not _compare(user, settings.basic_auth_user):
        return None
    return payload


def read_session_cookie(request: Request) -> dict[str, Any] | None:
    return parse_session_token(request.cookies.get(COOKIE_NAME))


def set_session_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_session_token(username),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def request_has_valid_bearer(request: Request) -> bool:
    auth = request.headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return _compare(token.strip(), settings.api_token)


def request_has_valid_session(request: Request) -> bool:
    return read_session_cookie(request) is not None


def unauthorized_api() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
    )


def require_console_session(request: Request) -> dict[str, Any]:
    """控制台 / VNC：必须已登录（Cookie）。"""
    if not console_auth_enabled():
        # 未配置账号时放行（仅建议内网）
        return {"u": "anonymous"}
    payload = read_session_cookie(request)
    if payload is None:
        raise unauthorized_api()
    return payload


def require_api_auth(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> None:
    """API：Bearer Token，或控制台登录 Cookie。"""
    if creds is not None and creds.scheme.lower() == "bearer":
        if _compare(creds.credentials, settings.api_token):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if request_has_valid_bearer(request):
        return

    if console_auth_enabled() and request_has_valid_session(request):
        return

    if not console_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    raise unauthorized_api()


# 旧名兼容
require_token = require_api_auth
require_basic = require_console_session


def _cookies_from_header(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not header:
        return out
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            out[name] = value
    return out


async def websocket_authorized(ws: WebSocket) -> bool:
    """WebSocket：query token / Bearer / 控制台 Cookie。"""
    token = ws.query_params.get("token")
    if token and _compare(token, settings.api_token):
        return True
    auth = ws.headers.get("authorization")
    if auth:
        scheme, _, payload = auth.partition(" ")
        if scheme.lower() == "bearer" and _compare(payload.strip(), settings.api_token):
            return True
    if not console_auth_enabled():
        return False
    cookies = _cookies_from_header(ws.headers.get("cookie"))
    return parse_session_token(cookies.get(COOKIE_NAME)) is not None
