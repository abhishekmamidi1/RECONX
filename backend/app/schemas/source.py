from enum import Enum

from pydantic import BaseModel


class SourceEnum(str, Enum):
    razorpay = "razorpay"
    bank = "bank"
    erp = "erp"


class SourceInfoOut(BaseModel):
    key: str
    label: str
    required_columns: list[str]
    optional_columns: list[str]
