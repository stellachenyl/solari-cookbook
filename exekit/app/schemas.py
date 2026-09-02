"""Pydantic request/response models (docs/EXEKIT_SPEC.md §D)."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class HealthResponse(BaseModel):
    status: str = "ok"
    solari_configured: bool
    billing_enabled: bool
    database: str
    active_sessions: int
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


MAX_CODE_LENGTH = 50_000
MAX_SESSION_ID_LENGTH = 200


class ExecutionRequest(BaseModel):
    code: str
    session_id: Optional[str] = None

    @field_validator("code")
    @classmethod
    def _code_not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("code must not be empty or whitespace-only")
        if len(v) > MAX_CODE_LENGTH:
            raise ValueError(f"code must be at most {MAX_CODE_LENGTH} characters")
        return v

    @field_validator("session_id")
    @classmethod
    def _session_id_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > MAX_SESSION_ID_LENGTH:
            raise ValueError(f"session_id must be at most {MAX_SESSION_ID_LENGTH} characters")
        return v


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
    truncated: bool = False


class SessionResponse(BaseModel):
    session_id: str
    status: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict = {}


class ErrorResponse(BaseModel):
    error: ErrorDetail

# --- billing --------------------------------------------------------------------


class BillingConfigResponse(BaseModel):
    billing_enabled: bool
    credit_amount: int
    price_usd: int
    currency: str = "usd"


class CheckoutRequest(BaseModel):
    credits: Optional[int] = None


class CheckoutResponse(BaseModel):
    checkout_url: str


class LedgerEntry(BaseModel):
    amount: int
    balance_after: int
    reason: str
    created_at: datetime


class LedgerResponse(BaseModel):
    key_last4: str
    credits: int
    ledger: list[LedgerEntry] = Field(default_factory=list)
