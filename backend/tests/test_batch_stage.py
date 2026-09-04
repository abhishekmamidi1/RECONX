"""Many-to-one batch grouping stage tests.

Covers, in order:
  - unit: batch_candidate_groups() (pure subset-sum generation)
  - unit: HeuristicReasoningAgent.decide_batch() decision rules
  - integration: exact aggregated payout confirmed on a fresh synthetic scope
  - integration: exact-sum with no corroborating signal is NEVER auto-resolved
    (coincidence risk -> needs_human -> manual_review_required exception)
  - integration: idempotency - a second run duplicates nothing
  - real-case golden: UTR888150260806 (the Phase-1 open AGGREGATED PAYOUT)
    stays an open unmatched exception after a pipeline run
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    ExceptionRecord,
    Match,
    MatchParticipant,
    Transaction,
)

reconciliation = pytest.importorskip(
    "app.services.reconciliation",
    reason="activates automatically once the Phase 2 reconciliation pipeline exists",
)

from app.services.reasoning.heuristic_agent import HeuristicReasoningAgent
from app.services.reconciliation.engine import batch_candidate_groups

_BASE = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

BATCH_POLICY = {
    "matching.batch.enabled": True,
    "matching.batch.max_components": 10,
    "matching.batch.date_window_days": 3,
    "matching.batch.amount_tolerance_pct": 0,
}


def _txn(
    source: str,
    ref: str,
    amount: str,
    days_from_base: int = 0,
    narration: str = "",
    direction: str = "credit",
) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        source=source,
        external_ref=ref,
        amount=Decimal(amount),
        direction=direction,
        currency="INR",
        txn_date=_BASE + timedelta(days=days_from_base),
        narration=narration,
        counterparty=None,
        status=None,
        raw={},
    )


def _bank(ref: str, amount: str, narration: str) -> Transaction:
    return _txn("bank", ref, amount, narration=narration)


# --------------------------------------------------------------------------- #
# 1) candidate-generation units                                               #
# --------------------------------------------------------------------------- #
class TestBatchCandidateGeneration:
    def test_exact_subset_group(self):
        bank = _bank("B1", "10000.00", "NEFT-CR UTR X AGGREGATED PAYOUT")
        components = [
            _txn("razorpay", "R1", "4500.00"),
            _txn("razorpay", "R2", "3500.00"),
            _txn("razorpay", "R3", "2000.00"),
            _txn("erp", "E1", "999.00"),
            _txn("razorpay", "R4", "4500.00", days_from_base=5),
        ]
        groups = batch_candidate_groups(bank, components, BATCH_POLICY)
        assert len(groups) == 1
        group = groups[0]
        assert group.total == Decimal("10000.00")
        assert group.residual == 0
        assert {c.external_ref for c in group.members} == {"R1", "R2", "R3"}

    def test_window_and_max_components(self):
        bank = _bank("B2", "100.00", "AGGREGATED PAYOUT")
        components = [
            _txn("razorpay", "R-A", "60.00"),
            _txn("razorpay", "R-B", "40.00"),
            _txn("razorpay", "R-C", "30.00"),
            _txn("razorpay", "R-D", "30.00"),
            _txn("razorpay", "R-E", "40.00", days_from_base=5),
        ]
        capped = {**BATCH_POLICY, "matching.batch.max_components": 2}
        with_2 = batch_candidate_groups(bank, components, capped)
        assert len(with_2) == 1
        assert {c.external_ref for c in with_2[0].members} == {"R-A", "R-B"}
        with_default = batch_candidate_groups(bank, components, BATCH_POLICY)
        sizes = sorted(len(g.members) for g in with_default)
        assert (2, 3) == tuple(sizes[0:2])
        assert all(c.external_ref != "R-E" for g in with_default for c in g.members)

    def test_ranking_fewest_components_then_residual(self):
        tolerant = {**BATCH_POLICY, "matching.batch.amount_tolerance_pct": 1}
        bank = _bank("B3", "100.00", "PAYOUT")
        components = [
            _txn("razorpay", "R1", "60.00"),
            _txn("razorpay", "R2", "40.00"),
            _txn("razorpay", "R3", "60.50"),
            _txn("razorpay", "R4", "30.00"),
            _txn("razorpay", "R5", "30.00"),
        ]
        groups = batch_candidate_groups(bank, components, tolerant)
        assert len(groups) >= 2
        first, second = groups[0], groups[1]
        assert len(first.members) == 2 and first.residual == 0
        assert set(first.members) == set(second.members) or first.residual <= second.residual
        assert sum(c.amount for c in first.members) == Decimal("100.00")

    def test_zero_tolerance_rejects_inexact_sums(self):
        bank = _bank("B4", "100.00", "PAYOUT")
        components = [_txn("razorpay", "R1", "60.50"), _txn("razorpay", "R2", "40.00")]
        groups = batch_candidate_groups(bank, components, BATCH_POLICY)
        assert groups == []

    def test_min_two_components_required(self):
        bank = _bank("B5", "500.00", "PAYOUT")
        assert batch_candidate_groups(bank, [_txn("razorpay", "R1", "500.00")], BATCH_POLICY) == []
        assert batch_candidate_groups(bank, [], BATCH_POLICY) == []

    def test_non_bank_target_rejected_and_debits_excluded(self):
        razorpay_target = _txn("razorpay", "R-T", "100.00")
        assert batch_candidate_groups(razorpay_target, [_txn("razorpay", "R1", "60.00"), _txn("razorpay", "R2", "40.00")], BATCH_POLICY) == []
        from_date_only = _txn("bank", "B6", "100.00", direction="credit")
        debit = _txn("razorpay", "R-DEBIT", "100.00", direction="debit")
        groups = batch_candidate_groups(from_date_only, [debit, _txn("razorpay", "R2", "60.00")], BATCH_POLICY)
        assert groups == []


# --------------------------------------------------------------------------- #
# 2) heuristic decision units                                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture
def agent():
    return HeuristicReasoningAgent()


class TestHeuristicDecideBatch:
    def test_exact_full_signal_is_match(self, agent):
        async def scenario():
            bank = _bank("B1", "45000.00", "NEFT-CR UTR888 AGGREGATED PAYOUT")
            comps = [_txn("razorpay", f"R{i}", amt) for i, amt in enumerate(["20000.00", "15000.00", "10000.00"])]
            return await agent.decide_batch(bank, comps, {})

        decision = asyncio.run(scenario())
        assert decision.decision == "match"
        assert decision.confidence == 0.93

    def test_exact_two_component_match(self, agent):
        async def scenario():
            bank = _bank("B2", "30000.00", "NEFT-CR UTR AGGREGATED PAYOUT")
            comps = [_txn("razorpay", f"R{i}", amt) for i, amt in enumerate(["20000.00", "10000.00"])]
            return await agent.decide_batch(bank, comps, {})

        decision = asyncio.run(scenario())
        assert decision.decision == "match"
        assert decision.confidence == 0.91

    def test_inexact_is_needs_human(self, agent):
        async def scenario():
            bank = _bank("B3", "30000.00", "NEFT-CR UTR AGGREGATED PAYOUT")
            comps = [_txn("razorpay", f"R{i}", amt) for i, amt in enumerate(["20000.00", "10000.00"])]
            comps[1].amount = Decimal("9999.00")
            return await agent.decide_batch(bank, comps, {})

        decision = asyncio.run(scenario())
        assert decision.decision == "needs_human"
        assert decision.confidence == 0.55

    def test_exact_partial_signal_is_needs_human(self, agent):
        async def scenario():
            bank = _bank("B4", "30000.00", "NEFT-CR UTR AGGREGATED PAYOUT")
            comps = [
                _txn("razorpay", "R1", "20000.00"),
                _txn("erp", "E1", "10000.00"),
            ]
            return await agent.decide_batch(bank, comps, {})

        decision = asyncio.run(scenario())
        assert decision.decision == "needs_human"
        assert decision.confidence == 0.60

    def test_exact_no_signal_never_auto_resolves(self, agent):
        async def scenario():
            bank = _bank("B5", "30000.00", "NEFT-CR VENDOR PAYMENT")
            comps = [
                _txn("razorpay", "R1", "20000.00"),
                _txn("erp", "E1", "10000.00"),
            ]
            return await agent.decide_batch(bank, comps, {})

        decision = asyncio.run(scenario())
        assert decision.decision == "needs_human"
        assert decision.confidence == 0.50


# --------------------------------------------------------------------------- #
# 3-5) integration scenarios                                                     #
# --------------------------------------------------------------------------- #
async def _delete_transactions(db, refs):
    rows = list(
        (await db.execute(select(Transaction.id).where(Transaction.external_ref.in_(refs))))
        .scalars()
        .all()
    )
    if not rows:
        return
    match_ids = list(
        (
            await db.execute(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.transaction_id.in_(rows)
                )
            )
        )
        .scalars()
        .all()
    )
    await db.execute(delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(rows)))
    await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
    await db.execute(delete(Match).where(Match.id.in_(match_ids)))
    await db.execute(delete(AuditLog).where(AuditLog.entity_id.in_(rows + match_ids)))
    await db.execute(delete(Transaction).where(Transaction.id.in_(rows)))


def _insert_new(db, txns):
    for t in txns:
        db.add(t)


async def _purge_matches_and_exceptions(db, rows):
    match_ids = list(
        (
            await db.execute(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.transaction_id.in_(rows)
                )
            )
        )
        .scalars()
        .all()
    )
    await db.execute(delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(rows)))
    await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
    await db.execute(delete(Match).where(Match.id.in_(match_ids)))


class TestBatchIntegration:
    def test_aggregated_payout_is_confirmed_and_audited(self):
        async def scenario():
            refs = ["SYN-B1", "SYN-R1", "SYN-R2", "SYN-R3"]
            rows = [
                _bank("SYN-B1", "45000.00", "NEFT-CR MOCK AGGREGATED PAYOUT"),
                _txn("razorpay", "SYN-R1", "20000.00"),
                _txn("razorpay", "SYN-R2", "15000.00"),
                _txn("razorpay", "SYN-R3", "10000.00", days_from_base=1),
            ]
            async with SessionLocal() as db:
                await _delete_transactions(db, refs)
                _insert_new(db, rows)
                await db.commit()
                scoped = [r.id for r in rows]
                await db.commit()

                summary1 = await reconciliation.run_reconciliation(
                    db, actor="pytest-batch", transaction_ids=scoped
                )
                await db.commit()

                matches = (
                    await db.execute(
                        select(Match)
                        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                        .where(MatchParticipant.transaction_id.in_(scoped))
                    )
                ).scalars().unique().all()
                batch_matches = [m for m in matches if m.match_type == "batch"]
                assert len(batch_matches) == 1
                bm = batch_matches[0]
                assert bm.status == "confirmed"
                assert bm.resolved_by == "auto"
                participants = (
                    await db.execute(
                        select(Transaction.external_ref, MatchParticipant.role)
                        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
                        .where(MatchParticipant.match_id == bm.id)
                    )
                ).all()
                roles = dict(participants)
                assert roles["SYN-B1"] == "primary"
                assert {roles[r] for r in ("SYN-R1", "SYN-R2", "SYN-R3")} == {"participant"}

                audit = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.action == "ai.decision",
                            AuditLog.entity_id == scoped[0],
                        )
                    )
                ).scalars().all()
                assert audit
                assert all(len(row.details.get("candidates")) == 3 for row in audit)
                assert all(row.details.get("residual") == "0" for row in audit)

                summary2 = await reconciliation.run_reconciliation(
                    db, actor="pytest-batch", transaction_ids=scoped
                )
                await db.commit()
                after = (
                    await db.execute(
                        select(Match)
                        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                        .where(MatchParticipant.transaction_id.in_(scoped))
                    )
                ).scalars().unique().all()
                assert [m.match_type for m in after].count("batch") == 1, (
                    "second run must not duplicate the batch match"
                )

                assert summary1["batch_candidates_generated"] >= 1
                assert summary1["batch_ai_evaluated"] >= 1
                assert summary1["batch_auto_resolved"] == 1
                assert summary1["batch_proposed"] == 0
                assert summary2["batch_auto_resolved"] == 0

            async with SessionLocal() as db:
                await _delete_transactions(db, refs)
                await db.commit()

        asyncio.run(scenario())

    def test_exact_sum_without_signal_is_never_auto_resolved(self):
        async def scenario():
            refs = ["SYN-B2", "SYN-R4", "SYN-R5"]
            rows = [
                _bank("SYN-B2", "30000.00", "NEFT-CR VENDOR PAYMENT"),
                _txn("razorpay", "SYN-R4", "20000.00"),
                _txn("razorpay", "SYN-R5", "10000.00"),
            ]
            async with SessionLocal() as db:
                await _delete_transactions(db, refs)
                _insert_new(db, rows)
                await db.commit()
                scoped = [r.id for r in rows]
                summary = await reconciliation.run_reconciliation(
                    db, actor="pytest-batch", transaction_ids=scoped
                )
                await db.commit()

                matches = (
                    await db.execute(
                        select(Match)
                        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                        .where(MatchParticipant.transaction_id.in_(scoped))
                    )
                ).scalars().unique().all()
                batch_matches = [m for m in matches if m.match_type == "batch"]
                assert len(batch_matches) == 1
                assert batch_matches[0].status == "proposed"
                exceptions = (
                    await db.execute(
                        select(ExceptionRecord).where(
                            ExceptionRecord.transaction_id.in_(scoped)
                        )
                    )
                ).scalars().all()
                assert any(e.exception_type == "manual_review_required" for e in exceptions)
                assert summary["batch_proposed"] == 1
                assert summary["batch_auto_resolved"] == 0

            async with SessionLocal() as db:
                await _delete_transactions(db, refs)
                await db.commit()

        asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 6) real-case golden: UTR888150260806 stays open unmatched                   #
# --------------------------------------------------------------------------- #
class TestRealCaseUtR888150260806:
    def test_aggregated_payout_remains_an_open_exception(self):
        async def scenario():
            refs = ["UTR888150260806", "INV-2026-0707"]
            async with SessionLocal() as db:
                found = (
                    await db.execute(
                        select(Transaction).where(Transaction.external_ref.in_(refs))
                    )
                ).scalars().all()
                assert len(found) == 2, (
                    "real-case dataset not present in DB; expected "
                    "UTR888150260806 + INV-2026-0707"
                )
                by_ref = {t.external_ref: t.id for t in found}
                await _purge_matches_and_exceptions(db, list(by_ref.values()))
                await db.commit()
                scope_ids = [by_ref[r] for r in refs]

                summary = await reconciliation.run_reconciliation(
                    db, actor="pytest-realcase", transaction_ids=scope_ids
                )
                await db.commit()

                utr_exceptions = (
                    await db.execute(
                        select(ExceptionRecord).where(
                            ExceptionRecord.transaction_id == by_ref["UTR888150260806"]
                        )
                    )
                ).scalars().all()
                assert utr_exceptions, "UTR888150260806 must surface as an exception"
                assert all(e.status == "open" for e in utr_exceptions)

                batch_matches = (
                    await db.execute(
                        select(Match)
                        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                        .where(
                            MatchParticipant.transaction_id.in_(scope_ids),
                            Match.match_type == "batch",
                        )
                    )
                ).scalars().unique().all()
                assert batch_matches == [], (
                    "no exact subset of available components sums to 122701.95; "
                    "the AGGREGATED PAYOUT must NOT be fabricated into a match"
                )
                assert summary["batch_auto_resolved"] == 0

                # Direct generation check: with only INV-2026-0707 leftover in
                # the window, the pool has fewer than 2 components -> no groups.
                bank = next(t for t in found if t.external_ref == "UTR888150260806")
                erp = next(t for t in found if t.external_ref == "INV-2026-0707")
                policy = {
                    "matching.batch.date_window_days": 3,
                    "matching.batch.amount_tolerance_pct": 0,
                    "matching.batch.max_components": 10,
                }
                assert batch_candidate_groups(bank, [erp], policy) == []

            return summary, by_ref

        summary, by_ref = asyncio.run(scenario())
        assert summary["batch_candidates_generated"] >= 0
        assert by_ref["UTR888150260806"]