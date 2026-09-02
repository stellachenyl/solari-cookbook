"""Request-ID middleware: trace every request end to end.

- Honors a client-provided X-Request-ID (sanitized), otherwise generates one.
- Attaches the id to request.state and to the logging context (contextvars),
  so every log line emitted while handling the request carries it.
- Echoes the id back on the response.
"""

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.logging_config import request_id_var, route_var  # single source of truth

logger = logging.getLogger("exekit.request")

_MAX_REQUEST_ID_LEN = 64


def _sanitize(raw: str | None) -> str | None:
    """Accept only sane client-supplied ids; never echo arbitrary bytes."""
    if not raw:
        return None
    cleaned = raw.strip()[:_MAX_REQUEST_ID_LEN]
    if cleaned and all(c.isalnum() or c in "-_." for c in cleaned):
        return cleaned
    return None


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = _sanitize(request.headers.get("X-Request-ID")) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        rid_token = request_id_var.set(request_id)
        path = request.url.path
        logger.info("request started %s %s", request.method, path)
        try:
            response = await call_next(request)
            route_template = getattr(request.scope.get("route"), "path", None)
            if route_template:
                route_var.set(route_template)
            response.headers["X-Request-ID"] = request_id
            logger.info("request finished %s %s status=%d", request.method, path, response.status_code)
            return response
        finally:
            request_id_var.reset(rid_token)
