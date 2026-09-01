"""Health endpoint."""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import get_settings
from app.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        solari_configured=settings.solari_configured(),
        database=settings.database_backend(),
        time=datetime.now(timezone.utc),
    )
