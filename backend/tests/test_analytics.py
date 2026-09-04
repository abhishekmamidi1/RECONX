"""Phase 6 analytics endpoint tests: bucketing, zero-fill, range scoping."""

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import Transaction
from app.services.reconciliation import run_reconciliation

SCOPE_REFS = [
    "pay_Hh8i9J0k", "INV-2026-0711", "UTR888170260811X",
    "pay_Sm1Th1cA", "INV-2026-0720",
    "pay_Pl4tFrmZ", "INV-2026-0721",
    "pay_Jj0k1L2m", "UTR888170260813",
]
TODAY = None  # resolved lazily; buckets are date-keyed


def _client() -> TestClient:
    return TestClient(app)


async def _ids():
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Transaction).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        missing = set(SCOPE_REFS) - {t.external_ref for t in rows}
        assert not missing, f"golden dataset not ingested: {sorted(missing)}"
        return {t.external_ref: t.id for t in rows}


async def _purge():
    from sqlalchemy import delete

    from app.models import ExceptionRecord, Match, MatchParticipant

    async with SessionLocal() as db:
        txn_ids = (
            await db.execute(
                select(Transaction.id).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        match_ids = (
            await db.execute(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.transaction_id.in_(txn_ids)
                )
            )
        ).scalars().all()
        await db.execute(delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(txn_ids)))
        await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))
        await db.commit()

    async with SessionLocal() as db:
        txn_ids = (
            await db.execute(
                select(Transaction.id).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        leftover = (
            await db.execute(
                select(Match.id)
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .where(MatchParticipant.transaction_id.in_(txn_ids))
            )
        ).all()
        assert not leftover, f"purge failed: {leftover}"


async def _run(ids):
    async with SessionLocal() as db:
        await run_reconciliation(db, actor="pytest-analytics", transaction_ids=list(ids.values()))
        await db.commit()


def test_analytics_overview_buckets_and_breakdowns():
    async def scenario():
        import datetime as dt

        ids = await _ids()
        await _purge()
        await _run(ids)

        today = dt.datetime.now(dt.timezone.utc).date()
        week_ago = today - dt.timedelta(days=6)
        return ids, today, week_ago

    ids, today, week_ago = asyncio.run(scenario())

    with _client() as client:
        data = client.get(
            "/api/v1/analytics/overview",
            params={"from": week_ago.isoformat(), "to": today.isoformat()},
        ).json()

        # shape + zero-filled continuous daily buckets covering the range
        assert data["range"]["from"] == week_ago.isoformat()
        assert data["range"]["to"] == today.isoformat()
        dates = [b["date"] for b in data["buckets"]]
        assert len(dates) == 7
        assert dates == sorted(dates)
        assert dates[-1] == today.isoformat()

        for bucket in data["buckets"]:
            assert set(bucket) == {
                "date",
                "matches_created",
                "auto_resolved",
                "human_resolved",
                "rejected",
                "exceptions_opened",
                "exceptions_resolved",
            }

        today_bucket = data["buckets"][-1]
        assert today_bucket["matches_created"] >= 4, (
            f"expected fresh pipeline artifacts today: {today_bucket}"
        )
        assert today_bucket["auto_resolved"] >= 1, "deterministic/AI auto-resolves must register"
        assert today_bucket["human_resolved"] == 0, "no human decisions were made in this scenario"
        assert today_bucket["exceptions_opened"] >= 1, "unmatched/needs_human must open exceptions"

        # breakdowns reflect the run composition at ambient policy
        types = {t["match_type"]: t["count"] for t in data["by_match_type"]}
        assert types.get("deterministic", 0) >= 2
        split = data["resolution_split"]
        assert split["auto"] >= 3 and split["human"] == 0

        # raise semantic sensitivity THROUGH THE ADMIN API and re-run: the
        # 'semantic'/'ai' buckets must appear in analytics afterwards.
        from app.models import PolicyConfig

        async def set_semantic_policy(threshold, sim_gate):
            async with SessionLocal() as db:
                t = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
                g = await db.get(PolicyConfig, "matching.ai.similarity_autoresolve_min")
                if threshold is None:
                    original = (float(t.value), float(g.value))
                    t.value = 0.10
                    g.value = 0.15  # hashing double produces low similarities; 0.15 keeps S2 eligible, blocks Pl4tFrmZ
                    await db.commit()
                    return original
                t.value = threshold
                g.value = sim_gate
                await db.commit()

        original_t, original_g = asyncio.run(set_semantic_policy(None, None))
        try:
            asyncio.run(_run(ids))
            data2 = client.get(
                "/api/v1/analytics/overview",
                params={"from": week_ago.isoformat(), "to": today.isoformat()},
            ).json()
            types2 = {t["match_type"]: t["count"] for t in data2["by_match_type"]}
            assert types2.get("semantic", 0) >= 1, f"semantic proposals must register: {types2}"
            assert types2.get("ai", 0) >= 1, "S2 auto-resolve is an AI match"
            assert data2["resolution_split"]["auto"] >= 4
            # both runs land in today's bucket
            assert data2["buckets"][-1]["matches_created"] > data["buckets"][-1]["matches_created"]
        finally:
            asyncio.run(set_semantic_policy(original_t, original_g))

        # range filtering excludes everything when scoped to the far future
        empty = client.get(
            "/api/v1/analytics/overview",
            params={"from": "9999-01-01", "to": "9999-01-07"},
        ).json()
        assert all(b["matches_created"] == 0 and b["exceptions_opened"] == 0 for b in empty["buckets"])
