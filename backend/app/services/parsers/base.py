from __future__ import annotations

import io
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import pandas as pd

_AMOUNT_STRIP_CHARS = str.maketrans("", "", "₹$,\u00a0 ")


class ParserError(ValueError):
    pass


def clean_amount(value: object) -> Decimal:
    """Parse a positive amount (magnitude). Raises for negatives, because
    callers that feed the positive-credit matcher must never see a negative —
    the model stores money as magnitude + direction, not signed values."""
    magnitude, sign = clean_signed_amount(value)
    if sign < 0:
        raise ValueError(f"negative amount: {value!r}")
    return magnitude


def clean_signed_amount(value: object) -> tuple[Decimal, int]:
    """Parse an amount into (magnitude, sign) where sign is +1 or -1. Used by
    sources (e.g. Razorpay settlement exports) that legitimately carry
    negative refund/reversal rows; the caller decides classification from the
    sign rather than dropping the data."""
    text = str(value).translate(_AMOUNT_STRIP_CHARS)
    if not text:
        raise ValueError("empty amount")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount: {value!r}") from exc
    sign = -1 if amount < 0 else 1
    return abs(amount), sign


def optional_amount(value: object) -> Decimal | None:
    if str(value).strip() == "":
        return None
    return clean_amount(value)


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


def clean_datetime(value: object) -> datetime:
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {value!r}")


def business_date(value: datetime) -> "date":
    if value.tzinfo is None:
        raise ValueError("naive datetime; all stored datetimes must be tz-aware UTC")
    return value.astimezone(timezone.utc).date()


@dataclass(slots=True)
class TransactionDraft:
    source: str
    external_ref: str
    amount: Decimal
    direction: str
    txn_date: datetime
    currency: str = "INR"
    narration: str | None = None
    counterparty: str | None = None
    status: str | None = None
    transaction_type: str = "settlement"
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class ParseResult:
    source: str
    rows_total: int = 0
    rows_parsed: int = 0
    rows_rejected: int = 0
    rejections: list[str] = field(default_factory=list)
    drafts: list[TransactionDraft] = field(default_factory=list)


class SourceParser(ABC):
    key: ClassVar[str]
    label: ClassVar[str]
    required_columns: ClassVar[frozenset[str]] = frozenset()
    optional_columns: ClassVar[frozenset[str]] = frozenset()

    def parse(self, content: bytes) -> ParseResult:
        df = self._read(content)
        result = ParseResult(source=self.key, rows_total=len(df))
        for position, row in enumerate(df.itertuples(index=False), start=2):
            row_dict = dict(zip(df.columns, row))
            try:
                draft = self._build(row_dict)
            except (ValueError, KeyError) as exc:
                result.rows_rejected += 1
                result.rejections.append(f"row {position}: {exc}")
                continue
            result.drafts.append(draft)
            result.rows_parsed += 1
        return result

    def _read(self, content: bytes) -> pd.DataFrame:
        if not content.strip():
            raise ParserError("uploaded file is empty")
        try:
            df = pd.read_csv(
                io.BytesIO(content),
                dtype=str,
                keep_default_na=False,
                encoding="utf-8-sig",
            )
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            raise ParserError(f"not a readable CSV: {exc}") from exc
        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
        missing = sorted(set(self.required_columns) - set(df.columns))
        if missing:
            raise ParserError(
                f"{self.key} CSV is missing required columns: {', '.join(missing)}"
            )
        return df

    @staticmethod
    def _text(row: dict, name: str) -> str:
        return str(row.get(name, "")).strip()

    @staticmethod
    def _require_ref(row: dict, name: str) -> str:
        ref = str(row.get(name, "")).strip()
        if not ref:
            raise ValueError(f"missing {name}")
        return ref

    @abstractmethod
    def _build(self, row: dict) -> TransactionDraft: ...
