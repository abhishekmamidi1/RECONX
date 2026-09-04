"""Report generation: reconciliation summary data, CSV and PDF exports.

Queries accept an optional date range (inclusive) applied to
matches.proposed_at / exceptions.opened_at and/or an optional ``ingestion_id``
batch scope. When a batch is selected the report describes that batch:

* summary stats / exceptions / match-rate are restricted to the batch's own
  transactions (``Transaction.ingestion_id``),
* matches shown are those with at least one participant from the batch,
* participants from *other* batches that appear in those matches are included
  as ``cross_batch_participants`` so reviewers keep full match context without
  leaking other batches into the headline numbers.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid

from sqlalchemy import false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExceptionRecord, Ingestion, Match, MatchParticipant, Transaction


def _in_chk(column, values):
    """``IN`` filter that degrades to ``FALSE`` for an empty set (avoids ``IN ()``)."""
    return column.in_(values) if values else false()


def _parse_range(from_str: str | None, to_str: str | None) -> tuple[dt.datetime | None, dt.datetime | None]:
    def parse(s: str | None, end: bool = False) -> dt.datetime | None:
        if not s:
            return None
        d = dt.date.fromisoformat(s)
        return dt.datetime.combine(d, dt.time(23, 59, 59, 999999) if end else dt.time.min, tzinfo=dt.timezone.utc)

    return parse(from_str), parse(to_str, end=True)


async def _batch_scope(db: AsyncSession, ingestion_id: uuid.UUID) -> dict:
    """Resolve an ingestion scope: its own transactions, related match ids and context rows."""
    ingestion = await db.get(Ingestion, ingestion_id)
    if ingestion is None:
        raise ValueError("ingestion_id does not exist")

    batch_ids = set(
        (await db.execute(select(Transaction.id).where(Transaction.ingestion_id == ingestion_id))).scalars()
    )
    related_ids: set = set()
    if batch_ids:
        related_ids = set(
            (
                await db.execute(
                    select(MatchParticipant.match_id).where(MatchParticipant.transaction_id.in_(batch_ids))
                )
            ).scalars()
        )

    cross_batch: list[dict] = []
    if related_ids:
        rows = (
            await db.execute(
                select(
                    Match.id,
                    Match.match_type,
                    MatchParticipant.role,
                    Transaction.id,
                    Transaction.external_ref,
                    Transaction.source,
                    Transaction.amount,
                )
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
                .where(MatchParticipant.match_id.in_(related_ids))
            )
        ).all()
        seen: set = set()
        for match_id, match_type, role, txn_id, external_ref, source, amount in rows:
            if txn_id in batch_ids:
                continue
            key = (str(match_id), str(txn_id))
            if key in seen:
                continue
            seen.add(key)
            cross_batch.append(
                {
                    "match_id": match_id,
                    "match_type": match_type,
                    "transaction_id": txn_id,
                    "external_ref": external_ref,
                    "source": source,
                    "role": role,
                    "amount": str(amount),
                }
            )

    return {
        "ingestion_id": str(ingestion.id),
        "filename": ingestion.filename,
        "source": ingestion.source,
        "rows_total": ingestion.rows_total or 0,
        "batch_ids": batch_ids,
        "related_match_ids": related_ids,
        "cross_batch_participants": cross_batch,
    }


async def build_summary(
    db: AsyncSession, from_str: str | None, to_str: str | None, ingestion_id: uuid.UUID | None = None
) -> dict:
    range_from, range_to = _parse_range(from_str, to_str)

    scope: dict | None = None
    if ingestion_id is not None:
        scope = await _batch_scope(db, ingestion_id)
        batch_ids: set = scope["batch_ids"]
        related_ids: set = scope["related_match_ids"]

    match_stmt = (
        select(
            Match.match_type,
            Match.status,
            Match.resolved_by,
            func.count(),
        ).group_by(Match.match_type, Match.status, Match.resolved_by)
    )
    if scope is not None:
        match_stmt = match_stmt.where(_in_chk(Match.id, related_ids))
    exception_stmt = (
        select(
            ExceptionRecord.exception_type,
            ExceptionRecord.status,
            Transaction.source,
            func.count(),
        )
        .join(Transaction, Transaction.id == ExceptionRecord.transaction_id, isouter=True)
        .group_by(ExceptionRecord.exception_type, ExceptionRecord.status, Transaction.source)
    )
    if range_from:
        match_stmt = match_stmt.where(Match.proposed_at >= range_from)
        exception_stmt = exception_stmt.where(ExceptionRecord.opened_at >= range_from)
    if range_to:
        match_stmt = match_stmt.where(Match.proposed_at <= range_to)
        exception_stmt = exception_stmt.where(ExceptionRecord.opened_at <= range_to)
    if scope is not None:
        exception_stmt = exception_stmt.where(_in_chk(ExceptionRecord.transaction_id, batch_ids))

    matches: list[dict] = []
    for match_type, status, resolved_by, count in (await db.execute(match_stmt)).all():
        matches.append(
            {"match_type": match_type, "status": status, "resolved_by": resolved_by, "count": int(count)}
        )

    exceptions: list[dict] = []
    for etype, status, source, count in (await db.execute(exception_stmt)).all():
        exceptions.append(
            {"exception_type": etype, "status": status, "source": source or "unknown", "count": int(count)}
        )

    confirmed_ids = select(Match.id).where(Match.status == "confirmed")
    if scope is not None:
        confirmed_ids = confirmed_ids.where(Match.id.in_(related_ids))
    participant_stmt = (
        select(Transaction.source, func.count(func.distinct(MatchParticipant.transaction_id)))
        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
        .where(MatchParticipant.match_id.in_(confirmed_ids))
        .group_by(Transaction.source)
    )
    total_stmt = select(Transaction.source, func.count()).group_by(Transaction.source)
    if scope is not None:
        participant_stmt = participant_stmt.where(_in_chk(MatchParticipant.transaction_id, batch_ids))
        total_stmt = total_stmt.where(_in_chk(Transaction.id, batch_ids))

    matched_by_source = {s: int(c) for s, c in (await db.execute(participant_stmt)).all()}
    total_by_source = {s: int(c) for s, c in (await db.execute(total_stmt)).all()}

    now = dt.datetime.now(dt.timezone.utc)
    open_exc_stmt = select(ExceptionRecord).where(ExceptionRecord.status.in_(["open", "in_review", "escalated"]))
    if scope is not None:
        open_exc_stmt = open_exc_stmt.where(_in_chk(ExceptionRecord.transaction_id, batch_ids))
    aging = {"under_7d": 0, "d7_30": 0, "over_30d": 0}
    for exc in (await db.execute(open_exc_stmt)).scalars():
        age_days = (now - exc.opened_at).days
        if age_days < 7:
            aging["under_7d"] += 1
        elif age_days < 30:
            aging["d7_30"] += 1
        else:
            aging["over_30d"] += 1

    auto_resolved = sum(m["count"] for m in matches if m["status"] == "confirmed" and m["resolved_by"] == "auto")
    human_resolved = sum(m["count"] for m in matches if m["resolved_by"] == "human")

    summary = {
        "generated_at": now.isoformat(),
        "range": {"from": range_from.date().isoformat() if range_from else None, "to": range_to.date().isoformat() if range_to else None},
        "matches": matches,
        "exceptions": exceptions,
        "match_rate_by_source": [
            {
                "source": s,
                "total_transactions": total_by_source.get(s, 0),
                "matched": matched_by_source.get(s, 0),
                "rate": round(matched_by_source[s] / total_by_source[s], 4) if total_by_source.get(s) else 0.0,
            }
            for s in ("razorpay", "bank", "erp")
        ],
        "auto_resolved_total": auto_resolved,
        "human_resolved_total": human_resolved,
        "open_exception_aging": aging,
    }
    if scope is not None:
        summary["scope"] = {
            "ingestion_id": scope["ingestion_id"],
            "filename": scope["filename"],
            "source": scope["source"],
            "rows_total": scope["rows_total"],
            "transactions_in_batch": len(scope["batch_ids"]),
        }
        summary["cross_batch_participants"] = scope["cross_batch_participants"]
    return summary


async def export_exceptions_csv(
    db: AsyncSession, from_str: str | None, to_str: str | None, ingestion_id: uuid.UUID | None = None
) -> str:
    """Full exception ledger with resolution status.

    For ingestion-scoped exports the batch identity and any cross-batch match
    participants are emitted as ``#`` preamble lines before the identical
    exception table, so existing consumers keep the same column layout.
    """
    scope: dict | None = None
    if ingestion_id is not None:
        scope = await _batch_scope(db, ingestion_id)

    stmt = (
        select(ExceptionRecord, Transaction)
        .join(Transaction, Transaction.id == ExceptionRecord.transaction_id, isouter=True)
        .order_by(ExceptionRecord.opened_at.desc())
    )
    range_from, range_to = _parse_range(from_str, to_str)
    if range_from:
        stmt = stmt.where(ExceptionRecord.opened_at >= range_from)
    if range_to:
        stmt = stmt.where(ExceptionRecord.opened_at <= range_to)
    if scope is not None:
        stmt = stmt.where(ExceptionRecord.transaction_id.in_(scope["batch_ids"]))

    buffer = io.StringIO()
    if scope is not None:
        buffer.write(f"# batch: {scope['filename']} (source={scope['source']}, rows_total={scope['rows_total']})")
        if scope["cross_batch_participants"]:
            buffer.write("\n# cross-batch participants:")
            for row in scope["cross_batch_participants"]:
                buffer.write(
                    f"\n#   {row['source']}:{row['external_ref']} ({row['role']}, amount={row['amount']}) "
                    f"in match {row['match_id']} [{row['match_type']}]"
                )
        buffer.write("\n")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "exception_id", "opened_at", "exception_type", "priority", "status",
            "transaction_ref", "source", "amount_impact", "assigned_to",
            "resolved_at", "resolution_note",
        ]
    )
    for exc, txn in (await db.execute(stmt)).all():
        writer.writerow(
            [
                str(exc.id),
                exc.opened_at.isoformat(),
                exc.exception_type,
                exc.priority,
                exc.status,
                txn.external_ref if txn else "",
                txn.source if txn else "",
                str(exc.amount_impact) if exc.amount_impact is not None else "",
                exc.assigned_to or "",
                exc.resolved_at.isoformat() if exc.resolved_at else "",
                exc.resolution_note or "",
            ]
        )
    return buffer.getvalue()


def render_pdf(summary: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReportTitle", parent=styles["Title"], fontSize=18, spaceAfter=2)
    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], textColor=colors.HexColor("#64748b"), fontSize=9)
    section_style = ParagraphStyle("Section", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Reconciliation Report",
    )
    flow: list = []

    flow.append(Paragraph("Reconciliation Report", title_style))
    scope: dict | None = summary.get("scope")
    if scope:
        range_label = summary["range"]["from"] or "beginning"
        flow.append(
            Paragraph(
                f"Batch {scope['filename']} (source={scope['source']}, "
                f"{scope['transactions_in_batch']}/{scope['rows_total']} transactions ingested) · "
                f"Period {range_label} → {summary['range']['to'] or 'today'} · generated "
                f"{summary['generated_at'][:19].replace('T', ' ')} UTC",
                subtitle_style,
            )
        )
    else:
        range_label = summary["range"]["from"] or "beginning"
        flow.append(
            Paragraph(
                f"Period {range_label} → {summary['range']['to'] or 'today'} · generated "
                f"{summary['generated_at'][:19].replace('T', ' ')} UTC",
                subtitle_style,
            )
        )
    flow.append(Spacer(1, 8 * mm))

    def table(data: list[list[str]], widths=None, highlight_rows=()):
        t = Table(data, colWidths=widths, hAlign="LEFT")
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for row_index in highlight_rows:
            style.append(("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#fef3c7")))
        t.setStyle(TableStyle(style))
        flow.append(t)
        flow.append(Spacer(1, 5 * mm))

    # Totals
    total_confirmed = sum(m["count"] for m in summary["matches"] if m["status"] == "confirmed")
    total_proposed = sum(m["count"] for m in summary["matches"] if m["status"] == "proposed")
    total_rejected = sum(m["count"] for m in summary["matches"] if m["status"] == "rejected")
    total_open = sum(e["count"] for e in summary["exceptions"] if e["status"] in ("open", "in_review", "escalated"))
    aging = summary["open_exception_aging"]

    flow.append(Paragraph("Reconciliation totals", section_style))
    table(
        [
            ["Metric", "Value"],
            ["Matches confirmed", str(total_confirmed)],
            ["— auto-resolved", str(summary["auto_resolved_total"])],
            ["— human-resolved", str(summary["human_resolved_total"])],
            ["Proposals awaiting review", str(total_proposed)],
            ["Rejected matches", str(total_rejected)],
            ["Open exceptions", str(total_open)],
        ],
        widths=[80 * mm, 30 * mm],
        highlight_rows=(7,) if total_open else (),
    )

    flow.append(Paragraph("Match rate by source", section_style))
    rate_rows = [["Source", "Transactions", "Matched", "Rate"]]
    for row in summary["match_rate_by_source"]:
        rate_rows.append([row["source"], str(row["total_transactions"]), str(row["matched"]), f'{row["rate"] * 100:.1f}%'])
    table(rate_rows, widths=[40 * mm, 30 * mm, 25 * mm, 20 * mm])

    if summary.get("cross_batch_participants"):
        flow.append(Paragraph("Match context — participants from other batches", section_style))
        ctx_rows = [["Match type", "Source", "Reference", "Role", "Amount"]]
        for row in summary["cross_batch_participants"]:
            ctx_rows.append(
                [
                    row["match_type"],
                    row["source"],
                    row["external_ref"],
                    row["role"],
                    row["amount"],
                ]
            )
        table(ctx_rows, widths=[30 * mm, 25 * mm, 50 * mm, 25 * mm, 25 * mm])

    flow.append(Paragraph("Exception aging (open items)", section_style))
    table(
        [
            ["Age bucket", "Count"],
            ["Under 7 days", str(aging["under_7d"])],
            ["7 – 30 days", str(aging["d7_30"])],
            ["Over 30 days", str(aging["over_30d"])],
        ],
        widths=[60 * mm, 25 * mm],
        highlight_rows=(3,) if aging["over_30d"] else (),
    )

    flow.append(Paragraph("Exceptions by type & status", section_style))
    exc_rows = [["Type", "Status", "Source", "Count"]]
    for e in sorted(summary["exceptions"], key=lambda x: (-x["count"], x["exception_type"]))[:14]:
        exc_rows.append([e["exception_type"].replace("_", " "), e["status"], e["source"], str(e["count"])])
    if len(exc_rows) > 1:
        table(exc_rows, widths=[55 * mm, 30 * mm, 30 * mm, 20 * mm])
    else:
        flow.append(Paragraph("No exceptions recorded in this period.", subtitle_style))

    doc.build(flow)
    return buf.getvalue()
