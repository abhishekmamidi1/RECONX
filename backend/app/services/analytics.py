"""Analytics aggregation for the dashboard charts.

One endpoint, one payload: daily buckets of reconciliation activity
(matches created / decided, exceptions opened / closed), plus in-range
breakdowns by match type and resolution authorship. Buckets are
zero-filled so charts show continuous timelines.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExceptionRecord, Match


def _range_bounds(from_str: str | None, to_str: str | None, days_default: int = 30) -> tuple[dt.datetime, dt.datetime]:
    def parse(s: str | None, end: bool = False) -> dt.datetime | None:
        if not s:
            return None
        d = dt.date.fromisoformat(s)
        return dt.datetime.combine(d, dt.time(23, 59, 59, 999999) if end else dt.time.min, tzinfo=dt.timezone.utc)

    range_to = parse(to_str, end=True) or (
        dt.datetime.now(dt.timezone.utc)
    )
    range_from = parse(from_str) or (range_to - dt.timedelta(days=days_default - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return range_from, range_to


def _bucket_dates(range_from: dt.datetime, range_to: dt.datetime) -> list[dt.date]:
    days = (range_to.date() - range_from.date()).days
    return [range_from.date() + dt.timedelta(days=i) for i in range(max(days + 1, 1))]


async def overview(
    db: AsyncSession,
    *,
    from_str: str | None = None,
    to_str: str | None = None,
) -> dict:
    range_from, range_to = _range_bounds(from_str, to_str)

    created_rows = await db.execute(
        select(func.date(Match.proposed_at), func.count())
        .where(Match.proposed_at >= range_from, Match.proposed_at <= range_to)
        .group_by(func.date(Match.proposed_at))
    )
    created_by_day = {str(d): int(c) for d, c in created_rows.all()}

    decided_rows = await db.execute(
        select(
            func.date(Match.resolved_at),
            Match.status,
            Match.resolved_by,
            func.count(),
        )
        .where(
            Match.resolved_at.is_not(None),
            Match.resolved_at >= range_from,
            Match.resolved_at <= range_to,
        )
        .group_by(func.date(Match.resolved_at), Match.status, Match.resolved_by)
    )
    decided: dict[str, dict[str, int]] = {}
    for day, status, resolved_by, count in decided_rows.all():
        key = str(day)
        bucket = decided.setdefault(key, {"auto": 0, "human": 0, "rejected": 0})
        if status == "rejected":
            bucket["rejected"] += int(count)
        elif resolved_by == "auto":
            bucket["auto"] += int(count)
        elif resolved_by == "human":
            bucket["human"] += int(count)

    opened_rows = await db.execute(
        select(func.date(ExceptionRecord.opened_at), func.count())
        .where(
            ExceptionRecord.opened_at >= range_from,
            ExceptionRecord.opened_at <= range_to,
        )
        .group_by(func.date(ExceptionRecord.opened_at))
    )
    opened_by_day = {str(d): int(c) for d, c in opened_rows.all()}

    closed_rows = await db.execute(
        select(func.date(ExceptionRecord.resolved_at), ExceptionRecord.status, func.count())
        .where(
            ExceptionRecord.resolved_at.is_not(None),
            ExceptionRecord.resolved_at >= range_from,
            ExceptionRecord.resolved_at <= range_to,
        )
        .group_by(func.date(ExceptionRecord.resolved_at), ExceptionRecord.status)
    )
    closed_by_day: dict[str, dict[str, int]] = {}
    for day, status, count in closed_rows.all():
        key = str(day)
        entry = closed_by_day.setdefault(key, {"resolved": 0, "dismissed": 0})
        if status == "dismissed":
            entry["dismissed"] += int(count)
        else:
            entry["resolved"] += int(count)

    buckets = []
    for day in _bucket_dates(range_from, range_to):
        key = day.isoformat()
        decisions = decided.get(key, {})
        closed = closed_by_day.get(key, {})
        buckets.append(
            {
                "date": key,
                "matches_created": created_by_day.get(key, 0),
                "auto_resolved": decisions.get("auto", 0),
                "human_resolved": decisions.get("human", 0),
                "rejected": decisions.get("rejected", 0),
                "exceptions_opened": opened_by_day.get(key, 0),
                "exceptions_resolved": closed.get("resolved", 0) + closed.get("dismissed", 0),
            }
        )

    type_rows = await db.execute(
        select(Match.match_type, func.count())
        .where(Match.proposed_at >= range_from, Match.proposed_at <= range_to)
        .group_by(Match.match_type)
    )
    by_match_type = [{"match_type": mt, "count": int(c)} for mt, c in type_rows.all()]

    split_rows = await db.execute(
        select(Match.resolved_by, func.count())
        .where(
            Match.status.in_(["confirmed", "rejected"]),
            Match.resolved_at >= range_from,
            Match.resolved_at <= range_to,
        )
        .group_by(Match.resolved_by)
    )
    resolution_split = {"auto": 0, "human": 0}
    for resolved_by, count in split_rows.all():
        if resolved_by in resolution_split:
            resolution_split[resolved_by] = int(count)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "range": {
            "from": range_from.date().isoformat(),
            "to": range_to.date().isoformat(),
        },
        "buckets": buckets,
        "by_match_type": by_match_type,
        "resolution_split": resolution_split,
    }
