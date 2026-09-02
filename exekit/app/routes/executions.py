"""Thin route: POST /executions delegates to services.executions."""

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session as DBSession

from app.dependencies import (
    get_current_api_key,
    get_db,
    get_runner,
    require_executions_rate_limit,
)
from app.models import ApiKey
from app.schemas import ExecutionRequest, ExecutionResponse
from app.services import executions as executions_service
from app.services.solari_runner import SolariRunner

logger = logging.getLogger("exekit.executions")
router = APIRouter()


@router.post("/executions", response_model=ExecutionResponse)
async def create_execution(
    body: ExecutionRequest,
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
    runner: SolariRunner = Depends(get_runner),
    _rl=Depends(require_executions_rate_limit),
) -> ExecutionResponse:
    return await executions_service.execute_code(
        db, api_key, body.code, body.session_id, runner=runner
    )
