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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("source", "external_ref", name="uq_transactions_source_ref"),
        CheckConstraint("amount >= 0", name="ck_transactions_amount_nonneg"),
        CheckConstraint(
            "source IN ('razorpay', 'bank', 'erp')", name="ck_transactions_source"
        ),
        CheckConstraint(
            "direction IN ('credit', 'debit')", name="ck_transactions_direction"
        ),
        CheckConstraint(
            "transaction_type IN ('settlement', 'refund')",
            name="ck_transactions_type",
        ),
        Index("idx_transactions_source_date", "source", "txn_date"),
        Index("idx_transactions_source_amount", "source", "amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ingestion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestions.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(16))
    external_ref: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    direction: Mapped[str] = mapped_column(String(8), default="credit")
    transaction_type: Mapped[str] = mapped_column(String(16), default="settlement")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    txn_date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    narration: Mapped[str | None] = mapped_column(Text)
    counterparty: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
