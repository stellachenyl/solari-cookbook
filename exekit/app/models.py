"""SQLModel tables for ExecKit (docs/EXEKIT_SPEC.md §E)."""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    id: int | None = Field(default=None, primary_key=True)
    key_hash: str = Field(unique=True, index=True)
    key_last4: str
    email: str | None = None
    plan: str = "free"
    credits: int = 0
    executions_count: int = 0
    is_active: bool = True
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SandboxSession(SQLModel, table=True):
    __tablename__ = "sandbox_sessions"

    id: str = Field(primary_key=True)
    solari_session_id: str = Field(index=True)
    api_key_id: int = Field(foreign_key="api_keys.id")
    status: str = "active"  # active | killed | expired | lost
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime = Field(default_factory=utcnow)
    killed_at: datetime | None = None


class Execution(SQLModel, table=True):
    __tablename__ = "executions"

    id: int | None = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="api_keys.id")
    session_id: str | None = None
    code: str
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    status: str  # completed | failed | timeout | solari_unconfigured | solari_unavailable | unsupported
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class CreditLedger(SQLModel, table=True):
    __tablename__ = "credit_ledger"

    id: int | None = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="api_keys.id")
    amount: int
    balance_after: int
    reason: str  # signup_grant | execution | solari_refund | admin_grant
    created_at: datetime = Field(default_factory=utcnow)
