from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config import data_path, settings


def _settings_file() -> Path:
    return data_path("gui_settings.json")


def _password_file() -> Path:
    return data_path("vnc_password")


def load_gui_settings() -> dict[str, Any]:
    defaults = {
        "vnc_page_url": settings.vnc_page_url,
        "vnc_password_set": _password_file().exists() or bool(settings.vnc_password),
        "default_webhook_url": "",
        "default_priority": 1,
    }
    path = _settings_file()
    if not path.exists():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return defaults
    defaults.update({k: v for k, v in raw.items() if k != "vnc_password"})
    defaults["vnc_password_set"] = _password_file().exists() or bool(settings.vnc_password)
    return defaults


def save_gui_settings(
    *,
    vnc_page_url: str | None = None,
    vnc_password: str | None = None,
    clear_vnc_password: bool = False,
    default_webhook_url: str | None = None,
    default_priority: int | None = None,
) -> dict[str, Any]:
    current = load_gui_settings()
    if vnc_page_url is not None:
        current["vnc_page_url"] = vnc_page_url.strip()
    if default_webhook_url is not None:
        current["default_webhook_url"] = default_webhook_url.strip()
    if default_priority is not None:
        current["default_priority"] = max(1, int(default_priority))

    persist = {
        "vnc_page_url": current["vnc_page_url"],
        "default_webhook_url": current.get("default_webhook_url", ""),
        "default_priority": current.get("default_priority", 1),
    }
    _settings_file().write_text(
        json.dumps(persist, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    pwd_path = _password_file()
    if clear_vnc_password:
        pwd_path.unlink(missing_ok=True)
    elif vnc_password is not None:
        if vnc_password == "":
            pwd_path.unlink(missing_ok=True)
        else:
            pwd_path.write_text(vnc_password, encoding="utf-8")

    return load_gui_settings()


def read_vnc_password() -> str:
    pwd_path = _password_file()
    if pwd_path.exists():
        return pwd_path.read_text(encoding="utf-8").strip()
    return (settings.vnc_password or "").strip()
