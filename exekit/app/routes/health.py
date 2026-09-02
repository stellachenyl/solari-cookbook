"""Health endpoint."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlmodel import Session as DBSession, func, select

from app.config import get_settings
from app.dependencies import get_db
from app.models import SandboxSession
from app.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(db: DBSession = Depends(get_db)) -> HealthResponse:
    settings = get_settings()
    active_sessions = db.exec(
        select(func.count()).select_from(SandboxSession).where(SandboxSession.status == "active")
    ).one()
    return HealthResponse(
        status="ok",
        solari_configured=settings.solari_configured(),
        billing_enabled=settings.billing_enabled,
        database=settings.database_backend(),
        active_sessions=active_sessions,
        time=datetime.now(timezone.utc),
    )
