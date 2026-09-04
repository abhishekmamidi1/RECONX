import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer


class CloseRunRequest(BaseModel):
    transaction_ids: list[uuid.UUID] | None = None


class CloseDecisionOut(BaseModel):
    record_ref: str
    source: str
    amount: Decimal
    decision: str
    matched_with: list[str] | None = None
    reason_code: str | None = None
    rationale: str
    suggested_next_action: str

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)


class SourceCloseOut(BaseModel):
    source: str
    total_transactions: int
    matched: int
    no_match: int
    deferred: int
    rate: float


class CloseRunResponse(BaseModel):
    model_config = ConfigDict()

    records_scanned: int
    matched: int
    no_match: int
    deferred: int
    match_rate: float
    deferred_by_reason: dict[str, int]
    by_source: list[SourceCloseOut]
    decisions: list[CloseDecisionOut]
    duration_ms: float
    # Measured metrics — added for dashboard and reporting
    execution_time_seconds: float = 0.0
    throughput_records_per_second: float = 0.0
    accuracy_available: bool = False
    decision_accuracy: float | None = None
    matched_precision: float | None = None
    matched_recall: float | None = None
    matched_f1: float | None = None
    no_match_precision: float | None = None
    no_match_recall: float | None = None
    deferred_precision: float | None = None
    deferred_recall: float | None = None
    records_evaluated: int = 0
    correct_predictions: int = 0
    total_errors: int = 0