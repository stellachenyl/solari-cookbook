"""Execution business logic (extracted from routes/executions.py).

The route validates the request shape (Pydantic) and applies rate limits;
everything else about running an execution lives here:

    validate code -> ownership-check session -> consume credit ->
    SolariRunner.run_python -> persist Execution -> increment
    executions_count -> refund on Solari infra failures -> response.

Failures raise domain errors (app/errors.py) that main.py renders as the
standard error envelope. No HTTP primitives in this module.
"""

import logging

from sqlmodel import Session as DBSession

from app.config import get_settings
from app.dependencies import get_runner
from app.errors import ExecKitError, InsufficientCredits, InvalidSession, SessionKilled, SolariNotConfigured, SolariUnavailable
from app.models import ApiKey, Execution, SandboxSession, utcnow
from app.schemas import ExecutionResponse
from app.services import sessions as sessions_service
from app.services.quota import consume_credit, refund_credit
from app.services.solari_runner import SolariRunner

logger = logging.getLogger("exekit.executions")

MAX_CODE_LENGTH = 50_000

# Solari infrastructure outcomes: the credit is refunded and the client gets
# the matching domain error. User-code outcomes (completed/failed/timeout)
# are returned as normal responses with the status in the body.
_REFUNDABLE_STATUSES: dict[str, type[ExecKitError]] = {
    "solari_unconfigured": SolariNotConfigured,
    "solari_unavailable": SolariUnavailable,
}


def validate_code(code: str) -> None:
    """Admission rules for submitted code. Pydantic already enforces the
    string type and max length; these are the service-level guarantees."""
    if not code or not code.strip():
        raise ExecKitError("code must not be empty.", code="empty_code", http_status=400)
    if len(code) > MAX_CODE_LENGTH:
        raise ExecKitError(
            f"code must be at most {MAX_CODE_LENGTH} characters.",
            code="code_too_long", http_status=400,
        )


def _resolve_session(
    db: DBSession, api_key: ApiKey, session_id: str | None
) -> SandboxSession | None:
    if session_id is None:
        return None
    row = sessions_service.get_session_for_api_key(db, session_id, api_key.id)
    if row is None:
        raise InvalidSession(f"No session {session_id} for this API key.")
    if row.status != "active":
        raise SessionKilled(f"Session {session_id} is {session_row_status(row)}.")
    return row


def session_row_status(row: SandboxSession) -> str:
    return row.status


async def execute_code(
    db: DBSession,
    api_key: ApiKey,
    code: str,
    session_id: str | None,
    runner: SolariRunner | None = None,
) -> ExecutionResponse:
    """Run `code` for `api_key`, bill it, and return the API response.

    Billing order: the credit is consumed before any Solari work and refunded
    when the outcome is Solari infrastructure failure (never for user-code
    failures). Every admitted run persists an Execution row and increments
    executions_count, including infra-failed ones.
    """
    runner = runner or get_runner()
    settings = get_settings()
    validate_code(code)

    session_row = _resolve_session(db, api_key, session_id)

    logger.info(
        "execution started key_last4=%s mode=%s code_chars=%d",
        api_key.key_last4,
        "session" if session_row else "one-shot",
        len(code),
    )

    try:
        consume_credit(db, api_key)
    except InsufficientCredits as exc:
        raise InsufficientCredits(
            message=exc.message,
            details={"credits_remaining": 0},
        )
    try:
        result = await runner.run_python(
            code, session_id=session_row.solari_session_id if session_row else None
        )
    except KeyError:
        # The runner holds handles in memory; a restart or reaping loses them.
        # The session is unrecoverable for this process — mark and refund.
        if session_row is not None:
            sessions_service.mark_session_killed(db, session_row, status="lost")
        refund_credit(db, api_key)
        db.commit()
        raise SessionKilled("Session is no longer available; create a new session.")

    refund_error = _REFUNDABLE_STATUSES.get(result.status)
    if refund_error is not None:
        refund_credit(db, api_key)

    execution = Execution(
        api_key_id=api_key.id,
        session_id=session_id,
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

    if refund_error is not None:
        logger.warning(
            "execution %d failed status=%s key_last4=%s",
            execution.id, result.status, api_key.key_last4,
        )
        raise refund_error(
            result.error or "Solari infrastructure is unavailable.",
            details={
                "execution_id": execution.id,
                "credits_remaining": api_key.credits,
            },
        )

    logger.info(
        "execution finished id=%d status=%s key_last4=%s credits_left=%d",
        execution.id, result.status, api_key.key_last4, api_key.credits,
    )
    return ExecutionResponse(
        execution_id=execution.id,
        session_id=session_id,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        status=result.status,
        error=result.error,
        artifacts=[a.model_dump() for a in result.artifacts],
        credits_remaining=api_key.credits,
        truncated=result.truncated,
    )
