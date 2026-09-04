import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "match_type IN ('deterministic', 'fuzzy', 'semantic', 'ai', 'manual', 'batch')",
            name="ck_matches_type",
        ),
        CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')", name="ck_matches_status"
        ),
        CheckConstraint("confidence_score >= 0 AND confidence_score <= 1",
                        name="ck_matches_confidence_range"),
        Index("idx_matches_status_proposed", "status", text("proposed_at DESC")),
        Index("idx_matches_type_confidence", "match_type", text("confidence_score DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_type: Mapped[str] = mapped_column(String(16))
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    resolved_by: Mapped[str | None] = mapped_column(String(8))
    decided_by: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    policy_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    proposed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class MatchParticipant(Base):
    __tablename__ = "match_participants"
    __table_args__ = (
        CheckConstraint(
            "role IN ('primary', 'candidate', 'participant')",
            name="ck_match_participants_role",
        ),
    )

    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("matches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(12), default="participant")
