import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Ingestion(Base):
    __tablename__ = "ingestions"
    __table_args__ = (
        CheckConstraint(
            "source IN ('razorpay', 'bank', 'erp')", name="ck_ingestions_source"
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed')", name="ck_ingestions_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(Text)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
