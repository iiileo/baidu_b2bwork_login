from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Auth
    api_token: str = "dev-token-change-me"
    webhook_secret: str = "dev-webhook-secret-change-me"
    # 控制台表单登录账号（都配置后启用 Cookie 会话）
    # 兼容旧名 BASIC_AUTH_USER / BASIC_AUTH_PASSWORD
    console_user: str = Field(
        default="admin",
        validation_alias=AliasChoices(
            "CONSOLE_USER", "console_user", "BASIC_AUTH_USER", "basic_auth_user"
        ),
    )
    console_password: str = Field(
        default="admin",
        validation_alias=AliasChoices(
            "CONSOLE_PASSWORD",
            "console_password",
            "BASIC_AUTH_PASSWORD",
            "basic_auth_password",
        ),
    )
    # Cookie 签名密钥（留空则回退 webhook_secret / api_token）
    session_secret: str = ""

    # Storage
    data_dir: str = "./data"
    queue_db_path: str = "./data/queue.db"
    session_ttl_days: int = 7

    # Queue / retry
    login_max_attempts: int = 3
    login_retry_delay: int = 5
    login_poll_interval: float = 3.0
    login_session_stale: int = 600

    # Human wait
    captcha_wait_seconds: int = 300
    security_wait_seconds: int = 300

    # Browser / Playwright
    playwright_browser: str = "chromium"
    playwright_headless: bool = True
    playwright_create_retry: int = 3
    playwright_navigation_timeout_ms: int = 60_000
    playwright_default_timeout_ms: int = 30_000

    # Login page
    login_url: str = "http://b2bwork.baidu.com"
    login_total_timeout: int = 600
    # 打开/填表/点击阶段看门狗（含启动浏览器）；超时走任务级重试
    login_phase_timeout: int = 90
    login_status_max_rounds: int = 300
    login_unknown_limit: int = 30

    # VNC / display（鉴权走控制台登录 Cookie + /vnc 代理，无需 VNC 密码）
    enable_vnc: bool = False
    display: str = ":99"
    # 本地 websockify/noVNC 上游（仅容器内访问；对外走 /vnc 代理）
    vnc_upstream: str = "http://127.0.0.1:7900"
    # 浏览器访问 noVNC 的默认地址（同源代理，GUI 可覆盖）
    vnc_page_url: str = "/vnc/vnc.html?autoconnect=true&resize=scale&path=vnc/websockify"
    # 浏览器语言（VNC 下窗口默认铺满 1280x800）
    browser_locale: str = "zh-CN"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8056


settings = Settings()


def data_path(*parts: str) -> Path:
    root = Path(settings.data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ensure_data_dir() -> Path:
    db = Path(settings.queue_db_path).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    data_path("challenges")
    return db
