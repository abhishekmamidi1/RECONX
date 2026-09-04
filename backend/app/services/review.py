"""Human-in-the-loop review actions.

Every mutation here follows the same data-model conventions the
reconciliation pipeline itself uses (matches.status / resolved_by /
decided_by, exceptions.status / assigned_to / resolution_note) so there is
no parallel "human path" in the data. Each action writes an audit_logs row
with actor + before/after state.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExceptionRecord, Match, MatchParticipant, Transaction
from app.services.audit import record_audit
from app.services.policy import load_policy
from app.services.reconciliation.engine import (
    _PIPELINE_EXCEPTION_TYPES,
    _priority_for,
)


class ReviewError(Exception):
    """Raised when a review action violates workflow state rules."""


def _match_snapshot(match: Match, refs: list[str]) -> dict:
    return {
        "status": match.status,
        "resolved_by": match.resolved_by,
        "decided_by": match.decided_by,
        "match_type": match.match_type,
        "confidence_score": float(match.confidence_score),
        "members": refs,
    }


async def _participant_refs(db: AsyncSession, match_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(Transaction.external_ref)
        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
        .where(MatchParticipant.match_id == match_id)
        .order_by(Transaction.source, Transaction.external_ref)
    )
    return [ref for (ref,) in rows.all()]


async def _resolve_linked_exceptions(
    db: AsyncSession,
    txn_ids: list[uuid.UUID],
    actor: str,
    note: str,
) -> int:
    if not txn_ids:
        return 0
    rows = await db.execute(
        select(ExceptionRecord).where(
            ExceptionRecord.transaction_id.in_(txn_ids),
            ExceptionRecord.exception_type.in_(_PIPELINE_EXCEPTION_TYPES),
            ExceptionRecord.status.in_(["open", "in_review", "escalated"]),
        )
    )
    resolved = 0
    now = dt.datetime.now(dt.timezone.utc)
    for exception in rows.scalars():
        before = {"status": exception.status}
        exception.status = "resolved"
        exception.resolved_at = now
        exception.assigned_to = actor
        exception.resolution_note = note or "resolved by human decision on related match"
        await record_audit(
            db,
            actor=actor,
            action="exception.resolved",
            entity_type="exception",
            entity_id=exception.id,
            before_state=before,
            after_state={"status": "resolved", "resolution_note": exception.resolution_note},
            details={"note": note},
        )
        resolved += 1
    return resolved


async def _load_proposed_match(db: AsyncSession, match_id: uuid.UUID) -> Match:
    match = await db.get(Match, match_id)
    if match is None:
        raise ReviewError(f"match {match_id} not found")
    if match.status != "proposed":
        raise ReviewError(
            f"match {match_id} is '{match.status}', only proposed matches can be decided"
        )
    return match


async def approve_match(
    db: AsyncSession, *, match_id: uuid.UUID, actor: str, note: str = ""
) -> tuple[Match, int]:
    match = await _load_proposed_match(db, match_id)
    refs = await _participant_refs(db, match.id)
    before = _match_snapshot(match, refs)

    match.status = "confirmed"
    match.resolved_by = "human"
    match.decided_by = actor
    match.resolved_at = dt.datetime.now(dt.timezone.utc)

    txn_ids = [
        row[0]
        for row in (
            await db.execute(
                select(MatchParticipant.transaction_id).where(
                    MatchParticipant.match_id == match.id
                )
            )
        ).all()
    ]
    resolved_count = await _resolve_linked_exceptions(db, txn_ids, actor, note or f"approved proposal {match.id}")

    await db.flush()
    await record_audit(
        db,
        actor=actor,
        action="match.approved",
        entity_type="match",
        entity_id=match.id,
        before_state=before,
        after_state=_match_snapshot(match, refs),
        details={"note": note, "resolved_exception_ids_affected": resolved_count},
    )
    return match, resolved_count


async def reject_match(
    db: AsyncSession, *, match_id: uuid.UUID, actor: str, note: str = ""
) -> Match:
    match = await _load_proposed_match(db, match_id)
    refs = await _participant_refs(db, match.id)
    before = _match_snapshot(match, refs)

    match.status = "rejected"
    match.resolved_by = "human"
    match.decided_by = actor
    match.resolved_at = dt.datetime.now(dt.timezone.utc)

    await db.flush()
    await record_audit(
        db,
        actor=actor,
        action="match.rejected",
        entity_type="match",
        entity_id=match.id,
        before_state=before,
        after_state=_match_snapshot(match, refs),
        details={"note": note},
    )
    return match


async def dismiss_exception(
    db: AsyncSession, *, exception_id: uuid.UUID, actor: str, note: str = ""
) -> ExceptionRecord:
    exception = await db.get(ExceptionRecord, exception_id)
    if exception is None:
        raise ReviewError(f"exception {exception_id} not found")
    if exception.status not in ("open", "in_review", "escalated"):
        raise ReviewError(
            f"exception {exception_id} is '{exception.status}' and cannot be dismissed"
        )
    before = {"status": exception.status}

    exception.status = "dismissed"
    exception.resolved_at = dt.datetime.now(dt.timezone.utc)
    exception.assigned_to = actor
    exception.resolution_note = note or "dismissed as false positive"

    await db.flush()
    await record_audit(
        db,
        actor=actor,
        action="exception.dismissed",
        entity_type="exception",
        entity_id=exception.id,
        before_state=before,
        after_state={
            "status": exception.status,
            "resolution_note": exception.resolution_note,
        },
        details={"note": note},
    )
    return exception


async def escalate_exception(
    db: AsyncSession, *, exception_id: uuid.UUID, actor: str, note: str = ""
) -> ExceptionRecord:
    exception = await db.get(ExceptionRecord, exception_id)
    if exception is None:
        raise ReviewError(f"exception {exception_id} not found")
    if exception.status not in ("open", "in_review"):
        raise ReviewError(
            f"exception {exception_id} is '{exception.status}' and cannot be escalated"
        )
    before = {"status": exception.status}

    exception.status = "escalated"
    exception.assigned_to = actor
    if note:
        exception.resolution_note = note

    await db.flush()
    await record_audit(
        db,
        actor=actor,
        action="exception.escalated",
        entity_type="exception",
        entity_id=exception.id,
        before_state=before,
        after_state={"status": exception.status, "assigned_to": actor},
        details={"note": note},
    )
    return exception


async def _assert_not_confirmed_members(db: AsyncSession, txn_ids: list[uuid.UUID]) -> None:
    rows = await db.execute(
        select(Match.id)
        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
        .where(MatchParticipant.transaction_id.in_(txn_ids), Match.status == "confirmed")
    )
    conflicting = [str(m) for (m,) in rows.all()]
    if conflicting:
        raise ReviewError(
            "transaction(s) already belong to a confirmed match; "
            f"confirmed matches are immutable inputs: {conflicting}"
        )


async def create_manual_match(
    db: AsyncSession,
    *,
    transaction_ids: list[uuid.UUID],
    actor: str,
    note: str = "",
    replace_proposed_match_id: uuid.UUID | None = None,
) -> tuple[Match, int]:
    txns: list[Transaction] = []
    for txn_id in transaction_ids:
        txn = await db.get(Transaction, txn_id)
        if txn is None:
            raise ReviewError(f"transaction {txn_id} not found")
        txns.append(txn)
    await _assert_not_confirmed_members(db, transaction_ids)

    replaced: Match | None = None
    if replace_proposed_match_id is not None:
        replaced = await _load_proposed_match(db, replace_proposed_match_id)

    policy = await load_policy(db)
    ordered = sorted(txns, key=lambda t: (t.source != "razorpay", t.source))
    refs = [t.external_ref for t in ordered]
    now = dt.datetime.now(dt.timezone.utc)

    match = Match(
        match_type="manual",
        confidence_score=Decimal("1.0000"),
        status="confirmed",
        resolved_by="human",
        decided_by=actor,
        rationale=f"manual match by {actor}: {note or 'reviewer decision'} "
        f"linking {len(refs)} records ({', '.join(refs)})",
        policy_snapshot={},
        proposed_at=now,
        resolved_at=now,
    )
    db.add(match)
    await db.flush()

    for index, txn in enumerate(ordered):
        role = "primary" if index == 0 else "participant"
        db.add(MatchParticipant(match_id=match.id, transaction_id=txn.id, role=role))

    if replaced is not None:
        replaced_refs = await _participant_refs(db, replaced.id)
        replaced_before = _match_snapshot(replaced, replaced_refs)
        replaced.status = "rejected"
        replaced.resolved_by = "human"
        replaced.decided_by = actor
        replaced.resolved_at = now
        await record_audit(
            db,
            actor=actor,
            action="match.rejected",
            entity_type="match",
            entity_id=replaced.id,
            before_state=replaced_before,
            after_state=_match_snapshot(replaced, replaced_refs),
            details={"note": f"superseded by manual match {match.id}", **{"superseded_by": str(match.id)}},
        )

    resolved_count = await _resolve_linked_exceptions(
        db, [t.id for t in txns], actor, note or f"manually matched into {match.id}"
    )

    await db.flush()
    await record_audit(
        db,
        actor=actor,
        action="match.manual_created",
        entity_type="match",
        entity_id=match.id,
        after_state=_match_snapshot(match, refs),
        details={
            "note": note,
            "transaction_ids": [str(t) for t in transaction_ids],
            "replaced_proposed_match_id": str(replace_proposed_match_id) if replace_proposed_match_id else None,
            "max_priority": _priority_for(max(t.amount for t in txns), policy),
        },
    )
    return match, resolved_count
