from __future__ import annotations

import base64
from pathlib import Path

import httpx

from src.config import settings
from src.logbus import log_bus


def recognize_captcha(image_path: str | Path) -> str:
    """调用云打码识别图片验证码，类型 10110。返回码非 10000 视为失败。"""
    token = settings.app_ydm_token
    if not token:
        raise RuntimeError("未配置 APP_YDM_TOKEN，无法识别验证码")

    raw = Path(image_path).read_bytes()
    payload = {
        "token": token,
        "type": settings.ydm_type,
        "image": base64.b64encode(raw).decode("ascii"),
    }
    log_bus.emit("云打码：提交验证码识别")
    with httpx.Client(timeout=settings.ydm_timeout) as client:
        resp = client.post(settings.ydm_api_url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    code = str(data.get("code", ""))
    if code != "10000":
        raise RuntimeError(f"云打码识别失败: {data}")

    text = str(data.get("data", {}).get("data", "")).strip()
    if not text:
        # 兼容部分返回结构
        text = str(data.get("data", "")).strip()
    if not text:
        raise RuntimeError(f"云打码返回空结果: {data}")
    log_bus.emit(f"云打码：识别结果长度 {len(text)}")
    return text
