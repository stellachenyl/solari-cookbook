"""Thin routes: POST /keys/request, GET /keys/me."""

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session as DBSession

from app.dependencies import get_current_api_key, get_db, require_request_rate_limit
from app.models import ApiKey
from app.schemas import KeyInfoResponse, KeyRequest, KeyResponse
from app.services import api_keys as api_keys_service

logger = logging.getLogger("exekit.keys")
router = APIRouter()


@router.post("/keys/request", response_model=KeyResponse)
def request_key(body: KeyRequest, db: DBSession = Depends(get_db)) -> KeyResponse:
    """Issue a new API key. The raw key appears exactly once, in this
    response; only its SHA-256 hash is stored (spec §E)."""
    api_key, raw_key = api_keys_service.create_api_key(db, email=body.email)
    logger.info("api key issued last4=%s plan=%s", api_key.key_last4, api_key.plan)
    return KeyResponse(
        api_key=raw_key,
        key_last4=api_key.key_last4,
        credits=api_key.credits,
        plan=api_key.plan,
    )


@router.get("/keys/me", response_model=KeyInfoResponse)
def key_me(
    api_key: ApiKey = Depends(get_current_api_key),
    _rl=Depends(require_request_rate_limit),
) -> KeyInfoResponse:
    return KeyInfoResponse(
        key_last4=api_key.key_last4,
        email=api_key.email,
        plan=api_key.plan,
        credits=api_key.credits,
        executions_count=api_key.executions_count,
        is_active=api_key.is_active,
    )
