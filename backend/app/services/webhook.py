"""ERP webhook delivery.

Pushes resolved reconciliation results to the ERP's webhook endpoint.
Deliveries — successful or failed — are always written to audit_logs so a
dropped notification is never silent. Retries use exponential backoff
within one invocation; a persistent failure is recorded with the error and
attempt count.

The target URL comes from settings (ERP_WEBHOOK_URL). Tests and callers may
override it per-request.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ExceptionRecord, Match, MatchParticipant, Transaction
from app.services.audit import record_audit


class WebhookDeliveryError(Exception):
    pass


def _resolved_payload_rows(
    matches: list[Match],
    exceptions: list[ExceptionRecord],
    participants: dict[uuid.UUID, list[tuple[str, str, str]]],
) -> list[dict]:
    items: list[dict] = []
    for match in matches:
        members = participants.get(match.id, [])
        items.append(
            {
                "kind": "match",
                "match_id": str(match.id),
                "match_type": match.match_type,
                "status": match.status,
                "resolution_type": match.resolved_by or ("auto" if match.status == "confirmed" else None),
                "decided_by": match.decided_by,
                "confidence": float(match.confidence_score),
                "resolved_at": match.resolved_at.isoformat() if match.resolved_at else None,
                "rationale": match.rationale,
                "transactions": [
                    {"external_ref": ref, "source": source, "direction": direction}
                    for source, ref, direction in members
                ],
            }
        )
    for exc in exceptions:
        items.append(
            {
                "kind": "exception",
                "exception_id": str(exc.id),
                "exception_type": exc.exception_type,
                "status": exc.status,
                "resolution_type": "human" if exc.status in ("resolved", "dismissed") else None,
                "decided_by": exc.assigned_to,
                "resolved_at": exc.resolved_at.isoformat() if exc.resolved_at else None,
                "resolution_note": exc.resolution_note,
            }
        )
    return items


async def collect_resolved(
    db: AsyncSession,
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    match_ids: list[uuid.UUID] | None = None,
) -> list[dict]:
    """Assemble resolved results (confirmed/rejected matches + closed exceptions)."""
    stmt = select(Match).where(Match.status.in_(["confirmed", "rejected"]))
    if match_ids:
        stmt = stmt.where(Match.id.in_(match_ids))
    if since:
        stmt = stmt.where(Match.resolved_at >= since)
    if until:
        stmt = stmt.where(Match.resolved_at <= until)
    stmt = stmt.order_by(Match.resolved_at.desc()).limit(500)
    matches = list((await db.execute(stmt)).scalars().all())

    participants: dict[uuid.UUID, list[tuple[str, str, str]]] = {}
    if matches:
        rows = await db.execute(
            select(
                MatchParticipant.match_id,
                Transaction.source,
                Transaction.external_ref,
                Transaction.direction,
            )
            .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
            .where(MatchParticipant.match_id.in_([m.id for m in matches]))
        )
        for match_id, source, ref, direction in rows.all():
            participants.setdefault(match_id, []).append((source, ref, direction))

    exc_stmt = select(ExceptionRecord).where(ExceptionRecord.status.in_(["resolved", "dismissed"]))
    if since:
        exc_stmt = exc_stmt.where(ExceptionRecord.resolved_at >= since)
    if until:
        exc_stmt = exc_stmt.where(ExceptionRecord.resolved_at <= until)
    exc_stmt = exc_stmt.order_by(ExceptionRecord.resolved_at.desc()).limit(500)
    exceptions = list((await db.execute(exc_stmt)).scalars().all())

    payload_rows = _resolved_payload_rows(matches, exceptions, participants)
    # Exceptions reference no transactions directly here; keep payload compact.
    return [row for row in payload_rows]


async def deliver(
    db: AsyncSession,
    *,
    url: str,
    payload: dict,
    actor: str,
    max_attempts: int = 3,
    backoff_base_s: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """POST the payload with retries + exponential backoff; audit the outcome."""
    attempt_errors: list[str] = []
    status_code: int | None = None

    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(url, json=payload, headers={"X-Actor": actor})
                status_code = response.status_code
                if 200 <= response.status_code < 300:
                    await record_audit(
                        db,
                        actor=actor,
                        action="webhook.delivered",
                        entity_type="webhook",
                        entity_id=None,
                        after_state={"url": url, "status_code": status_code},
                        details={
                            "url": url,
                            "attempts": attempt,
                            "batch_size": len(payload.get("items", [])),
                            "errors_before_success": attempt_errors or None,
                        },
                    )
                    return {
                        "ok": True,
                        "url": url,
                        "status_code": status_code,
                        "attempts": attempt,
                    }
                attempt_errors.append(f"HTTP {response.status_code}: {response.text[:200]}")
            except httpx.HTTPError as exc:
                attempt_errors.append(f"{type(exc).__name__}: {exc}")

            if attempt < max_attempts:
                await asyncio.sleep(backoff_base_s * (2 ** (attempt - 1)))

    await record_audit(
        db,
        actor=actor,
        action="webhook.failed",
        entity_type="webhook",
        entity_id=None,
        details={
            "url": url,
            "attempts": max_attempts,
            "errors": attempt_errors,
            "batch_size": len(payload.get("items", [])),
        },
    )
    raise WebhookDeliveryError(
        f"delivery failed after {max_attempts} attempts: {'; '.join(attempt_errors[-1:])}"
    )


async def default_webhook_url() -> str | None:
    url = get_settings().erp_webhook_url
    return url.strip() or None


async def push_resolved(
    db: AsyncSession,
    *,
    actor: str,
    url: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    max_attempts: int = 3,
    backoff_base_s: float = 1.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    target = url or await default_webhook_url()
    if not target:
        raise WebhookDeliveryError(
            "no webhook URL configured — set ERP_WEBHOOK_URL or pass an explicit URL"
        )

    items = await collect_resolved(db, since=since, until=until)
    payload = {
        "event": "reconciliation.results",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    }
    result = await deliver(
        db,
        url=target,
        payload=payload,
        actor=actor,
        max_attempts=max_attempts,
        backoff_base_s=backoff_base_s,
        transport=transport,
    )
    result["pushed_items"] = len(items)
    return result
