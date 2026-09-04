"""Unit tests for the close-pass reason mapping.

These exercise the deterministic evidence->reason selection without a
database. The end-to-end decision accuracy against the golden batch lives in
test_close_golden_dataset.py.
"""

import uuid

import pytest

from app.models import ExceptionRecord, Match, Transaction
from app.services.controller import REASON_CODES, reason_code_for_exception
from app.services.controller.close import _resolve_reason


class TestReasonCodeForException:
    def test_refund_reasons(self):
        assert reason_code_for_exception("refund") == "manual_investigation"
        assert reason_code_for_exception("duplicate_suspect") == "manual_investigation"

    def test_materiality_maps_from_amount_mismatch(self):
        assert reason_code_for_exception("amount_mismatch") == "materiality"

    def test_low_confidence_is_below_threshold(self):
        assert reason_code_for_exception("low_confidence_ai") == "below_threshold"

    def test_manual_review_blocked_by_similarity_is_below_threshold(self):
        assert (
            reason_code_for_exception(
                "manual_review_required", blocked_by_similarity=True
            )
            == "below_threshold"
        )
        assert (
            reason_code_for_exception(
                "manual_review_required", blocked_by_similarity=False
            )
            == "manual_investigation"
        )

    def test_unmatched_uses_narration_quality(self):
        assert (
            reason_code_for_exception("unmatched", narration_blank=True)
            == "data_quality"
        )
        assert (
            reason_code_for_exception("unmatched", narration_blank=False)
            == "no_candidate"
        )

    def test_unknown_exception_defers_to_manual(self):
        assert reason_code_for_exception("weird_case") == "manual_investigation"

    def test_reason_codes_are_the_required_set(self):
        assert REASON_CODES == [
            "no_candidate",
            "below_threshold",
            "conflicting_evidence",
            "materiality",
            "data_quality",
            "manual_investigation",
        ]


def _txn(ref: str, source: str, narration: str | None = None) -> Transaction:
    return Transaction(
        external_ref=ref,
        source=source,
        amount=0,
        direction="credit",
        transaction_type="settlement",
        currency="INR",
        narration=narration,
    )


class TestResolveReason:
    def test_conflicting_evidence_when_two_proposals(self):
        txn = _txn("pay_conflict", "razorpay")
        proposed_a = Match(status="proposed")
        proposed_b = Match(status="proposed")
        reason = _resolve_reason(txn, [proposed_a, proposed_b], None, {}, {}, [])
        assert reason == "conflicting_evidence"

    def test_own_open_exception_wins(self):
        txn = _txn("pay_x", "razorpay")
        exc = ExceptionRecord(exception_type="amount_mismatch", status="open")
        reason = _resolve_reason(txn, [], exc, {}, {}, [])
        assert reason == "materiality"

    def test_peer_exception_derives_reason(self):
        txn = _txn("pay_y", "razorpay")
        txn.id = uuid.uuid4()
        peer = _txn("INV-2026-0916", "erp")
        peer.id = uuid.uuid4()
        peer_exc = ExceptionRecord(exception_type="manual_review_required", status="open")
        peer_exc.id = uuid.uuid4()
        opened = {peer_exc.id: {"auto_resolve_blocked_by": "similarity_autoresolve_min"}}
        reason = _resolve_reason(
            txn,
            [Match(status="proposed")],
            None,
            opened,
            {peer.id: peer_exc},
            [peer],
        )
        assert reason == "below_threshold"

    def test_no_proposal_and_no_exception_is_no_candidate(self):
        txn = _txn("pay_z", "razorpay")
        assert _resolve_reason(txn, [], None, {}, {}, []) == "no_candidate"

    def test_proposed_without_any_exception_defers_to_manual(self):
        txn = _txn("pay_w", "razorpay")
        txn.id = uuid.uuid4()
        peer = _txn("INV-x", "erp")
        peer.id = uuid.uuid4()
        reason = _resolve_reason(
            txn, [Match(status="proposed")], None, {}, {}, [peer]
        )
        assert reason == "manual_investigation"