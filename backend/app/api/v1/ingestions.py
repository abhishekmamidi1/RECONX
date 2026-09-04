from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.models import Ingestion
from app.schemas import IngestionOut

router = APIRouter(prefix="/ingestions", tags=["ingestions"])


@router.get("", response_model=list[IngestionOut])
async def list_ingestions(
    db: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IngestionOut]:
    result = await db.execute(
        select(Ingestion).order_by(Ingestion.created_at.desc()).limit(limit)
    )
    return [IngestionOut.model_validate(i) for i in result.scalars().all()]
