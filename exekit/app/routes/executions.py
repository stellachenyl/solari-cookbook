"""POST /executions — quota, run, persist, refund on infra failure."""

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session as DBSession

from app.dependencies import (
    ApiError,
    get_current_api_key,
    get_db,
    get_runner,
)
from app.models import ApiKey, Execution, utcnow
from app.schemas import ExecutionRequest, ExecutionResponse
from app.services import sessions as sessions_service
from app.services.errors import SolariError
from app.services.quota import InsufficientCredits, consume_credit, refund_credit
from app.services.solari_runner import SolariRunner

logger = logging.getLogger("exekit.executions")
router = APIRouter()

MAX_CODE_LENGTH = 50_000

# Infra-class outcomes: HTTP 503 + the credit comes back (spec §G). User-code
# outcomes (completed/failed/timeout) are 200 with the status in the body.
_REFUNDABLE_STATUSES = {"solari_unconfigured", "solari_unavailable"}


@router.post("/executions", response_model=ExecutionResponse)
async def create_execution(
    body: ExecutionRequest,
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
    runner: SolariRunner = Depends(get_runner),
) -> ExecutionResponse:
    code = body.code
    if not code.strip():
        raise ApiError(400, "empty_code", "code must not be empty.")
    if len(code) > MAX_CODE_LENGTH:
        raise ApiError(400, "code_too_long", f"code must be at most {MAX_CODE_LENGTH} characters.")

    session_row = None
    if body.session_id is not None:
        # Ownership + liveness first, so invalid sessions never cost a credit.
        session_row = sessions_service.get_session_for_api_key(
            db, body.session_id, api_key.id
        )
        if session_row is None:
            raise ApiError(404, "invalid_session", f"No session {body.session_id} for this API key.")
        if session_row.status != "active":
            raise ApiError(410, "invalid_session", f"Session {body.session_id} is {session_row.status}.")

    # Debit before any Solari work; infra failures below refund.
    try:
        consume_credit(db, api_key)
    except InsufficientCredits:
        raise ApiError(
            402,
            "insufficient_credits",
            "You need more credits to run executions.",
            details={"credits_remaining": api_key.credits},
        )

    try:
        result = await runner.run_python(code, session_id=session_row.solari_session_id if session_row else None)
    except KeyError:
        # The runner holds handles in memory; a restart or reaping loses them.
        # The session is unrecoverable for this process — mark and refund.
        if session_row is not None:
            sessions_service.mark_session_killed(db, session_row, status="lost")
        refund_credit(db, api_key)
        db.commit()
        raise ApiError(
            410,
            "invalid_session",
            "Session is no longer available; create a new session.",
        )
    except SolariError as exc:  # defensive: run_python maps its own errors
        refund_credit(db, api_key)
        db.commit()
        raise ApiError(503, "solari_unavailable", str(exc))

    if result.status in _REFUNDABLE_STATUSES:
        refund_credit(db, api_key)

    execution = Execution(
        api_key_id=api_key.id,
        session_id=body.session_id,
        code=code,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        status=result.status,
        error=result.error,
        created_at=utcnow(),
        finished_at=utcnow(),
    )
    db.add(execution)
    api_key.executions_count += 1
    db.add(api_key)
    db.commit()
    db.refresh(execution)
    db.refresh(api_key)

    if result.status in _REFUNDABLE_STATUSES:
        raise ApiError(
            503,
            result.status,
            result.error or "Solari infrastructure is unavailable.",
            details={"execution_id": execution.id, "credits_remaining": api_key.credits},
        )

    logger.info(
        "execution %d finished status=%s key_last4=%s",
        execution.id, result.status, api_key.key_last4,
    )
    return ExecutionResponse(
        execution_id=execution.id,
        session_id=body.session_id,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        status=result.status,
        error=result.error,
        artifacts=[a.model_dump() for a in result.artifacts],
        credits_remaining=api_key.credits,
    )
