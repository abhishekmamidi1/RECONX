import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.api.v1 import integrations
from app.core.config import get_settings
from app.core.logging import configure_logging, request_id_var
from app.db.session import engine

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting %s v%s (env=%s)", settings.app_name, settings.app_version, settings.env)
    yield
    await engine.dispose()
    logger.info("shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable]
):
    request_id = uuid.uuid4().hex[:12]
    token = request_id_var.set(request_id)
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        raise
    finally:
        elapsed_ms = (perf_counter() - started) * 1000
        request_id_var.reset(token)
    response.headers["X-Request-Id"] = request_id
    logger.info(
        "%s %s -> %d (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = request_id_var.get()
    logger.error("internal error [rid=%s]: %s", rid, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error", "request_id": rid},
    )


app.include_router(api_router)
app.include_router(integrations.mock_router)


@app.get("/healthz", tags=["ops"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readiness() -> dict[str, str]:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ready"}
