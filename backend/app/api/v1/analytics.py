from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.services.analytics import overview

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/overview")
async def analytics_overview(
    db: SessionDep,
    from_: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict:
    return await overview(db, from_str=from_, to_str=to)
