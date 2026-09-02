"""Structured logging for ExecKit.

setup_logging() installs a log-record factory that stamps every record with
`request_id` and `route` from the request middleware's contextvars ("-" when
outside a request), and a single stdout handler with a matching format.

Redaction rules: call sites must never pass full API keys, code payloads, or
secrets into log arguments — log last4 / character counts instead.
"""

import logging
import sys
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
route_var: ContextVar[str] = ContextVar("route", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s rid=%(request_id)s route=%(route)s"

_configured = False


def _record_factory(*args, **kwargs):
    record = _original_factory(*args, **kwargs)
    record.request_id = request_id_var.get("-")
    record.route = route_var.get("-")
    return record


def setup_logging(level: int = logging.INFO) -> None:
    """Idempotent: safe to call from module import and tests."""
    global _configured
    if _configured:
        return
    _configured = True

    global _original_factory
    _original_factory = logging.getLogRecordFactory()
    logging.setLogRecordFactory(_record_factory)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    # uvicorn's default handlers would double-print our app logs
    for noisy in ("uvicorn.access", "uvicorn", "uvicorn.error"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True
