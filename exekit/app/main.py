"""ExecKit — API and playground for executing AI-generated Python in Solari sandboxes."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import create_all
from app.dependencies import ApiError
from app.routes import executions, health, keys, sessions
from app.schemas import ErrorDetail, ErrorResponse

logger = logging.getLogger("exekit")
settings = get_settings()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_all()
    if not settings.debug and settings.admin_token == "change-me":
        logger.warning("ADMIN_TOKEN is still the default; set a real token before deploying.")
    yield


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

app.include_router(health.router)
app.include_router(keys.router)
app.include_router(executions.router)
app.include_router(sessions.router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Malformed bodies (e.g. non-string code) become the standard envelope
    # instead of FastAPI's default {"detail": [...]} shape.
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(
            error=ErrorDetail(
                code="invalid_request",
                message="Request body failed validation.",
                details={
                    "errors": [
                        {"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
                        for err in exc.errors()
                    ]
                },
            )
        ).model_dump(),
    )


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=ErrorDetail(code=exc.code, message=exc.message, details=exc.details)
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never expose stack traces in the response; log them server-side always,
    # and only echo the exception message when DEBUG=true.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    message = "Internal server error"
    if settings.debug:
        message = f"{type(exc).__name__}: {exc}"
    body = ErrorResponse(
        error=ErrorDetail(code="internal", message=message, details={})
    ).model_dump()
    return JSONResponse(status_code=500, content=body)
