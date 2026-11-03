from __future__ import annotations

import base64
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import settings

_bearer = HTTPBearer(auto_error=False)


def basic_auth_enabled() -> bool:
    return bool(settings.basic_auth_user and settings.basic_auth_password)


def _compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def parse_basic_header(authorization: str | None) -> tuple[str, str] | None:
    if not authorization:
        return None
    scheme, _, payload = authorization.partition(" ")
    if scheme.lower() != "basic" or not payload:
        return None
    try:
        decoded = base64.b64decode(payload.strip()).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


def verify_basic_credentials(username: str, password: str) -> bool:
    if not basic_auth_enabled():
        return False
    return _compare(username, settings.basic_auth_user) and _compare(
        password, settings.basic_auth_password
    )


def request_has_valid_basic(request: Request) -> bool:
    parsed = parse_basic_header(request.headers.get("authorization"))
    if parsed is None:
        return False
    return verify_basic_credentials(parsed[0], parsed[1])


def request_has_valid_bearer(request: Request) -> bool:
    auth = request.headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return _compare(token.strip(), settings.api_token)


def unauthorized_basic() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="B2BWork Console"'},
    )


def require_basic(request: Request) -> None:
    """Console / VNC：启用 Basic 时必须通过。"""
    if not basic_auth_enabled():
        return
    if not request_has_valid_basic(request):
        raise unauthorized_basic()


def require_api_auth(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> None:
    """API：Bearer Token，或（启用时）Basic 账号密码。"""
    if creds is not None and creds.scheme.lower() == "bearer":
        if _compare(creds.credentials, settings.api_token):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if basic_auth_enabled() and request_has_valid_basic(request):
        return

    # 兼容：未带 Bearer scheme 但走了 HTTPBearer 空值时，再尝试 Bearer 原文
    if request_has_valid_bearer(request):
        return

    if basic_auth_enabled():
        raise unauthorized_basic()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing bearer token",
    )


# 旧名兼容
require_token = require_api_auth


async def websocket_authorized(ws: WebSocket) -> bool:
    """WebSocket：query token=API_TOKEN，或 Basic / Bearer 头。"""
    token = ws.query_params.get("token")
    if token and _compare(token, settings.api_token):
        return True
    auth = ws.headers.get("authorization")
    parsed = parse_basic_header(auth)
    if parsed and verify_basic_credentials(parsed[0], parsed[1]):
        return True
    if auth:
        scheme, _, payload = auth.partition(" ")
        if scheme.lower() == "bearer" and _compare(payload.strip(), settings.api_token):
            return True
    return False
