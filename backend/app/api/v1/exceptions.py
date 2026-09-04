from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.models import ExceptionRecord, Transaction
from app.schemas import ExceptionOut

router = APIRouter(prefix="/exceptions", tags=["exceptions"])


@router.get("", response_model=list[ExceptionOut])
async def list_exceptions(
    db: SessionDep,
    status: Annotated[str | None, Query(pattern="^(open|in_review|escalated|resolved|dismissed)$")] = None,
    exception_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ExceptionOut]:
    stmt = (
        select(ExceptionRecord, Transaction.external_ref)
        .join(Transaction, Transaction.id == ExceptionRecord.transaction_id, isouter=True)
        .order_by(ExceptionRecord.opened_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(ExceptionRecord.status == status)
    if exception_type:
        stmt = stmt.where(ExceptionRecord.exception_type == exception_type)

    rows = (await db.execute(stmt)).all()
    return [
        ExceptionOut(
            id=exception.id,
            transaction_id=exception.transaction_id,
            transaction_ref=ref,
            exception_type=exception.exception_type,
            priority=exception.priority,
            amount_impact=exception.amount_impact,
            status=exception.status,
            opened_at=exception.opened_at,
            resolution_note=exception.resolution_note,
        )
        for exception, ref in rows
    ]
