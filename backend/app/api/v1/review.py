import datetime as dt
import logging
import uuid
from decimal import Decimal

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from app.api.deps import ActorDep, SessionDep
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    ExceptionRecord,
    Match,
    MatchParticipant,
    Transaction,
)
from app.schemas.review import (
    ActionRequest,
    ActionResponse,
    AnalysisOut,
    BelowThresholdCandidateOut,
    CandidateOut,
    DashboardSummaryOut,
    ExceptionDetailOut,
    ManualMatchRequest,
    MatchDetailOut,
    MatchSummaryOut,
    QueueItemOut,
    QueueResponse,
    RecommendationOut,
    ResolvedParticipant,
    ReviewedItemOut,
    ReviewedItemsResponse,
    TxnDetailOut,
)
from app.services.audit import record_audit
from app.services.policy import load_policy
from app.services.reasoning import get_agent
from app.services.reconciliation.engine import _ALL_SOURCES, _priority_for
from app.services.review import (
    ReviewError,
    approve_match,
    create_manual_match,
    dismiss_exception,
    escalate_exception,
    reject_match,
)
from app.services.semantic.matcher import SemanticMatcher

logger = logging.getLogger(__name__)

router = APIRouter(tags=["review"])

_QUEUE_EXCEPTION_STATUSES = ("open", "in_review", "escalated")

_AI_ANALYSIS_ACTION = "ai.analysis"
# The analysis reference search re-runs the semantic embedder against the
# unmatched pool with a much lower bar than the live gate. Anything surfaced
# here is informational only — "below threshold, for reference only" — it is
# never proposed as a match and does not change matching/gating.
_AI_ANALYSIS_REFERENCE_THRESHOLD = 0.25
_AI_ANALYSIS_REFERENCE_TOP_K = 5


def _txn_detail(txn: Transaction) -> TxnDetailOut:
    return TxnDetailOut(
        id=txn.id,
        external_ref=txn.external_ref,
        source=txn.source,
        amount=txn.amount,
        direction=txn.direction,
        transaction_type=txn.transaction_type,
        currency=txn.currency,
        txn_date=txn.txn_date,
        narration=txn.narration,
        counterparty=txn.counterparty,
        status=txn.status,
        raw=txn.raw or {},
    )


async def _find_settlement(
    db: AsyncSession, source: str, external_ref: str
) -> Transaction | None:
    """Locate the positive settlement whose external_ref matches a payment
    identifier shared by a refund (read-only; never creates a match)."""
    row = await db.execute(
        select(Transaction)
        .where(Transaction.source == source)
        .where(Transaction.external_ref == external_ref)
        .where(Transaction.transaction_type == "settlement")
        .order_by(Transaction.txn_date)
        .limit(1)
    )
    return row.scalars().first()


async def _resolve_refund_origin(
    db: AsyncSession, txn: Transaction
) -> Transaction | None:
    """Resolve the original settlement a refund reverses, by the identifier the
    two share — payment_id (razorpay refund vs original razorpay settlement) or
    payment_ref (erp refund carrying the original payment reference). Pure
    reference data for the reviewer drawer; not part of the matching engine."""
    if getattr(txn, "transaction_type", "settlement") != "refund":
        return None
    raw = txn.raw or {}
    if txn.source == "razorpay" or txn.source == "erp":
        for key in ("payment_id", "payment_ref"):
            ref = raw.get(key)
            if not ref:
                continue
            origin = await _find_settlement(db, "razorpay", ref)
            if origin is not None and origin.id != txn.id:
                return origin
    return None


def _fmt_confidence(value) -> str | None:
    """Normalize a float/Decimal/str confidence to a '0.5500'-style string."""
    if value is None or value == "":
        return None
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


async def _opened_details(db: AsyncSession, exception_id: uuid.UUID) -> dict:
    """The details block the engine wrote when it opened the exception
    ('exception.opened'): stage, AI decision/similarity floor, missing
    sources — the evidence the reviewer needs before the raw data.
    """
    row = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "exception",
                AuditLog.entity_id == exception_id,
                AuditLog.action == "exception.opened",
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalars().first()
    return (row.details or {}) if row is not None else {}


async def _ai_decision_on(db: AsyncSession, txn_id: uuid.UUID) -> dict:
    """Latest ai.decision audit for a transaction (fallback when the
    exception.opened block isn't available, e.g. match-only detail views).
    """
    row = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "transaction",
                AuditLog.entity_id == txn_id,
                AuditLog.action == "ai.decision",
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalars().first()
    return (row.details or {}) if row is not None else {}


def _verdict_for(exception: ExceptionRecord | None, evidence: dict) -> str | None:
    if exception is not None:
        if exception.exception_type in ("manual_review_required", "low_confidence_ai"):
            return "needs_human"
        if exception.exception_type == "amount_mismatch":
            return "match"
    raw = evidence.get("ai_decision") or evidence.get("decision")
    raw = str(raw) if raw is not None else None
    if raw in ("match", "no_match", "needs_human"):
        return raw
    return None


async def _match_sources(db: AsyncSession, match_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(Transaction.source)
        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
        .where(MatchParticipant.match_id == match_id)
    )
    return {source for (source,) in rows.all()}


async def _participant_ids(db: AsyncSession, match_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(
        select(Transaction.id)
        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
        .where(MatchParticipant.match_id == match_id)
        .order_by(Transaction.source != "razorpay")
    )
    return [row[0] for row in rows.all()]


async def _cached_analysis(db: AsyncSession, exception_id: uuid.UUID) -> dict | None:
    """The latest ``ai.analysis`` cached for an exception (none => do not
    re-run the payload; it was never generated, generate on first open)."""
    row = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "exception",
                AuditLog.entity_id == exception_id,
                AuditLog.action == _AI_ANALYSIS_ACTION,
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalars().first()
    return (row.details or {}) if row is not None else None


def _analysis_out(details: dict) -> AnalysisOut:
    """Rehydrate the e2e AnalysisOut from the cached audit payload."""
    refs: list[BelowThresholdCandidateOut] = []
    for raw_ref in details.get("references") or []:
        if not raw_ref.get("transaction_id"):
            continue
        refs.append(
            BelowThresholdCandidateOut(
                transaction_id=uuid.UUID(raw_ref["transaction_id"]),
                external_ref=raw_ref.get("external_ref") or "",
                source=raw_ref.get("source") or "",
                amount=Decimal(raw_ref.get("amount") or "0"),
                txn_date=dt.datetime.fromisoformat(raw_ref["txn_date"]),
                narration=raw_ref.get("narration"),
                similarity=float(raw_ref.get("similarity") or 0.0),
            )
        )
    return AnalysisOut(
        label="AI Analysis",
        classification=details.get("classification") or "manual_investigation",
        confidence=details.get("confidence"),
        rationale=details.get("rationale") or "",
        missing_sources=[str(source) for source in (details.get("missing_sources") or [])],
        below_threshold_candidates=refs,
        model=details.get("model"),
    )


async def _below_threshold_references(
    db: AsyncSession,
    txn: Transaction,
    pool: list[Transaction],
    policy: dict,
) -> list[tuple[Transaction, float]]:
    """Re-run the semantic pool under a deliberately lower bar, purely to give
    the analysis agent reference context. Best-effort: any embedder/index
    failure degrades to no references rather than failing the drawer."""
    cross_source = [c for c in pool if c.source != txn.source and c.id != txn.id]
    if len(cross_source) < 2:
        return []
    try:
        matcher = SemanticMatcher(get_settings())
        await matcher.ensure_indexed(cross_source)
        reference_policy = dict(policy)
        reference_policy["matching.semantic.similarity_threshold"] = (
            _AI_ANALYSIS_REFERENCE_THRESHOLD
        )
        reference_policy["matching.semantic.top_k"] = _AI_ANALYSIS_REFERENCE_TOP_K
        hits = await matcher.top_candidates(txn, cross_source, reference_policy)
        return [(c, float(s)) for c, s in hits]
    except Exception as exc:
        logger.warning("below-threshold reference search failed for %s: %s", txn.id, exc)
        return []


async def _ai_analysis_for(
    db: AsyncSession,
    *,
    exception: ExceptionRecord,
    txn: Transaction,
    match: Match | None,
    pool: list[Transaction],
    actor: str,
) -> AnalysisOut:
    """Generate (or serve the cached) AI read for a pending item with no
    actionable proposal: zero-candidate sweeps and incomplete rule groups.

    Runs the reasoning agent on the transaction's own fields plus the missing
    sources and any below-threshold references, then caches the result once in
    an ``ai.analysis`` audit row so reopening the drawer never re-calls the
    model. Never proposes a match and never touches matching/gating.
    """
    cached = await _cached_analysis(db, exception.id)
    if cached is not None:
        return _analysis_out(cached)

    policy = await load_policy(db)
    settings = get_settings()
    agent = get_agent(settings)

    if match is not None:
        sources = await _match_sources(db, match.id)
        missing = sorted(_ALL_SOURCES - sources)
        hold_reason = (
            "the engine linked this record into an incomplete group "
            f"(missing source(s): {', '.join(missing) or 'unknown'})"
        )
    else:
        missing = sorted(_ALL_SOURCES - {txn.source})
        hold_reason = "no matcher stage produced a counterpart for this transaction"

    references = await _below_threshold_references(db, txn, pool, policy)

    result = await agent.analyze(
        txn, policy=policy, missing_sources=missing, references=references
    )

    reference_payload = [
        {
            "transaction_id": str(c.id),
            "external_ref": c.external_ref,
            "source": c.source,
            "amount": str(c.amount),
            "txn_date": c.txn_date.isoformat(),
            "narration": c.narration,
            "similarity": similarity,
        }
        for c, similarity in references
    ]
    await record_audit(
        db,
        actor=actor,
        action=_AI_ANALYSIS_ACTION,
        entity_type="exception",
        entity_id=exception.id,
        details={
            "model": agent.name,
            "classification": result.classification,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "missing_sources": missing,
            "references": reference_payload,
            "hold_reason": hold_reason,
            "exception_type": exception.exception_type,
        },
    )
    await db.commit()
    return AnalysisOut(
        label="AI Analysis",
        classification=result.classification,
        confidence=result.confidence,
        rationale=result.rationale,
        missing_sources=missing,
        below_threshold_candidates=[
            BelowThresholdCandidateOut(
                transaction_id=ref.id,
                external_ref=ref.external_ref,
                source=ref.source,
                amount=ref.amount,
                txn_date=ref.txn_date,
                narration=ref.narration,
                similarity=similarity,
            )
            for ref, similarity in references
        ],
        model=agent.name,
    )


async def _analysis_context(
    db: AsyncSession, exception: ExceptionRecord
) -> tuple[Transaction | None, Match | None, list[Transaction]]:
    """The (transaction, featured match, cross-source candidate pool) needed to
    generate an AI analysis. Shared by the first-open drawer request and the
    background generation task so both build identical context."""
    txn = await db.get(Transaction, exception.transaction_id) if exception.transaction_id else None
    if txn is None:
        return None, None, []
    related_rows = (
        (
            await db.execute(
                select(Match)
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .where(MatchParticipant.transaction_id == txn.id)
                .where(Match.status.in_(["proposed", "confirmed"]))
                .order_by(Match.proposed_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    featured = next(
        (m for m in related_rows if m.status == "proposed"),
        related_rows[0] if related_rows else None,
    )
    claimed = select(MatchParticipant.transaction_id).join(
        Match, Match.id == MatchParticipant.match_id
    ).where(Match.status == "confirmed")
    pool = (
        (
            await db.execute(
                select(Transaction)
                .where(Transaction.source != txn.source)
                .where(Transaction.id.not_in(claimed))
                .order_by(Transaction.txn_date)
                .limit(400)
            )
        )
        .scalars()
        .all()
    )
    return txn, featured, list(pool)


async def _generate_ai_analysis_background(
    exception_id: uuid.UUID, actor: str
) -> None:
    """Generate and cache the AI analysis off the drawer-open request path.

    The first open of a candidate-less pending exception used to run the
    reasoning agent synchronously, which made the drawer feel slow (seconds of
    LLM + semantic indexing). Here that work is dispatched as a FastAPI
    background task: the drawer returns instantly with ``analysis_status =
    "pending"`` and the frontend polls the analysis endpoint until the cached
    ``ai.analysis`` audit row is ready.

    Best-effort: any failure just leaves the item without an analysis block
    (matching the pre-existing fallback), it never brings the drawer down.
    """
    try:
        async with SessionLocal() as db:
            cached = await _cached_analysis(db, exception_id)
            if cached is not None:
                logger.info("analysis already cached for %s; skipping background run", exception_id)
                return
            exception = await db.get(ExceptionRecord, exception_id)
            if exception is None or exception.status not in _QUEUE_EXCEPTION_STATUSES:
                return
            txn, featured, pool = await _analysis_context(db, exception)
            if txn is None:
                return
            await _ai_analysis_for(
                db,
                exception=exception,
                txn=txn,
                match=featured,
                pool=pool,
                actor=actor,
            )
    except Exception as exc:
        logger.warning("background AI analysis failed for %s: %s", exception_id, exc)


async def _recommendation(
    db: AsyncSession,
    *,
    match: Match | None,
    exception: ExceptionRecord | None,
    txn_id: uuid.UUID | None,
) -> RecommendationOut | None:
    """Build the recommendation block for the drawer's top section.

    AI-shaped evidence (semantic / ai) gets a verdict + similarity/floor
    context straight from the pipeline's audit trail; rule-shaped evidence
    (deterministic / fuzzy / batch / sweep) gets the stage + why it is
    incomplete instead.
    """
    opened = await _opened_details(db, exception.id) if exception is not None else {}

    stage = match.match_type if match is not None else opened.get("stage")
    if stage == "semantic_ai":
        stage = "semantic"

    if stage in ("ai", "semantic"):
        evidence: dict = opened
        if not evidence:
            for tid in (
                await _participant_ids(db, match.id)
                if match is not None
                else [txn_id]
            ):
                if tid is None:
                    continue
                evidence = await _ai_decision_on(db, tid)
                if evidence:
                    break
        if not evidence and txn_id is not None:
            evidence = await _ai_decision_on(db, txn_id)
        if evidence and not opened:
            # Raw ai.decision rows keep similarity under best_candidate; fold
            # them up so the DTO carries one vocabulary.
            best = evidence.get("best_candidate") or {}
            evidence = {
                "ai_decision": evidence.get("ai_decision") or evidence.get("decision"),
                "ai_confidence": evidence.get("ai_confidence")
                or evidence.get("confidence"),
                "ai_rationale": evidence.get("ai_rationale") or evidence.get("rationale"),
                "similarity": (
                    evidence.get("similarity")
                    if evidence.get("similarity") is not None
                    else best.get("similarity")
                ),
                "similarity_autoresolve_min": evidence.get(
                    "similarity_autoresolve_min"
                ),
            }
        similarity = evidence.get("similarity")
        floor = evidence.get("similarity_autoresolve_min")
        floor_met = None
        if similarity is not None and floor is not None:
            floor_met = float(similarity) >= float(floor)
        confidence = (
            evidence.get("ai_confidence")
            or evidence.get("confidence")
            or (str(match.confidence_score) if match is not None else None)
        )
        rationale = (
            evidence.get("ai_rationale")
            or evidence.get("rationale")
            or (match.rationale if match is not None else None)
        )
        blocked = evidence.get("auto_resolve_blocked_by")
        if blocked is None and evidence.get("materiality"):
            blocked = "materiality"
        conf_threshold = None
        conf_floor_met = None
        if match is not None and match.policy_snapshot:
            raw_gate = match.policy_snapshot.get("gate.ai_min_confidence_autoresolve")
            if raw_gate is not None:
                try:
                    conf_threshold = float(raw_gate)
                except (TypeError, ValueError):
                    conf_threshold = None
                if conf_threshold is not None and confidence is not None:
                    try:
                        conf_floor_met = float(confidence) >= conf_threshold
                    except (TypeError, ValueError):
                        conf_floor_met = None
        return RecommendationOut(
            verdict=_verdict_for(exception, evidence),
            stage=stage,
            confidence_score=_fmt_confidence(confidence),
            similarity=float(similarity) if similarity is not None else None,
            similarity_autoresolve_min=float(floor) if floor is not None else None,
            floor_met=floor_met,
            confidence_autoresolve_min=conf_threshold,
            confidence_floor_met=conf_floor_met,
            blocked_reason=str(blocked) if blocked else None,
            rationale=rationale,
        )

    # Rule-shaped evidence: no AI verdict to surface.
    if match is not None:
        sources = await _match_sources(db, match.id)
        missing = sorted(_ALL_SOURCES - sources)
        incomplete_reason = None
        if match.status != "confirmed":
            if exception is not None and exception.exception_type == "amount_mismatch":
                incomplete_reason = "amount discrepancy flagged outside materiality limits"
            elif missing:
                incomplete_reason = f"missing source(s): {', '.join(missing)}"
        return RecommendationOut(
            stage=match.match_type,
            confidence_score=str(match.confidence_score),
            rationale=match.rationale,
            incomplete_reason=incomplete_reason,
        )

    # Sweep / no-candidate: nothing produced a match for this transaction.
    note = opened.get("note") or "no candidate produced by any matcher stage"
    return RecommendationOut(
        stage=opened.get("stage") or "sweep",
        rationale=note,
        incomplete_reason="no candidate produced by any matcher stage",
    )


async def _load_queue(
    db: AsyncSession,
    *,
    item_type: str | None,
    status: str | None,
    exception_type: str | None,
    priority: str | None,
) -> tuple[list[QueueItemOut], dict[str, int]]:
    policy = await load_policy(db)

    exception_stmt = select(ExceptionRecord, Transaction.external_ref).join(
        Transaction, Transaction.id == ExceptionRecord.transaction_id, isouter=True
    )
    if status:
        exception_stmt = exception_stmt.where(ExceptionRecord.status == status)
    else:
        exception_stmt = exception_stmt.where(
            ExceptionRecord.status.in_(_QUEUE_EXCEPTION_STATUSES)
        )
    if exception_type:
        exception_stmt = exception_stmt.where(ExceptionRecord.exception_type == exception_type)
    if priority:
        exception_stmt = exception_stmt.where(ExceptionRecord.priority == priority)

    items: list[QueueItemOut] = []
    for exception, ref in (await db.execute(exception_stmt)).all():
        items.append(
            QueueItemOut(
                item_type="exception",
                id=exception.id,
                title=exception.exception_type.replace("_", " "),
                status=exception.status,
                priority=exception.priority,
                amount_impact=exception.amount_impact,
                opened_at=exception.opened_at,
                refs=[ref] if ref else [],
                exception_type=exception.exception_type,
            )
        )

    proposal_stmt = select(Match).where(Match.status == "proposed")
    proposals = (await db.execute(proposal_stmt.order_by(Match.proposed_at.desc()))).scalars().unique().all()

    refs_by_match: dict[uuid.UUID, list[str]] = {}
    amounts: dict[uuid.UUID, Decimal] = {}
    if proposals:
        rows = await db.execute(
            select(MatchParticipant.match_id, Transaction)
            .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
            .where(MatchParticipant.match_id.in_([m.id for m in proposals]))
        )
        for match_id, txn in rows.all():
            refs_by_match.setdefault(match_id, []).append(txn.external_ref)
            amounts[match_id] = max(amounts.get(match_id, Decimal("0")), txn.amount)

    for match in proposals:
        items.append(
            QueueItemOut(
                item_type="proposal",
                id=match.id,
                title=f"{match.match_type} proposal",
                status="proposed",
                priority=_priority_for(amounts.get(match.id, Decimal("0")), policy),
                amount_impact=amounts.get(match.id),
                opened_at=match.proposed_at,
                refs=sorted(refs_by_match.get(match.id, [])),
                confidence=str(match.confidence_score),
                match_type=match.match_type,
                rationale=match.rationale,
            )
        )

    counts = {
        "exceptions": sum(1 for i in items if i.item_type == "exception"),
        "proposals": sum(1 for i in items if i.item_type == "proposal"),
    }
    exception_scoped = bool(exception_type or priority)
    if item_type:
        items = [i for i in items if i.item_type == item_type]
    elif exception_scoped:
        items = [i for i in items if i.item_type == "exception"]
        if exception_type:
            items = [i for i in items if i.exception_type == exception_type]
        if priority:
            items = [i for i in items if i.priority == priority]
    return items, counts


@router.get("/review-queue", response_model=QueueResponse)
async def review_queue(
    db: SessionDep,
    item_type: str | None = Query(None, pattern="^(exception|proposal)$"),
    status: str | None = Query(
        None, pattern="^(open|in_review|escalated|resolved|dismissed|proposed)$"
    ),
    exception_type: str | None = None,
    priority: str | None = Query(None, pattern="^(low|medium|high|critical)$"),
    sort_by: str = Query("opened_at", pattern="^(amount_impact|opened_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> QueueResponse:
    items, counts = await _load_queue(
        db,
        item_type=item_type,
        status=status,
        exception_type=exception_type,
        priority=priority,
    )
    reverse = order == "desc"

    def sort_key(item: QueueItemOut):
        if sort_by == "amount_impact":
            return (item.amount_impact is not None, item.amount_impact or 0)
        return item.opened_at

    items.sort(key=sort_key, reverse=reverse)
    return QueueResponse(items=items, counts=counts)


_HUMAN_MATCH_ACTIONS = {
    "match.approved": "approved",
    "match.manual_created": "manually matched",
    "match.rejected": "rejected",
}
_HUMAN_MATCH_ACTIONS_BY_LABEL = {v: k for k, v in _HUMAN_MATCH_ACTIONS.items()}
_REVIEWED_ACTIONS = {"approved", "manually matched", "rejected", "dismissed"}


@router.get("/reviewed", response_model=ReviewedItemsResponse)
async def list_reviewed_items(
    db: SessionDep,
    action: str | None = Query(None, pattern="^(approved|manually matched|rejected|dismissed)$"),
    actor: str | None = None,
    item_type: str | None = Query(None, pattern="^(match|exception)$"),
    limit: int = Query(100, ge=1, le=250),
) -> ReviewedItemsResponse:
    """Human decisions, attributed from the audit trail.

    Combines human-reviewed matches (approved / manually matched / rejected)
    with explicitly *dismissed* exceptions. Exceptions that were resolved
    incidentally because a linked match was approved are deliberately omitted
    here — the match itself is the reviewed item, so including them would
    double-count the same decision.
    """
    items: list[ReviewedItemOut] = []
    match_count = 0
    exception_count = 0

    # ── Human-reviewed matches ──────────────────────────────
    match_stmt = select(Match).where(
        Match.resolved_by == "human",
        Match.status.in_(["confirmed", "rejected"]),
    )
    if item_type == "exception":
        match_stmt = match_stmt.where(False)
    if action and action != "dismissed":
        match_stmt = match_stmt.where(
            Match.id.in_(
                select(AuditLog.entity_id).where(
                    AuditLog.entity_type == "match",
                    AuditLog.action == _HUMAN_MATCH_ACTIONS_BY_LABEL[action],
                )
            )
        )
    if actor:
        match_stmt = match_stmt.where(Match.decided_by == actor)
    match_stmt = match_stmt.order_by(Match.resolved_at.desc().nulls_last(), Match.id).limit(limit)
    matches = (await db.execute(match_stmt)).scalars().unique().all()

    if matches:
        participant_rows = await db.execute(
            select(MatchParticipant, Transaction)
            .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
            .where(MatchParticipant.match_id.in_([m.id for m in matches]))
        )
        participants_by_match: dict = {}
        for participant, txn in participant_rows.all():
            participants_by_match.setdefault(participant.match_id, []).append(
                ResolvedParticipant(
                    transaction_id=txn.id,
                    external_ref=txn.external_ref,
                    source=txn.source,
                    role=participant.role,
                    amount=txn.amount,
                )
            )

        audit_rows = await db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "match",
                AuditLog.entity_id.in_([m.id for m in matches]),
                AuditLog.action.in_(_HUMAN_MATCH_ACTIONS),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
        audit_by_match: dict = {}
        for audit in audit_rows.scalars():
            if audit.entity_id not in audit_by_match:
                audit_by_match[audit.entity_id] = audit

        match_count = len(matches)
        for match in matches:
            audit = audit_by_match.get(match.id)
            items.append(
                ReviewedItemOut(
                    item_type="match",
                    id=match.id,
                    status=match.status,
                    action=_HUMAN_MATCH_ACTIONS[audit.action] if audit else "approved",
                    actor=(audit.actor if audit else None) or match.decided_by,
                    note=(audit.details or {}).get("note") if audit else None,
                    reviewed_at=match.resolved_at,
                    match_type=match.match_type,
                    confidence_score=str(match.confidence_score),
                    participants=participants_by_match.get(match.id, []),
                    rationale=match.rationale,
                )
            )

    # ── Dismissed exceptions ────────────────────────────────
    exc_stmt = (
        select(ExceptionRecord, Transaction.external_ref)
        .join(Transaction, Transaction.id == ExceptionRecord.transaction_id, isouter=True)
        .where(ExceptionRecord.status == "dismissed")
    )
    if item_type == "match":
        exc_stmt = exc_stmt.where(False)
    if action == "dismissed":
        pass  # already filtered to dismissed
    elif action:
        exc_stmt = exc_stmt.where(False)
    exception_rows = (await db.execute(exc_stmt)).all()

    if exception_rows:
        exc_ids = [e.id for e, _ in exception_rows]
        exc_audit_rows = await db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "exception",
                AuditLog.entity_id.in_(exc_ids),
                AuditLog.action == "exception.dismissed",
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        )
        exc_audit_by_id: dict = {}
        for audit in exc_audit_rows.scalars():
            if audit.entity_id not in exc_audit_by_id:
                exc_audit_by_id[audit.entity_id] = audit

        exception_count = len(exception_rows)
        for exception, ref in exception_rows:
            if actor and exception.assigned_to != actor:
                continue
            audit = exc_audit_by_id.get(exception.id)
            items.append(
                ReviewedItemOut(
                    item_type="exception",
                    id=exception.id,
                    status="dismissed",
                    action="dismissed",
                    actor=exception.assigned_to or (audit.actor if audit else None),
                    note=exception.resolution_note or ((audit.details or {}).get("note") if audit else None),
                    reviewed_at=exception.resolved_at,
                    exception_type=exception.exception_type,
                    priority=exception.priority,
                    amount_impact=str(exception.amount_impact) if exception.amount_impact is not None else None,
                    transaction_ref=ref,
                )
            )

    items.sort(
        key=lambda i: i.reviewed_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    items = items[:limit]
    return ReviewedItemsResponse(
        items=items,
        match_count=match_count,
        exception_count=exception_count,
    )


@router.get("/review-queue/exceptions/{exception_id}", response_model=ExceptionDetailOut)
async def exception_detail(
    db: SessionDep,
    actor: ActorDep,
    exception_id: uuid.UUID,
    background_tasks: BackgroundTasks,
) -> ExceptionDetailOut:
    exception = await db.get(ExceptionRecord, exception_id)
    if exception is None:
        raise HTTPException(status_code=404, detail=f"exception {exception_id} not found")

    txn, featured, candidate_rows = await _analysis_context(db, exception)

    original_transaction: TxnDetailOut | None = None
    if txn is not None and exception.exception_type == "refund":
        origin = await _resolve_refund_origin(db, txn)
        if origin is not None:
            original_transaction = _txn_detail(origin)

    related_rows = (
        await db.execute(
            select(Match)
            .join(MatchParticipant, MatchParticipant.match_id == Match.id)
            .where(MatchParticipant.transaction_id == exception.transaction_id)
            .where(Match.status.in_(["proposed", "confirmed"]))
            .order_by(Match.proposed_at.desc())
            .limit(10)
        )
    ).scalars().unique().all()

    candidates: list[CandidateOut] = []
    if txn is not None and exception.status in ("open", "in_review", "escalated"):
        probe = " ".join(filter(None, [txn.narration, txn.counterparty, txn.external_ref])).lower()
        scored: list[tuple[float, Transaction]] = []
        for cand in candidate_rows:
            cand_text = " ".join(
                filter(None, [cand.narration, cand.counterparty, cand.external_ref])
            ).lower()
            text_score = token_set_ratio(probe, cand_text) / 100.0
            amount_span = max(txn.amount, cand.amount) or Decimal("1")
            proximity = 1.0 - min(1.0, float(abs(txn.amount - cand.amount) / amount_span))
            same_day = txn.txn_date.date() == cand.txn_date.date()
            score = 0.4 * text_score + 0.4 * proximity + (0.2 if same_day else 0.0)
            scored.append((score, cand))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        candidates = [
            CandidateOut(
                transaction_id=cand.id,
                external_ref=cand.external_ref,
                source=cand.source,
                amount=cand.amount,
                txn_date=cand.txn_date,
                narration=cand.narration,
                score=round(score, 4),
            )
            for score, cand in scored[:8]
            if score >= 0.30
        ]

    recommendation = await _recommendation(
        db,
        match=featured,
        exception=exception,
        txn_id=exception.transaction_id,
    )

    # Candidate-less pending items get an AI analysis on first open (cached
    # once per exception in an ai.analysis audit row). Items that already
    # carry AI/semantic evidence keep their recommendation block untouched.
    #
    # The generation runs off the request critical path as a background task:
    # the drawer returns instantly with analysis_status="pending" and the
    # frontend polls /analysis until the cached result is ready.
    analysis_status = "none"
    if (
        txn is not None
        and exception.status in _QUEUE_EXCEPTION_STATUSES
        and recommendation is not None
        and (recommendation.stage or "") not in ("ai", "semantic")
    ):
        cached = await _cached_analysis(db, exception.id)
        if cached is not None:
            recommendation.analysis = _analysis_out(cached)
            analysis_status = "ready"
        else:
            analysis_status = "pending"
            background_tasks.add_task(
                _generate_ai_analysis_background,
                exception_id=exception.id,
                actor=actor,
            )

    return ExceptionDetailOut(
        id=exception.id,
        exception_type=exception.exception_type,
        priority=exception.priority,
        status=exception.status,
        amount_impact=str(exception.amount_impact) if exception.amount_impact is not None else None,
        opened_at=exception.opened_at,
        resolution_note=exception.resolution_note,
        transaction=_txn_detail(txn) if txn is not None else None,
        original_transaction=original_transaction,
        related_matches=[
            MatchSummaryOut(
                id=m.id,
                match_type=m.match_type,
                confidence_score=str(m.confidence_score),
                status=m.status,
                resolved_by=m.resolved_by,
                decided_by=m.decided_by,
                rationale=m.rationale,
            )
            for m in related_rows
        ],
        candidates=candidates,
        recommendation=recommendation,
        analysis_status=analysis_status,
    )


@router.get("/review-queue/exceptions/{exception_id}/analysis", response_model=AnalysisOut | None)
async def exception_analysis(
    db: SessionDep, exception_id: uuid.UUID
) -> AnalysisOut | None:
    """Return the cached AI analysis for an exception, or None while it is still
    being generated in the background. The drawer polls this after opening a
    pending item."""
    cached = await _cached_analysis(db, exception_id)
    if cached is None:
        return None
    return _analysis_out(cached)


@router.get("/review-queue/matches/{match_id}", response_model=MatchDetailOut)
async def match_detail(db: SessionDep, match_id: uuid.UUID) -> MatchDetailOut:
    match = await db.get(Match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail=f"match {match_id} not found")
    rows = await db.execute(
        select(Transaction)
        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
        .where(MatchParticipant.match_id == match.id)
        .order_by(Transaction.source, Transaction.external_ref)
    )
    participants = [_txn_detail(t) for t in rows.scalars().all()]
    primary_txn_id = next(
        (p.id for p in participants if p.source == "razorpay"),
        participants[0].id if participants else None,
    )
    recommendation = await _recommendation(
        db, match=match, exception=None, txn_id=primary_txn_id
    )
    return MatchDetailOut(
        id=match.id,
        match_type=match.match_type,
        confidence_score=str(match.confidence_score),
        status=match.status,
        resolved_by=match.resolved_by,
        decided_by=match.decided_by,
        rationale=match.rationale,
        proposed_at=match.proposed_at,
        resolved_at=match.resolved_at,
        participants=participants,
        recommendation=recommendation,
    )


async def _apply(action):
    try:
        return await action()
    except ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/review/matches/{match_id}/approve", response_model=ActionResponse)
async def approve_match_endpoint(
    db: SessionDep, actor: ActorDep, match_id: uuid.UUID, payload: ActionRequest | None = None
) -> ActionResponse:
    async def run():
        return await approve_match(
            db, match_id=match_id, actor=actor, note=(payload.note if payload else "")
        )

    match, resolved = await _apply(run)
    await db.commit()
    return ActionResponse(
        message=f"match approved by {actor}",
        match_id=match.id,
        resolved_exceptions=resolved,
    )


@router.post("/review/matches/{match_id}/reject", response_model=ActionResponse)
async def reject_match_endpoint(
    db: SessionDep, actor: ActorDep, match_id: uuid.UUID, payload: ActionRequest | None = None
) -> ActionResponse:
    async def run():
        return await reject_match(
            db, match_id=match_id, actor=actor, note=(payload.note if payload else "")
        )

    match = await _apply(run)
    await db.commit()
    return ActionResponse(message=f"match rejected by {actor}", match_id=match.id)


@router.post("/review/matches/manual", response_model=ActionResponse)
async def manual_match_endpoint(
    db: SessionDep, actor: ActorDep, payload: ManualMatchRequest
) -> ActionResponse:
    if len(set(payload.transaction_ids)) != len(payload.transaction_ids):
        raise HTTPException(status_code=422, detail="duplicate transaction ids in request")

    async def run():
        return await create_manual_match(
            db,
            transaction_ids=payload.transaction_ids,
            actor=actor,
            note=payload.note,
            replace_proposed_match_id=payload.replace_proposed_match_id,
        )

    match, resolved = await _apply(run)
    await db.commit()
    return ActionResponse(
        message=f"manual match created by {actor}",
        match_id=match.id,
        resolved_exceptions=resolved,
    )


@router.post("/review/exceptions/{exception_id}/dismiss", response_model=ActionResponse)
async def dismiss_exception_endpoint(
    db: SessionDep, actor: ActorDep, exception_id: uuid.UUID, payload: ActionRequest | None = None
) -> ActionResponse:
    async def run():
        return await dismiss_exception(
            db, exception_id=exception_id, actor=actor, note=(payload.note if payload else "")
        )

    exception = await _apply(run)
    await db.commit()
    return ActionResponse(
        message=f"exception dismissed by {actor}", exception_id=exception.id
    )


@router.post("/review/exceptions/{exception_id}/escalate", response_model=ActionResponse)
async def escalate_exception_endpoint(
    db: SessionDep, actor: ActorDep, exception_id: uuid.UUID, payload: ActionRequest | None = None
) -> ActionResponse:
    async def run():
        return await escalate_exception(
            db, exception_id=exception_id, actor=actor, note=(payload.note if payload else "")
        )

    exception = await _apply(run)
    await db.commit()
    return ActionResponse(
        message=f"exception escalated by {actor}", exception_id=exception.id
    )


@router.get("/dashboard/summary", response_model=DashboardSummaryOut)
async def dashboard_summary(db: SessionDep) -> DashboardSummaryOut:
    today = dt.datetime.now(dt.timezone.utc).date()

    type_rows = await db.execute(
        select(ExceptionRecord.exception_type, func.count())
        .where(ExceptionRecord.status.in_(_QUEUE_EXCEPTION_STATUSES))
        .group_by(ExceptionRecord.exception_type)
    )
    exceptions_by_type = {etype: count for etype, count in type_rows.all()}

    priority_rows = await db.execute(
        select(ExceptionRecord.priority, func.count())
        .where(ExceptionRecord.status.in_(_QUEUE_EXCEPTION_STATUSES))
        .group_by(ExceptionRecord.priority)
    )
    exceptions_by_priority = {prio: count for prio, count in priority_rows.all()}

    open_total = sum(exceptions_by_type.values())
    proposals = await db.scalar(
        select(func.count()).select_from(Match).where(Match.status == "proposed")
    )

    auto_today = await db.scalar(
        select(func.count())
        .select_from(Match)
        .where(
            Match.status == "confirmed",
            Match.resolved_by == "auto",
            func.date(Match.resolved_at) == today,
        )
    ) or 0
    human_today = await db.scalar(
        select(func.count())
        .select_from(Match)
        .where(
            Match.resolved_by == "human",
            func.date(Match.resolved_at) == today,
        )
    ) or 0
    exceptions_closed_today = await db.scalar(
        select(func.count())
        .select_from(ExceptionRecord)
        .where(
            ExceptionRecord.status.in_(["resolved", "dismissed"]),
            func.date(ExceptionRecord.resolved_at) == today,
        )
    ) or 0

    return DashboardSummaryOut(
        open_exceptions_total=open_total,
        exceptions_by_type=exceptions_by_type,
        exceptions_by_priority=exceptions_by_priority,
        proposals_awaiting_review=int(proposals),
        decisions_today_total=int(auto_today) + int(human_today),
        auto_resolved_today=int(auto_today),
        human_resolved_today=int(human_today),
        exceptions_closed_today=int(exceptions_closed_today),
    )
