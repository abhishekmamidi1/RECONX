import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ActorDep, SessionDep
from app.models import AuditLog, Ingestion, Transaction
from app.services.audit import record_audit
from app.services.controller import close_reconciliation
from app.services.reports import build_summary, export_exceptions_csv, render_pdf

router = APIRouter(prefix="/reports", tags=["reports"])


def _validated_ingestion(ingestion_id: uuid.UUID | None) -> uuid.UUID | None:
    return ingestion_id


async def _ingestion_filename(db: AsyncSession, ingestion_id: uuid.UUID | None) -> str | None:
    if ingestion_id is None:
        return None
    ingestion = await db.get(Ingestion, ingestion_id)
    if ingestion is None:
        raise HTTPException(status_code=404, detail="ingestion_id does not exist")
    return ingestion.filename


async def _log_generation(
    db: AsyncSession,
    actor: str,
    fmt: str,
    range_from: str | None,
    range_to: str | None,
    ingestion_id: uuid.UUID | None = None,
):
    details: dict = {"format": fmt, "range_from": range_from, "range_to": range_to}
    if ingestion_id is not None:
        details["ingestion_id"] = str(ingestion_id)
        details["ingestion_filename"] = await _ingestion_filename(db, ingestion_id)
    await record_audit(
        db,
        actor=actor,
        action="report.generated",
        entity_type="report",
        entity_id=None,
        details=details,
    )


def _filename(fmt: str, range_to: str | None, ingestion_filename: str | None = None) -> str:
    if ingestion_filename:
        stem = ingestion_filename.rsplit(".", 1)[0] if "." in ingestion_filename else ingestion_filename
        return f"batch-report_{stem}.{fmt}"
    day = range_to or dt.date.today().isoformat()
    return f"reconciliation-report_{day}.{fmt}"


@router.get("/summary")
async def report_summary(
    db: SessionDep,
    actor: ActorDep,
    from_: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ingestion_id: uuid.UUID | None = Query(None),
) -> dict:
    try:
        summary = await build_summary(db, from_, to, ingestion_id=ingestion_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _log_generation(db, actor, "summary", from_, to, ingestion_id)
    await db.commit()
    return summary


@router.get("/export.csv")
async def export_csv(
    db: SessionDep,
    actor: ActorDep,
    from_: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ingestion_id: uuid.UUID | None = Query(None),
) -> Response:
    try:
        csv_text = await export_exceptions_csv(db, from_, to, ingestion_id=ingestion_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _log_generation(db, actor, "csv", from_, to, ingestion_id)
    await db.commit()
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_filename("csv", to, await _ingestion_filename(db, ingestion_id))}"'
            )
        },
    )


@router.get("/export.pdf")
async def export_pdf(
    db: SessionDep,
    actor: ActorDep,
    from_: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ingestion_id: uuid.UUID | None = Query(None),
) -> Response:
    try:
        summary = await build_summary(db, from_, to, ingestion_id=ingestion_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    pdf_bytes = render_pdf(summary)
    await _log_generation(db, actor, "pdf", from_, to, ingestion_id)
    await db.commit()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_filename("pdf", to, await _ingestion_filename(db, ingestion_id))}"'
            )
        },
    )


async def _scope_transaction_ids(db: AsyncSession, ingestion_id: uuid.UUID | None) -> list[uuid.UUID] | None:
    """Resolve an ingestion scope into its transaction ids, or ``None`` for the whole ledger."""
    if ingestion_id is None:
        return None
    ingestion = await db.get(Ingestion, ingestion_id)
    if ingestion is None:
        raise HTTPException(status_code=404, detail="ingestion_id does not exist")
    ids = (
        await db.execute(select(Transaction.id).where(Transaction.ingestion_id == ingestion_id))
    ).scalars().all()
    return list(ids)


def _exception_list(result: dict) -> list[dict]:
    """Flatten deferred decisions into the honest exception list with reason codes."""
    by_reason: dict[str, int] = result.get("deferred_by_reason", {})
    return [
        {
            "record_ref": None,
            "reason_code": code,
            "count": count,
        }
        for code, count in by_reason.items()
        if count
    ]


@router.get("/loop-close")
async def report_loop_close(
    db: SessionDep,
    actor: ActorDep,
    ingestion_id: uuid.UUID | None = Query(None),
) -> dict:
    """Run-time metrics for the close controller on a batch (or the whole ledger).

    Reports throughput, match rate (overall and by source), and — when the
    scoped records exactly match the golden ground-truth labels — decision
    accuracy / per-class precision & recall. The exception list is the honest
    ledger of deferred records by reason code.
    """
    try:
        transaction_ids = await _scope_transaction_ids(db, ingestion_id)
        result = await close_reconciliation(db, actor=actor, transaction_ids=transaction_ids)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    scope_meta: dict | None = None
    if ingestion_id is not None:
        ingestion = await db.get(Ingestion, ingestion_id)
        if ingestion is not None:
            scope_meta = {
                "ingestion_id": str(ingestion.id),
                "filename": ingestion.filename,
                "source": ingestion.source,
                "rows_total": ingestion.rows_total or 0,
            }

    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": scope_meta,
        "records_scanned": result["records_scanned"],
        "matched": result["matched"],
        "no_match": result["no_match"],
        "deferred": result["deferred"],
        "match_rate": result["match_rate"],
        "match_rate_by_source": result["by_source"],
        "deferred_by_reason": result["deferred_by_reason"],
        "exceptions": _exception_list(result),
        "throughput_records_per_second": result.get("throughput_records_per_second", 0.0),
        "execution_time_seconds": result.get("execution_time_seconds", 0.0),
        "duration_ms": result.get("duration_ms", 0.0),
        "accuracy_available": result.get("accuracy_available", False),
        "decision_accuracy": result.get("decision_accuracy"),
        "matched_precision": result.get("matched_precision"),
        "matched_recall": result.get("matched_recall"),
        "matched_f1": result.get("matched_f1"),
        "no_match_precision": result.get("no_match_precision"),
        "no_match_recall": result.get("no_match_recall"),
        "deferred_precision": result.get("deferred_precision"),
        "deferred_recall": result.get("deferred_recall"),
        "records_evaluated": result.get("records_evaluated", 0),
        "correct_predictions": result.get("correct_predictions", 0),
        "total_errors": result.get("total_errors", 0),
    }

    await _log_generation(db, actor, "loop-close", None, None, ingestion_id)
    await db.commit()
    return report