"""ExecKit — API and playground for executing AI-generated Python in Solari sandboxes."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import create_all
from app.routes.health import router as health_router
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

app.include_router(health_router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
