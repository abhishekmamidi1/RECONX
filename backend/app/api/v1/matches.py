from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import SessionDep
from app.models import AuditLog, Match, MatchParticipant, Transaction
from app.schemas import MatchOut, ParticipantOut, ResolvedMatchOut, ResolvedMatchesResponse, ResolvedParticipant

router = APIRouter(prefix="/matches", tags=["matches"])

_HUMAN_ACTIONS = {
    "match.approved": "approved",
    "match.manual_created": "manually matched",
    "match.rejected": "rejected",
}
_HUMAN_ACTIONS_BY_LABEL = {label: key for key, label in _HUMAN_ACTIONS.items()}


@router.get("", response_model=list[MatchOut])
async def list_matches(
    db: SessionDep,
    status: Annotated[str | None, Query(pattern="^(proposed|confirmed|rejected)$")] = None,
    match_type: Annotated[
        str | None, Query(pattern="^(deterministic|fuzzy|semantic|ai|manual)$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[MatchOut]:
    stmt = select(Match).order_by(Match.proposed_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Match.status == status)
    if match_type:
        stmt = stmt.where(Match.match_type == match_type)
    matches = (await db.execute(stmt)).scalars().unique().all()

    participant_rows = await db.execute(
        select(MatchParticipant, Transaction)
        .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
        .where(MatchParticipant.match_id.in_([m.id for m in matches]))
    )
    by_match: dict = {}
    for participant, txn in participant_rows.all():
        by_match.setdefault(participant.match_id, []).append(
            ParticipantOut(
                external_ref=txn.external_ref,
                source=txn.source,
                role=participant.role,
                amount=txn.amount,
            )
        )

    return [
        MatchOut(
            id=match.id,
            match_type=match.match_type,
            confidence_score=match.confidence_score,
            status=match.status,
            resolved_by=match.resolved_by,
            decided_by=match.decided_by,
            rationale=match.rationale,
            proposed_at=match.proposed_at,
            resolved_at=match.resolved_at,
            participants=by_match.get(match.id, []),
        )
        for match in matches
    ]


@router.get("/resolved", response_model=ResolvedMatchesResponse)
async def list_resolved_matches(
    db: SessionDep,
    resolution: Annotated[str, Query(pattern="^(auto|human)$")] = "auto",
    match_type: Annotated[
        str | None, Query(pattern="^(deterministic|fuzzy|semantic|ai|manual|batch)$")
    ] = None,
    action: Annotated[str | None, Query(pattern="^(approved|manually matched|rejected)$")] = None,
    actor: str | None = None,
    sort_by: str = Query("resolved_at", pattern="^(resolved_at|proposed_at|confidence_score)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: Annotated[int, Query(ge=1, le=250)] = 100,
) -> ResolvedMatchesResponse:
    auto_total = await db.scalar(
        select(func.count())
        .select_from(Match)
        .where(Match.status == "confirmed", Match.resolved_by == "auto")
    ) or 0
    human_total = await db.scalar(
        select(func.count())
        .select_from(Match)
        .where(Match.resolved_by == "human", Match.status.in_(["confirmed", "rejected"]))
    ) or 0

    if resolution == "auto":
        stmt = select(Match).where(
            Match.status == "confirmed", Match.resolved_by == "auto"
        )
    else:
        stmt = select(Match).where(
            Match.resolved_by == "human", Match.status.in_(["confirmed", "rejected"])
        )
    if match_type:
        stmt = stmt.where(Match.match_type == match_type)
    if resolution == "human" and action:
        stmt = stmt.where(
            Match.id.in_(select(AuditLog.entity_id).where(
                AuditLog.entity_type == "match",
                AuditLog.action == _HUMAN_ACTIONS_BY_LABEL[action],
            ))
        )
    if resolution == "human" and actor:
        stmt = stmt.where(Match.decided_by == actor)

    order_column = {
        "resolved_at": Match.resolved_at,
        "proposed_at": Match.proposed_at,
        "confidence_score": Match.confidence_score,
    }[sort_by]
    stmt = stmt.order_by(
        order_column.desc() if order == "desc" else order_column.asc(), Match.id
    ).limit(limit)
    matches = (await db.execute(stmt)).scalars().unique().all()

    rows = await db.execute(
        select(MatchParticipant, Transaction)
        .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
        .where(MatchParticipant.match_id.in_([m.id for m in matches]))
    )
    participants_by_match: dict = {}
    for participant, txn in rows.all():
        participants_by_match.setdefault(participant.match_id, []).append(
            ResolvedParticipant(
                transaction_id=txn.id,
                external_ref=txn.external_ref,
                source=txn.source,
                role=participant.role,
                amount=txn.amount,
            )
        )

    resolved_by: dict = {}
    if resolution == "human" and matches:
        audit_stmt = (
            select(AuditLog, Match.id)
            .where(
                AuditLog.entity_type == "match",
                AuditLog.entity_id.in_([m.id for m in matches]),
            )
            .where(Match.id == AuditLog.entity_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
        rows = await db.execute(audit_stmt)
        for audit, _ in rows.all():
            if audit.entity_id not in resolved_by and audit.action in _HUMAN_ACTIONS:
                resolved_by[audit.entity_id] = audit

    items: list[ResolvedMatchOut] = []
    for match in matches:
        human_audit = resolved_by.get(match.id)
        items.append(
            ResolvedMatchOut(
                match_id=match.id,
                match_type=match.match_type,
                status=match.status,
                confidence_score=str(match.confidence_score),
                resolved_by=match.resolved_by,
                decided_by=match.decided_by,
                rationale=match.rationale,
                proposed_at=match.proposed_at,
                resolved_at=match.resolved_at,
                participants=participants_by_match.get(match.id, []),
                stage=match.match_type if resolution == "auto" else None,
                actor=match.decided_by if resolution == "human" else None,
                action=_HUMAN_ACTIONS[human_audit.action] if human_audit else None,
                note=(human_audit.details or {}).get("note") if human_audit else None,
            )
        )

    return ResolvedMatchesResponse(
        resolution=resolution,
        auto=int(auto_total),
        human=int(human_total),
        items=items,
    )
