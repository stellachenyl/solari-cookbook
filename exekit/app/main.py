"""ExecKit — API and playground for executing AI-generated Python in Solari sandboxes."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import create_all
from app.dependencies import get_runner
from app.errors import ExecKitError
from app.logging_config import setup_logging
from app.middleware.request_id import RequestIDMiddleware
from app.schemas import ErrorDetail, ErrorResponse
from app.routes import admin, billing, executions, health, history, keys, sessions

setup_logging()
logger = logging.getLogger("exekit")
settings = get_settings()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_all()
    if not settings.debug and settings.admin_token == "change-me":
        logger.warning("ADMIN_TOKEN is still the default; set a real token before deploying.")

    # Idle-session cleanup loop; cancelled on shutdown. Must not crash the
    # app when Solari is unconfigured — cleanup_once handles its own errors.
    from app.services.session_cleanup import cleanup_loop

    cleanup_task = asyncio.create_task(
        cleanup_loop(
            get_runner(),
            interval_seconds=get_settings().session_cleanup_interval_seconds,
        )
    )
    logger.info("session cleanup scheduled every %ds",
                get_settings().session_cleanup_interval_seconds)
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("shutdown complete")


app = FastAPI(
    title="ExecKit",
    description="Execute AI-generated Python code safely in Solari sandboxes.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Development only: any localhost/127.0.0.1 port.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestIDMiddleware)


def get_runner():
    from app.dependencies import get_runner as _get_runner

    return _get_runner()


app.include_router(health.router)
app.include_router(keys.router)
app.include_router(executions.router)
app.include_router(history.router)
app.include_router(sessions.router)
app.include_router(admin.router)
app.include_router(billing.router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _error_response(request: Request, status_code: int, code: str, message: str,
                    details: dict | None = None) -> JSONResponse:
    payload = dict(details or {})
    payload.setdefault("request_id", request.state.request_id)
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error=ErrorDetail(code=code, message=message, details=payload)
        ).model_dump(),
    )


@app.exception_handler(ExecKitError)
async def domain_error_handler(request: Request, exc: ExecKitError) -> JSONResponse:
    logger.warning("domain error path=%s code=%s status=%d: %s",
                   request.url.path, exc.code, exc.http_status, exc.message)
    return _error_response(request, exc.http_status, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Malformed bodies (e.g. non-string code) become the standard envelope.
    return _error_response(
        request,
        422,
        "invalid_request",
        "Request body failed validation.",
        {"errors": [
            {"loc": [str(x) for x in err.get("loc", [])],
             "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Route-level HTTPExceptions (404 on unknown paths, 405, ...) use the
    # same envelope; the plain detail string becomes the message.
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error_response(request, exc.status_code, code, str(exc.detail))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never expose stack traces in the response; log them server-side always,
    # and only echo the exception message when DEBUG=true.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    message = "Internal server error"
    if get_settings().debug:
        message = f"{type(exc).__name__}: {exc}"
    return _error_response(request, 500, "internal", message)


@app.middleware("http")
async def _no_store_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response
