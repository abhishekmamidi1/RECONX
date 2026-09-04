"""Golden acceptance tests — the permanent regression gate for matchers.

Dataset: backend/sample_data/{razorpay_settlement_test,bank_statement_test,erp_transaction_test}.csv
Fixture integrity is verified separately in test_golden_dataset.py.

Ground truth (locked with product):
    pay_Gg7h8I9j / INV-2026-0710 / UTR888170260810  -> deterministic exact (3-way)
    pay_Ii9j0K1l / INV-2026-0712 / UTR888170260812  -> deterministic exact (3-way)
    pay_Kk1l2M3n / INV-2026-0714 / UTR888170260814  -> deterministic exact (3-way)
    pay_Hh8i9J0k / INV-2026-0711 / UTR888170260811X -> fuzzy only (bank ref typo'd)
    pay_Sm1Th1cA / INV-2026-0720                    -> semantic + AI match
      (no shared refs, materially different wording, near-equal economics;
       the heuristic agent resolves it as a confident match)
    pay_Pl4tFrmZ / INV-2026-0721                    -> semantic candidate, AI says
      needs_human -> manual_review_required exception, never auto-resolved
    pay_Jj0k1L2m / UTR888170260813                  -> missing ERP leg -> exception
    CHG-Q2-FY2702 (bank charge debit)               -> inert: no match, no exception

Pipeline contract pinned by these tests:

    from app.services.reconciliation import run_reconciliation

    summary = await run_reconciliation(db, actor=..., transaction_ids=[...])

  - Scope-limited: processes exactly the supplied transaction ids.
  - Deterministic proposals passing the confidence/materiality gates are
    auto-resolved (matches.status='confirmed', resolved_by='auto').
  - Semantic similarity alone never resolves; only an AI 'match' decision
    clearing gate.ai_min_confidence_autoresolve AND materiality confirms.
  - Every AI evaluation writes an 'ai.decision' audit row with the full
    rationale text.
  - Re-running over an already-reconciled scope must be idempotent: no
    duplicate matches or exceptions.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    ExceptionRecord,
    Match,
    MatchParticipant,
    PolicyConfig,
    Transaction,
)

reconciliation = pytest.importorskip(
    "app.services.reconciliation",
    reason="activates automatically once the Phase 2 reconciliation pipeline exists",
)

EXACT_TRIOS = [
    ("pay_Gg7h8I9j", "INV-2026-0710", "UTR888170260810"),
    ("pay_Ii9j0K1l", "INV-2026-0712", "UTR888170260812"),
    ("pay_Kk1l2M3n", "INV-2026-0714", "UTR888170260814"),
]
FUZZY_PAIR = ("pay_Hh8i9J0k", "INV-2026-0711", "UTR888170260811X")
SEMANTIC_MATCH_PAIR = ("pay_Sm1Th1cA", "INV-2026-0720")
SEMANTIC_HUMAN_PAIR = ("pay_Pl4tFrmZ", "INV-2026-0721")
MISSING_ERP_REFS = ("pay_Jj0k1L2m", "UTR888170260813")
INERT_REFS = ("CHG-Q2-FY2702",)

ALL_REFS = [ref for trio in EXACT_TRIOS for ref in trio] + [
    ref for ref in FUZZY_PAIR
] + list(SEMANTIC_MATCH_PAIR) + list(SEMANTIC_HUMAN_PAIR) + list(MISSING_ERP_REFS) + list(INERT_REFS)


async def _load_scope():
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Transaction).where(Transaction.external_ref.in_(ALL_REFS))
            )
        ).scalars().all()
        found = {t.external_ref for t in rows}
        missing = set(ALL_REFS) - found
        assert not missing, (
            "test dataset not ingested; upload razorpay_settlement_test.csv, "
            f"bank_statement_test.csv, erp_transaction_test.csv. Missing: {sorted(missing)}"
        )
        by_ref = {t.external_ref: t.id for t in rows}
        threshold_row = await db.get(PolicyConfig, "matching.fuzzy.score_threshold")
        return by_ref, float(threshold_row.value)


async def _participants_for(db, match_id):
    rows = await db.execute(
        select(Transaction.external_ref)
        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
        .where(MatchParticipant.match_id == match_id)
    )
    return {r for r in rows.scalars().all()}


async def _purge_scope_state(db, refs):
    rows = (
        await db.execute(select(Transaction.id).where(Transaction.external_ref.in_(refs)))
    ).scalars().all()
    match_ids = (
        await db.execute(
            select(MatchParticipant.match_id).where(
                MatchParticipant.transaction_id.in_(rows)
            )
        )
    ).scalars().all()
    await db.execute(delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(rows)))
    await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
    await db.execute(delete(Match).where(Match.id.in_(match_ids)))
    return rows


class TestGoldenReconciliation:
    def test_ground_truth(self):
        async def scenario():
            by_ref, fuzzy_threshold = await _load_scope()
            scope_ids = [by_ref[ref] for ref in ALL_REFS if ref not in INERT_REFS]

            async with SessionLocal() as db:
                await _purge_scope_state(db, list(by_ref))
                threshold_row = await db.get(
                    PolicyConfig, "matching.semantic.similarity_threshold"
                )
                original_threshold = threshold_row.value
                threshold_row.value = 0.10
                sim_gate_row = await db.get(
                    PolicyConfig, "matching.ai.similarity_autoresolve_min"
                )
                original_sim_gate = sim_gate_row.value
                # Hermetic env uses the hashing double, whose similarities are
                # far lower than real BGE-m3 (0.27 vs 0.66 on the genuine pair).
                # 0.15 keeps pay_Sm1Th1cA (0.27) eligible and forces
                # pay_Pl4tFrmZ (0.11) to human review under the gate.
                sim_gate_row.value = 0.15
                await db.commit()

            try:
                async with SessionLocal() as db:
                    summary = await reconciliation.run_reconciliation(
                        db, actor="pytest-golden", transaction_ids=scope_ids
                    )
                    await db.commit()
            finally:
                async with SessionLocal() as db:
                    restore_row = await db.get(
                        PolicyConfig, "matching.semantic.similarity_threshold"
                    )
                    restore_row.value = original_threshold
                    sim_gate_row = await db.get(
                        PolicyConfig, "matching.ai.similarity_autoresolve_min"
                    )
                    sim_gate_row.value = original_sim_gate
                    await db.commit()

            async with SessionLocal() as db:
                matches = (
                    await db.execute(
                        select(Match)
                        .join(
                            MatchParticipant,
                            MatchParticipant.match_id == Match.id,
                        )
                        .where(MatchParticipant.transaction_id.in_(scope_ids))
                    )
                ).scalars().unique().all()

                exact = [
                    m
                    for m in matches
                    if m.match_type == "deterministic"
                    and m.status == "confirmed"
                    and m.resolved_by == "auto"
                ]
                assert len(exact) == 3, (
                    f"expected 3 exact auto-resolves, got {len(exact)}"
                )
                for payment_ref, invoice_no, utr in EXACT_TRIOS:
                    covering = []
                    for match in exact:
                        participants = await _participants_for(db, match.id)
                        if {payment_ref, invoice_no, utr} <= participants:
                            covering.append(match)
                    assert len(covering) >= 1, f"no exact match covers {payment_ref}"
                    assert covering[0].confidence_score == Decimal("1.0000")

                fuzzy = [m for m in matches if m.match_type == "fuzzy"]
                assert len(fuzzy) == 1, (
                    f"expected exactly 1 fuzzy match, got {len(fuzzy)}"
                )
                fm = fuzzy[0]
                assert Decimal(str(fm.confidence_score)) > Decimal(
                    str(fuzzy_threshold / 100)
                )
                assert Decimal(str(fm.confidence_score)) < 1
                participants = await _participants_for(db, fm.id)
                assert {"pay_Hh8i9J0k", "UTR888170260811X"} <= participants

                ai_confirmed = [
                    m
                    for m in matches
                    if m.match_type == "ai" and m.status == "confirmed"
                ]
                assert len(ai_confirmed) == 1, (
                    f"expected exactly 1 AI-confirmed match, got {len(ai_confirmed)}"
                )
                ai_match = ai_confirmed[0]
                s1_participants = await _participants_for(db, ai_match.id)
                assert set(SEMANTIC_MATCH_PAIR) <= s1_participants
                assert Decimal(str(ai_match.confidence_score)) >= Decimal("0.90")
                assert "AI (heuristic-offline)" in (ai_match.rationale or "")

                ai_decision_rows = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.action == "ai.decision",
                            AuditLog.entity_id.in_(
                                [by_ref[ref] for ref in SEMANTIC_MATCH_PAIR]
                            ),
                        )
                    )
                ).scalars().all()
                assert ai_decision_rows, "semantic evaluation must leave an audit trail"
                assert all(row.details.get("rationale") for row in ai_decision_rows)

                exceptions = (
                    await db.execute(
                        select(ExceptionRecord).where(
                            ExceptionRecord.transaction_id.in_(
                                scope_ids + [by_ref[ref] for ref in INERT_REFS]
                            )
                        )
                    )
                ).scalars().all()
                unmatched = [
                    e for e in exceptions if e.exception_type == "unmatched"
                ]
                assert len(unmatched) == 1, (
                    f"expected exactly 1 unmatched exception, got {len(unmatched)}"
                )
                assert unmatched[0].transaction_id == by_ref["pay_Jj0k1L2m"]

                review = [
                    e
                    for e in exceptions
                    if e.exception_type == "manual_review_required"
                ]
                assert len(review) == 1, (
                    f"expected exactly 1 manual_review_required, got {len(review)}"
                )
                assert review[0].transaction_id == by_ref["pay_Pl4tFrmZ"]

                charge_id = by_ref["CHG-Q2-FY2702"]
                charge_exceptions = [
                    e for e in exceptions if e.transaction_id == charge_id
                ]
                assert not charge_exceptions, "bank charge must not raise exceptions"

                charge_in_matches = await db.execute(
                    select(MatchParticipant).where(
                        MatchParticipant.transaction_id == charge_id
                    )
                )
                assert charge_in_matches.first() is None

            return summary

        summary = asyncio.run(scenario())
        assert summary["exact_auto_resolved"] == 3
        assert summary["fuzzy_auto_resolved"] == 1
        assert summary["ai_auto_resolved"] == 1
        assert summary["ai_candidates_evaluated"] >= 2
        assert summary["exceptions_opened"] == 2
