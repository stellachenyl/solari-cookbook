"""Pydantic request/response models (docs/EXEKIT_SPEC.md §D)."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr


class HealthResponse(BaseModel):
    status: str = "ok"
    solari_configured: bool
    database: str
    time: datetime


class KeyRequest(BaseModel):
    email: Optional[EmailStr] = None


class KeyResponse(BaseModel):
    api_key: str
    key_last4: str
    credits: int
    plan: str


class KeyInfoResponse(BaseModel):
    key_last4: str
    email: Optional[str] = None
    plan: str
    credits: int
    executions_count: int
    is_active: bool


class ExecutionRequest(BaseModel):
    code: str
    session_id: Optional[str] = None


class ExecutionResponse(BaseModel):
    execution_id: int
    session_id: Optional[str] = None
    stdout: str
    stderr: str
    exit_code: Optional[int] = None
    status: str
    error: Optional[str] = None
    artifacts: list = []
    credits_remaining: int


class SessionResponse(BaseModel):
    session_id: str
    status: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict = {}


class ErrorResponse(BaseModel):
    error: ErrorDetail
