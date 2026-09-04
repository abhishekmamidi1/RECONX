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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExceptionRecord(Base):
    __tablename__ = "exceptions"
    __table_args__ = (
        CheckConstraint(
            "exception_type IN ("
            "'unmatched', 'amount_mismatch', 'duplicate_suspect', "
            "'low_confidence_ai', 'manual_review_required', 'refund')",
            name="ck_exceptions_type",
        ),
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')",
            name="ck_exceptions_priority",
        ),
        CheckConstraint(
            "status IN ('open', 'in_review', 'escalated', 'resolved', 'dismissed')",
            name="ck_exceptions_status",
        ),
        Index("idx_exceptions_status_opened", "status", "opened_at"),
        Index("idx_exceptions_priority", "priority", text("opened_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL")
    )
    exception_type: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(8), default="medium")
    amount_impact: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(16), default="open")
    assigned_to: Mapped[str | None] = mapped_column(Text)
    resolution_note: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
