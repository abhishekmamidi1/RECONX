from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.models import Transaction
from app.schemas import SourceEnum, TransactionOut, TransactionPage

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=TransactionPage)
async def list_transactions(
    db: SessionDep,
    source: SourceEnum | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TransactionPage:
    filters = []
    if source is not None:
        filters.append(Transaction.source == source.value)

    count_stmt = select(func.count()).select_from(Transaction)
    items_stmt = (
        select(Transaction)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    for condition in filters:
        count_stmt = count_stmt.where(condition)
        items_stmt = items_stmt.where(condition)

    total = (await db.execute(count_stmt)).scalar_one()
    transactions = (await db.execute(items_stmt)).scalars().all()
    return TransactionPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[TransactionOut.model_validate(t) for t in transactions],
    )
