import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer


class RunRequest(BaseModel):
    transaction_ids: list[uuid.UUID] | None = None


class RunResponse(BaseModel):
    transactions_scanned: int
    exact_auto_resolved: int
    fuzzy_auto_resolved: int
    incomplete_proposed: int
    exceptions_opened: int
    conflicts: list[dict]
    duration_ms: float
    ai_candidates_evaluated: int = 0
    ai_auto_resolved: int = 0
    ai_proposed: int = 0
    ai_no_match: int = 0
    batch_candidates_generated: int = 0
    batch_ai_evaluated: int = 0
    batch_auto_resolved: int = 0
    batch_proposed: int = 0
    batch_no_match: int = 0


class ParticipantOut(BaseModel):
    external_ref: str
    source: str
    role: str
    amount: Decimal

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)


class MatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_type: str
    confidence_score: Decimal
    status: str
    resolved_by: str | None
    decided_by: str | None
    rationale: str | None
    proposed_at: dt.datetime
    resolved_at: dt.datetime | None
    participants: list[ParticipantOut] = []

    @field_serializer("confidence_score")
    def serialize_confidence(self, value: Decimal) -> str:
        return str(value)


class ExceptionOut(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID | None
    transaction_ref: str | None = None
    exception_type: str
    priority: str
    amount_impact: Decimal | None
    status: str
    opened_at: dt.datetime
    resolution_note: str | None

    @field_serializer("amount_impact")
    def serialize_amount(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None
