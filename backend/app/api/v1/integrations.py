"""ERP integration endpoints: push resolved results + mock receiver.

The mock receiver is a stub ERP: it accepts the webhook payload, keeps the
last N in memory (dev convenience, not durable), and returns 200. It exists
so the delivery path is testable end-to-end without a real ERP system.
"""

import collections
import datetime as dt
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import ActorDep, SessionDep
from app.schemas.review import AuditEntryOut
from app.services.webhook import WebhookDeliveryError, push_resolved
from sqlalchemy import select

from app.models import AuditLog

router = APIRouter(prefix="/integrations/erp", tags=["integrations"])

# In-memory ring buffer for the mock ERP receiver (per-process).
_MOCK_RECEIVED: collections.deque = collections.deque(maxlen=25)


class PushRequest(BaseModel):
    url: str | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None


@router.post("/push")
async def push_to_erp(
    db: SessionDep,
    actor: ActorDep,
    payload: PushRequest | None = None,
) -> JSONResponse:
    body = payload or PushRequest()
    try:
        result = await push_resolved(
            db,
            actor=actor,
            url=body.url,
            since=body.since,
            until=body.until,
        )
    except WebhookDeliveryError as exc:
        await db.commit()  # persist the webhook.failed audit row before returning
        return JSONResponse(status_code=502, content={"ok": False, "detail": str(exc)})
    await db.commit()
    return JSONResponse(content={"ok": True, **result})


@router.get("/deliveries", response_model=list[AuditEntryOut])
async def deliveries(
    db: SessionDep,
    limit: int = Query(20, ge=1, le=100),
) -> list[AuditEntryOut]:
    rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "webhook")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        AuditEntryOut(
            id=row.id,
            actor=row.actor,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=None,
            before_state=None,
            after_state=row.after_state,
            details=row.details,
            request_id=row.request_id,
            created_at=row.created_at,
        )
        for row in rows
    ]


# ── Mock ERP receiver ────────────────────────────────────────
mock_router = APIRouter(prefix="/mock/erp", tags=["mock"])


@mock_router.post("/webhook")
async def mock_webhook(request: Request) -> JSONResponse:
    raw = await request.body()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw.decode(errors="replace")[:2000]}
    entry = {
        "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "actor": request.headers.get("X-Actor"),
        "payload": parsed,
    }
    _MOCK_RECEIVED.appendleft(entry)
    return JSONResponse(
        content={
            "received": True,
            "items": len(parsed.get("items", [])) if isinstance(parsed, dict) else 0,
            "stored_total": len(_MOCK_RECEIVED),
        }
    )


@mock_router.get("/webhook/received")
async def mock_received(limit: int = Query(10, ge=1, le=25)) -> dict:
    return {"count": len(_MOCK_RECEIVED), "entries": list(_MOCK_RECEIVED)[:limit]}


@mock_router.delete("/webhook/received")
async def mock_clear() -> dict:
    _MOCK_RECEIVED.clear()
    return {"cleared": True}
