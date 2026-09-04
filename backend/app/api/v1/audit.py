from typing import Annotated

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.models import AuditLog
from app.schemas.review import AuditEntryOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditEntryOut])
async def list_audit(
    db: SessionDep,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: uuid.UUID | None = None,
    action: str | None = None,
    actor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[AuditEntryOut]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    return [
        AuditEntryOut(
            id=row.id,
            actor=row.actor,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            before_state=row.before_state,
            after_state=row.after_state,
            details=row.details,
            request_id=row.request_id,
            created_at=row.created_at,
        )
        for row in (await db.execute(stmt)).scalars()
    ]
