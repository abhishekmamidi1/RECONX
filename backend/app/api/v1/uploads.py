import hashlib
from pathlib import PurePosixPath
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import insert, select

from app.api.deps import ActorDep, SessionDep
from app.core.config import get_settings
from app.models import Ingestion, Transaction
from app.schemas import SourceEnum, UploadResponse
from app.services.audit import record_audit
from app.services.parsers import ParseResult, ParserError, get_parser
from app.services.parsers.base import TransactionDraft

router = APIRouter(prefix="/uploads", tags=["uploads"])

_INSERT_CHUNK_SIZE = 500
_REJECTION_PREVIEW_LIMIT = 5


def _safe_filename(name: str | None) -> str:
    cleaned = (name or "upload.csv").replace("\\", "/").split("/")[-1].strip()
    return cleaned or "upload.csv"


def _draft_to_row(draft: TransactionDraft, ingestion_id) -> dict:
    return {
        "ingestion_id": ingestion_id,
        "source": draft.source,
        "external_ref": draft.external_ref,
        "amount": draft.amount,
        "direction": draft.direction,
        "transaction_type": draft.transaction_type,
        "currency": draft.currency,
        "txn_date": draft.txn_date,
        "narration": draft.narration,
        "counterparty": draft.counterparty,
        "status": draft.status,
        "raw": draft.raw,
    }


@router.post("", response_model=UploadResponse, status_code=201)
async def upload_csv(
    source: Annotated[SourceEnum, Form()],
    file: Annotated[UploadFile, File()],
    db: SessionDep,
    actor: ActorDep,
) -> UploadResponse:
    settings = get_settings()

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(content) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds max upload size of {settings.max_upload_size_mb} MB",
        )

    try:
        parser = get_parser(source.value)
        result: ParseResult = await anyio.to_thread.run_sync(parser.parse, content)
    except ParserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    checksum = hashlib.sha256(content).hexdigest()
    filename = _safe_filename(file.filename)

    if result.rows_total == 0:
        ingestion = Ingestion(
            source=source.value,
            filename=filename,
            checksum_sha256=checksum,
            rows_total=0,
            status="failed",
            error_detail="CSV contained no data rows",
        )
        db.add(ingestion)
        await db.flush()
        await record_audit(
            db,
            actor=actor,
            action="ingestion.failed",
            entity_type="ingestion",
            entity_id=ingestion.id,
            details={"filename": filename, "reason": "no data rows"},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail="CSV contained no data rows")

    ingestion = Ingestion(
        source=source.value,
        filename=filename,
        checksum_sha256=checksum,
        rows_total=result.rows_total,
        rows_rejected=result.rows_rejected,
        status="pending",
    )
    db.add(ingestion)
    await db.flush()

    drafts = result.drafts
    refs = [d.external_ref for d in drafts]
    existing_refs: set[str] = set()
    if refs:
        existing_result = await db.execute(
            select(Transaction.external_ref).where(
                Transaction.source == source.value,
                Transaction.external_ref.in_(refs),
            )
        )
        existing_refs = set(existing_result.scalars().all())

    unique_new: list[TransactionDraft] = []
    intra_file_duplicates = 0
    for draft in drafts:
        if draft.external_ref in existing_refs:
            continue
        if any(d.external_ref == draft.external_ref for d in unique_new):
            intra_file_duplicates += 1
            continue
        unique_new.append(draft)

    for start in range(0, len(unique_new), _INSERT_CHUNK_SIZE):
        chunk = unique_new[start : start + _INSERT_CHUNK_SIZE]
        rows = [_draft_to_row(d, ingestion.id) for d in chunk]
        await db.execute(insert(Transaction), rows)

    duplicates_skipped = len(drafts) - len(unique_new)

    ingestion.rows_inserted = len(unique_new)
    ingestion.rows_skipped_duplicate = duplicates_skipped
    ingestion.status = "completed"
    if result.rejections:
        preview = "; ".join(result.rejections[:_REJECTION_PREVIEW_LIMIT])
        extra = len(result.rejections) - _REJECTION_PREVIEW_LIMIT
        ingestion.error_detail = f"{preview}; (+{extra} more)" if extra > 0 else preview

    await record_audit(
        db,
        actor=actor,
        action="ingestion.completed",
        entity_type="ingestion",
        entity_id=ingestion.id,
        after_state={
            "source": ingestion.source,
            "filename": filename,
            "rows_total": ingestion.rows_total,
            "rows_inserted": ingestion.rows_inserted,
            "rows_skipped_duplicate": ingestion.rows_skipped_duplicate,
            "rows_rejected": ingestion.rows_rejected,
            "status": ingestion.status,
        },
        details={
            "checksum_sha256": checksum,
            "rejections_preview": result.rejections[:10],
        },
    )
    await db.commit()
    await db.refresh(ingestion)

    return UploadResponse(
        ingestion_id=ingestion.id,
        source=ingestion.source,
        filename=filename,
        checksum_sha256=checksum,
        rows_total=ingestion.rows_total,
        rows_inserted=ingestion.rows_inserted,
        rows_skipped_duplicate=ingestion.rows_skipped_duplicate,
        rows_rejected=ingestion.rows_rejected,
        status=ingestion.status,
        rejections_preview=result.rejections[:10],
    )
