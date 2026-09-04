import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict


class UploadResponse(BaseModel):
    ingestion_id: uuid.UUID
    source: str
    filename: str
    checksum_sha256: str
    rows_total: int
    rows_inserted: int
    rows_skipped_duplicate: int
    rows_rejected: int
    status: str
    rejections_preview: list[str] = []


class IngestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    filename: str
    rows_total: int
    rows_inserted: int
    rows_skipped_duplicate: int
    rows_rejected: int
    status: str
    error_detail: str | None
    created_at: dt.datetime
