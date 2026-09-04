"""Idempotency regression: re-running the pipeline must never duplicate
exceptions or proposed matches for an already-excepted transaction.

Regression context: _reset_proposed_state originally reclaimed only
'unmatched'/'amount_mismatch' exceptions, so AI-stage types
('manual_review_required', 'low_confidence_ai') accumulated one duplicate
per re-run for pay_Pl4tFrmZ.
"""

import asyncio

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models import ExceptionRecord, Match, MatchParticipant, PolicyConfig, Transaction

reconciliation = pytest.importorskip(
    "app.services.reconciliation",
    reason="activates automatically once the Phase 2 reconciliation pipeline exists",
)

SCOPE_REFS = [
    "pay_Pl4tFrmZ",
    "INV-2026-0721",
    "pay_Jj0k1L2m",
    "UTR888170260813",
    "CHG-Q2-FY2702",
]


async def _load_scope():
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Transaction).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        found = {t.external_ref for t in rows}
        missing = set(SCOPE_REFS) - found
        assert not missing, f"test dataset not ingested, missing {sorted(missing)}"
        return {t.external_ref: t.id for t in rows}


async def _purge(db, by_ref):
    ids = list(by_ref.values())
    match_ids = (
        await db.execute(
            select(MatchParticipant.match_id).where(
                MatchParticipant.transaction_id.in_(ids)
            )
        )
    ).scalars().all()
    await db.execute(
        delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(ids))
    )
    await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
    await db.execute(delete(Match).where(Match.id.in_(match_ids)))


async def _snapshot(db, by_ref):
    exceptions = (
        await db.execute(
            select(ExceptionRecord, Transaction.external_ref)
            .join(Transaction, Transaction.id == ExceptionRecord.transaction_id)
            .where(ExceptionRecord.transaction_id.in_(list(by_ref.values())))
            .order_by(ExceptionRecord.id)
        )
    ).all()
    exception_state = sorted(
        (ref, e.exception_type, e.status) for e, ref in exceptions
    )

    matches = (
        await db.execute(
            select(Match)
            .join(MatchParticipant, MatchParticipant.match_id == Match.id)
            .where(MatchParticipant.transaction_id.in_(list(by_ref.values())))
        )
    ).scalars().unique().all()
    match_state = sorted((m.match_type, m.status) for m in matches)

    return {"exceptions": exception_state, "matches": match_state}


async def _global_counts(db):
    from sqlalchemy import func

    total_matches = await db.scalar(select(func.count()).select_from(Match))
    total_proposed = (
        await db.execute(
            select(func.count()).select_from(Match).where(Match.status == "proposed")
        )
    ).scalar_one()
    with_participants = (
        await db.execute(
            select(func.count())
            .select_from(Match)
            .where(
                Match.status == "proposed",
                Match.id.in_(select(MatchParticipant.match_id)),
            )
        )
    ).scalar_one()
    total_exceptions = await db.scalar(select(func.count()).select_from(ExceptionRecord))
    return {
        "matches_total": int(total_matches),
        "proposed_total": int(total_proposed),
        "orphaned_proposals": int(total_proposed) - int(with_participants),
        "exceptions_total": int(total_exceptions),
    }


class TestReconciliationIdempotency:
    def test_repeated_runs_do_not_duplicate_exceptions_or_matches(self):
        async def scenario():
            by_ref = await _load_scope()
            threshold_row_state = {}

            async with SessionLocal() as db:
                await _purge(db, by_ref)
                row = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
                threshold_row_state["original"] = row.value
                row.value = 0.10
                await db.commit()

            try:
                runs = []
                globals_ = []
                for _ in range(2):
                    async with SessionLocal() as db:
                        await reconciliation.run_reconciliation(
                            db,
                            actor="pytest-idempotency",
                            transaction_ids=[by_ref[r] for r in SCOPE_REFS if r != "CHG-Q2-FY2702"],
                        )
                        await db.commit()
                    async with SessionLocal() as db:
                        runs.append(await _snapshot(db, by_ref))
                        globals_.append(await _global_counts(db))
            finally:
                async with SessionLocal() as db:
                    restore = await db.get(
                        PolicyConfig, "matching.semantic.similarity_threshold"
                    )
                    restore.value = threshold_row_state["original"]
                    await db.commit()

            assert runs[0] == runs[1], (
                f"second run diverged from first:\nrun1={runs[0]}\nrun2={runs[1]}"
            )
            assert globals_[0] == globals_[1], (
                f"global row counts grew across identical re-runs:\n"
                f"after run1={globals_[0]}\nafter run2={globals_[1]}"
            )
            assert globals_[0]["orphaned_proposals"] == 0, (
                "no proposed match may exist without its participants"
            )

            manual_reviews = [
                e for e in runs[1]["exceptions"] if e[1] == "manual_review_required"
            ]
            assert len(manual_reviews) == 1, (
                f"expected exactly 1 manual_review_required, got {manual_reviews}"
            )
            unmatched = [e for e in runs[1]["exceptions"] if e[1] == "unmatched"]
            assert len(unmatched) == 1
            return runs[1]

        final_state = asyncio.run(scenario())
        assert ("pay_Pl4tFrmZ", "manual_review_required", "open") in final_state["exceptions"]
        assert final_state["matches"], "expected at least one live proposal after run"
