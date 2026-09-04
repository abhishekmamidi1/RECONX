import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class QueueItemOut(BaseModel):
    item_type: str  # "exception" | "proposal"
    id: uuid.UUID
    title: str
    status: str
    priority: str
    amount_impact: Decimal | None
    opened_at: dt.datetime
    refs: list[str] = []
    confidence: str | None = None
    match_type: str | None = None
    exception_type: str | None = None
    rationale: str | None = None

    @field_serializer("amount_impact")
    def serialize_amount(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


class QueueResponse(BaseModel):
    items: list[QueueItemOut]
    counts: dict[str, int]


class TxnDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_ref: str
    source: str
    amount: Decimal
    direction: str
    transaction_type: str
    currency: str
    txn_date: dt.datetime
    narration: str | None
    counterparty: str | None
    status: str | None
    raw: dict

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)


class CandidateOut(BaseModel):
    transaction_id: uuid.UUID
    external_ref: str
    source: str
    amount: Decimal
    txn_date: dt.datetime
    narration: str | None
    score: float

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)


class MatchSummaryOut(BaseModel):
    id: uuid.UUID
    match_type: str
    confidence_score: str
    status: str
    resolved_by: str | None
    decided_by: str | None
    rationale: str | None


class BelowThresholdCandidateOut(BaseModel):
    """A cross-source record the semantic pool surfaced for the record under
    review, at a similarity below ``matching.semantic.similarity_threshold``.
    Purely informational — the AI flags it for the operator's glance but it is
    NOT a real candidate and must never be treated as a proposal."""

    transaction_id: uuid.UUID
    external_ref: str
    source: str
    amount: Decimal
    txn_date: dt.datetime
    narration: str | None
    similarity: float

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)


class AnalysisOut(BaseModel):
    """AI-produced read on a pending item that has no actionable proposal yet.

    Complements the engine's ``recommendation`` block when the rules/AI could
    not produce a match: for zero-candidate sweeps and incomplete rule groups
    the reasoning agent classifies the hold as likely-pending / data-quality /
    manual-investigation. Generated lazily on first drawer open, cached once
    per exception in an ``ai.analysis`` audit row, and clearly labeled as an
    analysis rather than a recommendation (it proposes no match)."""

    label: str = "AI Analysis"
    classification: str  # likely_pending | data_quality | manual_investigation
    confidence: float | None = None
    rationale: str
    missing_sources: list[str] = []
    below_threshold_candidates: list[BelowThresholdCandidateOut] = []
    model: str | None = None


class RecommendationOut(BaseModel):
    """The first thing a reviewer should read: why this item reached the
    human queue and what the engine thinks should happen.

    For AI-produced evidence (``stage`` in ai/semantic) ``verdict`` is a
    plain machine statement (match / no_match / needs_human) plus the
    similarity evidence against the ``matching.ai.similarity_autoresolve_min``
    floor and the confidence scored by the reasoning layer. For rule-based
    evidence (deterministic / fuzzy / batch) there is no AI verdict —
    ``incomplete_reason`` explains why the group was not auto-confirmed
    (e.g. "missing source(s): erp").
    """

    verdict: str | None = None
    stage: str | None = None
    confidence_score: str | None = None
    similarity: float | None = None
    similarity_autoresolve_min: float | None = None
    floor_met: bool | None = None
    confidence_autoresolve_min: float | None = None
    confidence_floor_met: bool | None = None
    blocked_reason: str | None = None
    rationale: str | None = None
    incomplete_reason: str | None = None
    analysis: AnalysisOut | None = None


class ExceptionDetailOut(BaseModel):
    id: uuid.UUID
    exception_type: str
    priority: str
    status: str
    amount_impact: str | None
    opened_at: dt.datetime
    resolution_note: str | None
    transaction: TxnDetailOut | None
    original_transaction: TxnDetailOut | None = None
    related_matches: list[MatchSummaryOut] = []
    candidates: list[CandidateOut] = []
    recommendation: RecommendationOut | None = None
    # ready | pending | none — tells the drawer whether the AI analysis is
    # already generated (ready), being generated off the request path (pending,
    # poll the analysis endpoint), or not applicable for this item (none).
    analysis_status: str = "none"


class MatchDetailOut(BaseModel):
    id: uuid.UUID
    match_type: str
    confidence_score: str
    status: str
    resolved_by: str | None
    decided_by: str | None
    rationale: str | None
    proposed_at: dt.datetime
    resolved_at: dt.datetime | None
    participants: list[TxnDetailOut] = []
    recommendation: RecommendationOut | None = None


class ActionRequest(BaseModel):
    note: str = ""


class ManualMatchRequest(BaseModel):
    transaction_ids: list[uuid.UUID] = Field(min_length=2)
    note: str = ""
    replace_proposed_match_id: uuid.UUID | None = None


class ActionResponse(BaseModel):
    ok: bool = True
    message: str
    match_id: uuid.UUID | None = None
    exception_id: uuid.UUID | None = None
    resolved_exceptions: int = 0


class AuditEntryOut(BaseModel):
    id: int
    actor: str
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    before_state: dict | None
    after_state: dict | None
    details: dict | None
    request_id: str | None
    created_at: dt.datetime


class DashboardSummaryOut(BaseModel):
    open_exceptions_total: int
    exceptions_by_type: dict[str, int]
    exceptions_by_priority: dict[str, int]
    proposals_awaiting_review: int
    decisions_today_total: int
    auto_resolved_today: int
    human_resolved_today: int
    exceptions_closed_today: int


class ResolvedParticipant(BaseModel):
    external_ref: str
    source: str
    role: str
    amount: Decimal
    transaction_id: uuid.UUID

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)


class ResolvedMatchOut(BaseModel):
    """A match that a decision has already been made on.

    Auto-resolved (`stage` = the matcher that produced it: deterministic /
    fuzzy / semantic / ai / batch) or human-resolved (`actor` + `action`
    attributed from the audit trail).
    """

    match_id: uuid.UUID
    match_type: str
    status: str  # confirmed | rejected (human view can include rejected)
    confidence_score: str
    resolved_by: str | None
    decided_by: str | None
    rationale: str | None
    proposed_at: dt.datetime
    resolved_at: dt.datetime | None
    participants: list[ResolvedParticipant] = []
    stage: str | None = None
    actor: str | None = None
    action: str | None = None
    note: str | None = None


class ResolvedMatchesResponse(BaseModel):
    resolution: str  # "auto" | "human"
    auto: int  # lifetime confirmed auto-resolved matches
    human: int  # lifetime matches decided by a human reviewer
    items: list[ResolvedMatchOut]


class ReviewedItemOut(BaseModel):
    """A single record of a human decision, either on a proposed match or on
    an open exception.

    Actions are attributed from the audit trail so the UI can show *what the
    system recommended vs what the human actually did*:
      - match   approved         (confirmed the pipeline/AI suggestion)
      - match   manually matched (built a different match than proposed)
      - match   rejected         (overrode the suggestion)
      - exception dismissed      (judged a false positive)
    """

    item_type: str  # "match" | "exception"
    id: uuid.UUID
    status: str  # match: confirmed|rejected ; exception: dismissed
    action: str  # approved | manually matched | rejected | dismissed
    actor: str | None
    note: str | None
    reviewed_at: dt.datetime | None
    # match-specific (null for exceptions)
    match_type: str | None = None
    confidence_score: str | None = None
    participants: list[ResolvedParticipant] = []
    rationale: str | None = None
    # exception-specific (null for matches)
    exception_type: str | None = None
    priority: str | None = None
    amount_impact: str | None = None
    transaction_ref: str | None = None


class ReviewedItemsResponse(BaseModel):
    items: list[ReviewedItemOut]
    match_count: int
    exception_count: int
