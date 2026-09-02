"""Execution history and artifact endpoints (all require X-API-Key).

GET /executions                                   — list own executions
GET /executions/{execution_id}                    — detail (code behind include_code)
GET /executions/{execution_id}/artifacts          — artifact metadata list
GET /executions/{execution_id}/artifacts/{aid}/download — stored content

Ownership: every lookup is scoped to the caller's api_key_id and renders as
404 when it does not match, so other users' executions are indistinguishable
from nonexistent ones.
"""

import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from sqlmodel import Session as DBSession, select

from app.dependencies import get_current_api_key, get_db
from app.errors import NotFound
from app.models import ApiKey, Execution, ExecutionArtifact
from app.schemas import (
    ArtifactMeta,
    ExecutionArtifactsResponse,
    ExecutionDetailResponse,
    HistoryExecution,
    HistoryResponse,
)
from app.services.executions import _artifact_meta

logger = logging.getLogger("exekit.history")
router = APIRouter()

# Content-Disposition filenames must stay header-safe; anything odd is dropped.
_SAFE_FILENAME = re.compile(r'^[A-Za-z0-9._ ()\[\]-]+$')
DEFAULT_DOWNLOAD_MIME = "application/octet-stream"


def _get_owned_execution(db: DBSession, execution_id: int, api_key: ApiKey) -> Execution:
    execution = db.get(Execution, execution_id)
    if execution is None or execution.api_key_id != api_key.id:
        raise NotFound("Execution not found.", code="not_found")
    return execution


def _artifact_meta_for(db: DBSession, execution_id: int) -> list[ArtifactMeta]:
    rows = db.exec(
        select(ExecutionArtifact)
        .where(ExecutionArtifact.execution_id == execution_id)
        .order_by(ExecutionArtifact.id)
    ).all()
    return _artifact_meta(list(rows))


@router.get("/executions", response_model=HistoryResponse)
def list_executions(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
) -> HistoryResponse:
    query = select(Execution).where(Execution.api_key_id == api_key.id)
    if status is not None:
        query = query.where(Execution.status == status)
    rows = db.exec(
        query.order_by(Execution.id.desc()).offset(offset).limit(limit)
    ).all()

    counts: dict[int, int] = {}
    for row in rows:
        counts[row.id] = 0
    if rows:
        ids = list(counts)
        artifact_rows = db.exec(
            select(ExecutionArtifact).where(ExecutionArtifact.execution_id.in_(ids))
        ).all()
        for art in artifact_rows:
            counts[art.execution_id] += 1

    return HistoryResponse(
        executions=[
            HistoryExecution(
                id=row.id,
                session_id=row.session_id,
                status=row.status,
                exit_code=row.exit_code,
                created_at=row.created_at,
                finished_at=row.finished_at,
                artifact_count=counts.get(row.id, 0),
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/executions/{execution_id}", response_model=ExecutionDetailResponse)
def get_execution_detail(
    execution_id: int,
    include_code: bool = Query(default=False),
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
) -> ExecutionDetailResponse:
    execution = _get_owned_execution(db, execution_id, api_key)
    duration_ms = None
    if execution.finished_at is not None:
        delta = execution.finished_at - execution.created_at
        duration_ms = max(int(delta.total_seconds() * 1000), 0)
    return ExecutionDetailResponse(
        id=execution.id,
        session_id=execution.session_id,
        status=execution.status,
        exit_code=execution.exit_code,
        stdout=execution.stdout,
        stderr=execution.stderr,
        error=execution.error,
        created_at=execution.created_at,
        finished_at=execution.finished_at,
        code=execution.code if include_code else None,
        duration_ms=duration_ms,
        artifacts=_artifact_meta_for(db, execution.id),
    )


@router.get(
    "/executions/{execution_id}/artifacts",
    response_model=ExecutionArtifactsResponse,
)
def list_execution_artifacts(
    execution_id: int,
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
) -> ExecutionArtifactsResponse:
    execution = _get_owned_execution(db, execution_id, api_key)
    return ExecutionArtifactsResponse(
        execution_id=execution.id,
        artifacts=_artifact_meta_for(db, execution.id),
    )


@router.get("/executions/{execution_id}/artifacts/{artifact_id}/download")
def download_execution_artifact(
    execution_id: int,
    artifact_id: int,
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
) -> Response:
    execution = _get_owned_execution(db, execution_id, api_key)
    artifact = db.get(ExecutionArtifact, artifact_id)
    if artifact is None or artifact.execution_id != execution.id:
        raise NotFound("Artifact not found.", code="not_found")
    if artifact.data_text is None:
        # Never stored (too large or binary) — nothing to download.
        raise NotFound(
            "Artifact content was not stored (too large or binary).",
            code="artifact_not_stored",
        )

    filename = artifact.filename if _SAFE_FILENAME.match(artifact.filename or "") else "artifact.txt"
    content_type = artifact.mime_type or DEFAULT_DOWNLOAD_MIME
    if not content_type.startswith("text/") and content_type != DEFAULT_DOWNLOAD_MIME:
        content_type = DEFAULT_DOWNLOAD_MIME
    logger.info(
        "artifact downloaded id=%d execution=%d bytes=%d",
        artifact.id, execution.id, artifact.size_bytes,
    )
    return Response(
        content=artifact.data_text,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{quote(filename)}"',
            "X-Artifact-Filename": quote(artifact.filename or "artifact.txt"),
        },
    )
