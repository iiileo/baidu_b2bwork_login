from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class CreateSessionRequest(BaseModel):
    username: str
    password: str
    webhook_url: str | None = None
    priority: int = Field(default=1, ge=1)
    client_ref: str | None = None
    silent: bool = False

    @field_validator("username", "password")
    @classmethod
    def non_empty(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("字段不能为空")
        return v


class CreateSessionResponse(BaseModel):
    id: str
    status: str
    queue_position: int


class CaptchaSubmitRequest(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def non_empty(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("验证码不能为空")
        return v.strip()


class QueueStatsResponse(BaseModel):
    queued: int
    running: int
    waiting: int
    retrying: int


class SessionResponse(BaseModel):
    id: str
    status: str
    username: str
    client_ref: str | None = None
    priority: int = 1
    silent: bool = False
    created_at: str
    updated_at: str
    finished_at: str | None = None
    challenge: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
