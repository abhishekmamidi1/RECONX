"""Golden close-pass acceptance test over the 61-record synthetic batch.

Dataset: backend/sample_data/close_{razorpay_settlements,bank_statement,
erp_transactions}.csv (61 records) with locked labels in
backend/sample_data/close_ground_truth.csv.

Ground truth (verified hermetic outcomes, 42 matched / 3 no_match / 16 deferred):

* 30 trio records: determinant pairs pay_cL9qW2mN0..N9 / INV-2026-0901..0910 /
  UTR88909260001..0010 -> matched
* 6 fuzzy records: pay_fzQ3r8T5a+b / INV-2026-0911/0912 / UTR889092600F1X/F2X
  (bank UTR typo'd) -> matched
* 4 aggregated: pay_ag2P9c6X1..3 + UTR889092600AG -> matched
* 2 semantic auto-resolve: pay_smK0lP2qR / INV-2026-0913 -> matched
* 3 bank charges: CHG-SEP-FY2801..03 -> no_match
* 16 deferred:
    no_candidate x8       pay_eo7Q2t9X1/E1, pay_eo7Q2t9X2/E2 (missing ERP),
                          pay_mx0K9p3Z1, pay_dq1W3e5R7, INV-2026-0917/0918
    below_threshold x2    pay_plM4nR6qT / INV-2026-0914 (similarity floor)
    materiality x2        pay_mA9c3F5x1 / INV-2026-0916 (5% amount gap)
    manual_investigation  pay_rf9X7a4B2-REFUND / UTR889092600R3 /
    x3                    INV-2026-0915 (refunds)
    data_quality x1       UTR889092600D1 (blank narration)
"""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, select

from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    ExceptionRecord,
    Match,
    MatchParticipant,
    PolicyConfig,
    Transaction,
)
from app.services.controller import close_reconciliation
from app.services.parsers import (
    BankStatementParser,
    ErpTransactionParser,
    RazorpaySettlementParser,
)
from app.services.reconciliation import run_reconciliation

reconciliation = pytest.importorskip(
    "app.services.reconciliation",
    reason="activates automatically once the Phase 2 reconciliation pipeline exists",
)

_SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"

_BATCH_FILES = {
    "razorpay": "close_razorpay_settlements.csv",
    "bank": "close_bank_statement.csv",
    "erp": "close_erp_transactions.csv",
}
_LABELS_FILE = "close_ground_truth.csv"


def _load_labels():
    labels: dict[str, dict] = {}
    with open(_SAMPLE_DIR / _LABELS_FILE, encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        for line in fh:
            if not line.strip():
                continue
            values = line.rstrip("\n").split(",")
            row = dict(zip(header, values))
            labels[row["record_id"]] = row
    return labels


def _parse_batch():
    parsers = {
        "razorpay": RazorpaySettlementParser(),
        "bank": BankStatementParser(),
        "erp": ErpTransactionParser(),
    }
    drafts = []
    for source, fname in _BATCH_FILES.items():
        result = parsers[source].parse((_SAMPLE_DIR / fname).read_bytes())
        assert result.rows_rejected == 0, f"{fname} has rejections"
        drafts.append((source, result.drafts))
    return drafts


def _refs_in_batch():
    refs = set()
    for source, drafts in _parse_batch():
        refs.update(d.external_ref for d in drafts)
    return refs


async def _ingest_batch(db):
    for source, drafts in _parse_batch():
        existing = set(
            (
                await db.execute(
                    select(Transaction.external_ref).where(
                        Transaction.source == source,
                        Transaction.external_ref.in_([d.external_ref for d in drafts]),
                    )
                )
            ).scalars()
        )
        for draft in drafts:
            if draft.external_ref in existing:
                continue
            await db.execute(
                insert(Transaction).values(
                    ingestion_id=None,
                    source=draft.source,
                    external_ref=draft.external_ref,
                    amount=draft.amount,
                    direction=draft.direction,
                    transaction_type=draft.transaction_type,
                    currency=draft.currency,
                    txn_date=draft.txn_date,
                    narration=draft.narration,
                    counterparty=draft.counterparty,
                    status=draft.status,
                    raw=draft.raw,
                )
            )
    await db.commit()


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
    await db.commit()
    return list(rows)


class TestCloseGoldenDataset:
    def test_batch_files_are_well_formed(self):
        for source, drafts in _parse_batch():
            refs = {d.external_ref for d in drafts}
            assert len(refs) == len(drafts), f"{source} has duplicate refs"
        refs = _refs_in_batch()
        assert len(refs) == 61, f"batch must have exactly 61 records, got {len(refs)}"

    def test_ground_truth_labels_coverage(self):
        labels = _load_labels()
        assert len(labels) == 61
        assert set(labels) == _refs_in_batch()

    def test_close_roundtrip_matches_labels(self):
        async def scenario():
            labels = _load_labels()
            refs = _refs_in_batch()

            async with SessionLocal() as db:
                await _ingest_batch(db)
                scope_ids = await _purge_scope_state(db, refs)

                threshold_row = await db.get(
                    PolicyConfig, "matching.semantic.similarity_threshold"
                )
                original_threshold = threshold_row.value
                threshold_row.value = 0.10
                sim_gate_row = await db.get(
                    PolicyConfig, "matching.ai.similarity_autoresolve_min"
                )
                original_sim_gate = sim_gate_row.value
                sim_gate_row.value = 0.15
                await db.commit()

            try:
                async with SessionLocal() as db:
                    await run_reconciliation(
                        db, actor="pytest-close-golden", transaction_ids=scope_ids
                    )
                    await db.commit()

                async with SessionLocal() as db:
                    result = await close_reconciliation(
                        db, actor="pytest-close-golden", transaction_ids=scope_ids
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

            decisions = {d["record_ref"]: d for d in result["decisions"]}
            assert set(decisions) == set(labels), (
                "close pass must decide exactly the labeled records"
            )

            decision_mismatches = []
            reason_mismatches = []
            pair_mismatches = []
            for record_ref, label in labels.items():
                decision = decisions[record_ref]
                expected_result = label["expected_result"]
                if decision["decision"] != expected_result:
                    decision_mismatches.append(
                        (record_ref, decision["decision"], expected_result)
                    )
                if expected_result == "deferred":
                    expected_reason = label["expected_exception_reason"] or "manual_investigation"
                    if decision["reason_code"] != expected_reason:
                        reason_mismatches.append(
                            (record_ref, decision["reason_code"], expected_reason)
                        )
                if expected_result == "matched":
                    expected_pairs = {
                        r for r in label["expected_reference_pair"].split(";") if r
                    }
                    matched_with = set(decision["matched_with"] or [])
                    if not expected_pairs.issubset(matched_with):
                        pair_mismatches.append(
                            (record_ref, sorted(matched_with), sorted(expected_pairs))
                        )

            assert not decision_mismatches, f"decision mismatches: {decision_mismatches}"
            assert not reason_mismatches, f"reason mismatches: {reason_mismatches}"
            assert not pair_mismatches, f"pair mismatches: {pair_mismatches}"

            assert result["records_scanned"] == 61
            assert result["matched"] == 42
            assert result["no_match"] == 3
            assert result["deferred"] == 16
            assert result["deferred_by_reason"]["no_candidate"] == 8
            assert result["deferred_by_reason"]["below_threshold"] == 2
            assert result["deferred_by_reason"]["materiality"] == 2
            assert result["deferred_by_reason"]["manual_investigation"] == 3
            assert result["deferred_by_reason"]["data_quality"] == 1
            assert result["deferred_by_reason"]["conflicting_evidence"] == 0
            assert result["match_rate"] == pytest.approx(42 / 61, abs=1e-4)

            async with SessionLocal() as db:
                actions = set(
                    (
                        await db.execute(
                            select(AuditLog.action).where(
                                AuditLog.action.like("controller.close_%")
                            )
                        )
                    ).scalars()
                )
                assert "controller.close_started" in actions
                assert "controller.close_completed" in actions
                assert "controller.close_matched" in actions
                assert "controller.close_deferred" in actions
                assert "controller.close_no_match" in actions
            return result

        result = asyncio.run(scenario())
        assert len(result["decisions"]) == 61