import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.api.deps import ActorDep, SessionDep
from app.schemas.review import AuditEntryOut
from app.services.policy_admin import (
    PolicyError,
    coerce_out,
    key_history,
    list_policy,
    update_policy,
)

router = APIRouter(prefix="/policy", tags=["policy"])


@router.get("")
async def get_policy(db: SessionDep) -> list[dict]:
    fields = await list_policy(db)
    for field in fields:
        field["value"] = coerce_out(field["value"])
        if field["last_changed"]:
            field["last_changed"] = {
                **field["last_changed"],
                "before": coerce_out(field["last_changed"]["before"]),
                "after": coerce_out(field["last_changed"]["after"]),
            }
    return fields


@router.patch("/{key}")
async def patch_policy(
    db: SessionDep,
    actor: ActorDep,
    key: str,
    value: Any = Body(..., embed=True),
) -> dict:
    try:
        result = await update_policy(db, key=key, value=value, actor=actor)
    except PolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    result["value"] = coerce_out(result.get("value"))
    result["before"] = coerce_out(result.get("before"))
    return {**result, "actor": actor}


@router.get("/{key}/history", response_model=list[AuditEntryOut])
async def policy_history(
    db: SessionDep,
    key: str,
    limit: int = Query(20, ge=1, le=100),
) -> list[AuditEntryOut]:
    entries = await key_history(db, key, limit)
    return [
        AuditEntryOut(
            id=entry.id,
            actor=entry.actor,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id if entry.entity_id is None else uuid.UUID(str(entry.entity_id)),
            before_state=entry.before_state,
            after_state=entry.after_state,
            details=entry.details,
            request_id=entry.request_id,
            created_at=entry.created_at,
        )
        for entry in entries
    ]
