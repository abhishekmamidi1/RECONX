import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ingestion_id: uuid.UUID | None
    source: str
    external_ref: str
    amount: Decimal
    direction: str
    transaction_type: str
    currency: str
    txn_date: dt.datetime
    narration: str | None
    counterparty: str | None
    status: str | None

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)


class TransactionPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TransactionOut]
