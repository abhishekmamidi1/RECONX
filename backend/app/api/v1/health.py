from time import perf_counter

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import SessionDep
from app.core.config import get_settings
from app.schemas import AIHealth, DatabaseHealth, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(db: SessionDep) -> HealthResponse:
    settings = get_settings()
    started = perf_counter()
    error: str | None = None
    try:
        await db.execute(text("SELECT 1"))
        connected = True
    except Exception as exc:
        connected = False
        error = str(exc)[:300]
    latency_ms = (perf_counter() - started) * 1000 if connected else None
    return HealthResponse(
        status="ok" if connected else "degraded",
        app_version=settings.app_version,
        env=settings.env,
        database=DatabaseHealth(
            connected=connected, latency_ms=round(latency_ms, 2) if latency_ms else None, error=error
        ),
        ai=AIHealth(
            reasoning_provider=settings.reasoning_provider,
            reasoning_model=settings.reasoning_model,
            reasoning_mode="live" if settings.reasoning_provider == "ollama" else "fallback",
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
        ),
    )
