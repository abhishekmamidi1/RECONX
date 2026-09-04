import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.models import Transaction
from app.services.reconciliation.engine import (
    _Group,
    _Leg,
    _breaches_materiality,
    _money_discrepancy,
    _rationale,
    fuzzy_score,
    fuzzy_threshold,
    pair_deterministic,
)

POLICY = {
    "matching.deterministic.date_window_days": 0,
    "matching.fuzzy.score_threshold": 85,
    "matching.fuzzy.date_window_days": 5,
    "matching.fuzzy.amount_tolerance_pct": 0,
}


def _txn(
    source: str,
    ref: str,
    amount: str,
    day: int,
    *,
    direction: str = "credit",
    raw: dict | None = None,
    narration: str | None = None,
) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        source=source,
        external_ref=ref,
        amount=Decimal(amount),
        direction=direction,
        currency="INR",
        txn_date=datetime(2026, 8, day, tzinfo=timezone.utc),
        narration=narration,
        raw=raw or {},
    )


class TestDeterministicPairing:
    def test_links_erp_via_payment_ref_and_bank_via_utr(self):
        rz = _txn(
            "razorpay",
            "pay_X1",
            "100.00",
            15,
            raw={"payment_id": "pay_X1", "utr": "UTR1", "gross_amount": "105.00"},
        )
        erp = _txn("erp", "INV-1", "105.00", 15, raw={"payment_ref": "pay_X1"})
        bank = _txn("bank", "UTR1", "100.00", 15)

        legs, conflicts = pair_deterministic([rz, erp, bank], POLICY)

        assert not conflicts
        pairs = {(leg.a, leg.b) for leg in legs}
        assert (rz.id, erp.id) in pairs or (erp.id, rz.id) in pairs
        assert (rz.id, bank.id) in pairs or (bank.id, rz.id) in pairs

    def test_typoed_utr_never_links_deterministically(self):
        rz = _txn("razorpay", "pay_X2", "100.00", 16, raw={"utr": "UTR2"})
        bank = _txn("bank", "UTR2X", "100.00", 16)

        legs, _ = pair_deterministic([rz, bank], POLICY)

        assert legs == []

    def test_duplicate_erp_claims_are_flagged_not_paired(self):
        rz = _txn("razorpay", "pay_X3", "100.00", 15, raw={})
        erp1 = _txn("erp", "INV-A", "100.00", 15, raw={"payment_ref": "pay_X3"})
        erp2 = _txn("erp", "INV-B", "105.00", 15, raw={"payment_ref": "pay_X3"})

        legs, conflicts = pair_deterministic([rz, erp1, erp2], POLICY)

        assert legs == []
        assert len(conflicts) == 1
        assert conflicts[0]["candidates"] == ["INV-A", "INV-B"]

    def test_outside_date_window_is_rejected(self):
        rz = _txn("razorpay", "pay_X4", "100.00", 1, raw={"utr": "UTR9"})
        erp = _txn("erp", "INV-9", "105.00", 9, raw={"payment_ref": "pay_X4"})

        legs, _ = pair_deterministic([rz, erp], POLICY)

        assert legs == []


class TestFuzzyScoring:
    def test_golden_fuzzy_pair_clears_threshold_without_exact_identity(self):
        policy = dict(POLICY)
        rz = _txn(
            "razorpay",
            "pay_Hh8i9J0k",
            "7411.99",
            16,
            narration="Razorpay settlement setl_Nx2002 for payment pay_Hh8i9J0k",
        )
        bank = _txn(
            "bank",
            "UTR888170260811X",
            "7411.99",
            16,
            narration="IMPS-CR RAZORPAY SETTLEMENT UTR888170260811 CUST REF",
        )

        score = fuzzy_score(rz, bank, policy)

        assert score is not None
        assert fuzzy_threshold(policy) < score < 1

    def test_amount_mismatch_beyond_tolerance_is_ineligible(self):
        rz = _txn("razorpay", "pay_A", "100.00", 10)
        bank = _txn("bank", "UTR-B", "200.00", 10)

        assert fuzzy_score(rz, bank, POLICY) is None

    def test_same_day_exact_amount_scores_above_lagged_exact_amount(self):
        policy = dict(POLICY)
        tight = fuzzy_score(
            _txn("razorpay", "pay_C", "500.00", 10),
            _txn("bank", "UTR-C", "500.00", 10),
            policy,
        )
        loose = fuzzy_score(
            _txn("razorpay", "pay_D", "500.00", 10),
            _txn("bank", "UTR-D", "500.00", 15),
            policy,
        )

        assert tight is not None and loose is not None
        assert tight > loose

    def test_any_amount_delta_is_ineligible_under_zero_tolerance(self):
        policy = dict(POLICY)

        assert (
            fuzzy_score(
                _txn("razorpay", "pay_F", "500.00", 10),
                _txn("bank", "UTR-F", "498.75", 10),
                policy,
            )
            is None
        )

    def test_debit_rows_cannot_pair_via_fuzzy(self):
        rz = _txn("razorpay", "pay_E", "100.00", 10)
        charge = _txn("bank", "CHG-1", "100.00", 10, direction="debit")

        assert fuzzy_score(rz, charge, POLICY) is None


MATERIALITY_POLICY = {
    "materiality.max_abs_discrepancy_inr": "500.00",
    "materiality.max_discrepancy_pct": "0.02",
}


class TestMaterialityGate:
    def test_zero_discrepancy_passes(self):
        rz = _txn("razorpay", "pay_M1", "1000.00", 10)
        bank = _txn("bank", "UTR-M1", "1000.00", 10)
        group_members = {rz.id, bank.id}
        txns = {rz.id: rz, bank.id: bank}

        discrepancy = _money_discrepancy(
            type("G", (), {"members": group_members})(), txns
        )

        assert discrepancy == Decimal("0")
        assert not _breaches_materiality(
            discrepancy, Decimal("1000"), MATERIALITY_POLICY
        )

    def test_batched_sum_against_single_deposit_passes_when_equal(self):
        rz_a = _txn("razorpay", "pay_M2", "600.00", 10)
        rz_b = _txn("razorpay", "pay_M3", "400.00", 11)
        bank = _txn("bank", "UTR-M2", "1000.00", 12)

        class G:
            members = {rz_a.id, rz_b.id, bank.id}

        discrepancy = _money_discrepancy(G(), {rz_a.id: rz_a, rz_b.id: rz_b, bank.id: bank})

        assert discrepancy == Decimal("0")
        assert not _breaches_materiality(discrepancy, Decimal("1000"), MATERIALITY_POLICY)

    def test_absolute_limit_breach_blocks_auto_resolve(self):
        assert _breaches_materiality(
            Decimal("600.00"), Decimal("100000"), MATERIALITY_POLICY
        )

    def test_percentage_limit_breach_blocks_even_small_amounts(self):
        assert _breaches_materiality(
            Decimal("30.00"), Decimal("1000"), MATERIALITY_POLICY
        )

    def test_group_without_bank_leg_has_no_money_side_to_compare(self):
        rz = _txn("razorpay", "pay_M4", "100.00", 10)
        erp = _txn("erp", "INV-M4", "105.00", 10)

        class G:
            members = {rz.id, erp.id}

        assert (
            _money_discrepancy(G(), {rz.id: rz, erp.id: erp}) is None
        )


class TestFuzzyAmountTolerance:
    """Round-trip regression for the 0.1% fuzzy amount tolerance.

    The rounding acceptance (UTR888170260832 vs pay_Rr6TtUuVv, 1482.31 vs
    1482.30) must become fuzzy-eligible, while unrelated amounts further apart
    than 0.1% must stay ineligible so the tolerance never pairs strangers.
    """

    TOL_POLICY = {
        "matching.fuzzy.score_threshold": 85,
        "matching.fuzzy.date_window_days": 5,
        "matching.fuzzy.amount_tolerance_pct": 0.001,
    }

    def test_rounding_delta_within_tolerance_scores(self):
        rz = _txn(
            "razorpay",
            "pay_Rr6TtUuVv",
            "1482.30",
            30,
            narration="Razorpay settlement setl_Nx4003 for payment pay_Rr6TtUuVv",
        )
        bank = _txn(
            "bank",
            "UTR888170260832",
            "1482.31",
            30,
            narration="NEFT-CR RAZORPAY SOFTWARE PVT LTD UTR888170260832",
        )

        score = fuzzy_score(rz, bank, self.TOL_POLICY)

        assert score is not None, "0.01 gap on 1482 must clear the 0.1% tolerance"
        assert fuzzy_threshold(self.TOL_POLICY) < score < 1

    def test_delta_beyond_tolerance_is_ineligible(self):
        rz = _txn("razorpay", "pay_A", "1000.00", 10)
        bank = _txn("bank", "UTR-B", "998.50", 10)

        assert fuzzy_score(rz, bank, self.TOL_POLICY) is None, (
            "0.15% gap must remain ineligible under a 0.1% tolerance"
        )

    def test_boundary_at_exactly_tolerance_is_eligible(self):
        rz = _txn("razorpay", "pay_C", "1000.00", 10)
        bank = _txn("bank", "UTR-C", "999.00", 10)

        assert fuzzy_score(rz, bank, self.TOL_POLICY) is not None


class TestRationaleSourceCount:
    def test_two_source_confirmed_rationale_says_two_source(self):
        rz = _txn("razorpay", "pay_Ff6g7H8i9", "1245.57", 10)
        erp = _txn("erp", "INV-2026-0706", "1300.00", 10)
        group = _Group(members={rz.id, erp.id}, legs=[_Leg(rz.id, erp.id, "exact", Decimal("1"))])
        txns_by_id = {rz.id: rz, erp.id: erp}

        text = _rationale(group, txns_by_id, "confirmed", None, POLICY)

        assert "2-source agreement" in text
        assert "three-source" not in text

    def test_three_source_confirmed_rationale_says_three_source(self):
        rz = _txn("razorpay", "pay_Ff6g7H8i9", "1245.57", 10)
        erp = _txn("erp", "INV-2026-0706", "1300.00", 10)
        bank = _txn("bank", "UTR888170260899", "1245.57", 10)
        group = _Group(members={rz.id, erp.id, bank.id})
        txns_by_id = {rz.id: rz, erp.id: erp, bank.id: bank}

        text = _rationale(group, txns_by_id, "confirmed", None, POLICY)

        assert "3-source agreement" in text
