from decimal import Decimal

from app.services.parsers import (
    BankStatementParser,
    ErpTransactionParser,
    RazorpaySettlementParser,
)
from app.services.parsers.base import business_date
from tests.conftest import load_sample


class TestGoldenDatasetRazorpay:
    def test_seven_settlements_with_expected_refs_and_credits(self):
        drafts = RazorpaySettlementParser().parse(
            load_sample("razorpay_settlement_test.csv")
        ).drafts

        assert len(drafts) == 7
        credits = {d.external_ref: d.amount for d in drafts}
        assert credits == {
            "pay_Gg7h8I9j": Decimal("4941.00"),
            "pay_Hh8i9J0k": Decimal("7411.99"),
            "pay_Ii9j0K1l": Decimal("3162.24"),
            "pay_Jj0k1L2m": Decimal("11858.40"),
            "pay_Kk1l2M3n": Decimal("939.04"),
            "pay_Sm1Th1cA": Decimal("4420.00"),
            "pay_Pl4tFrmZ": Decimal("2494.10"),
        }

    def test_semantic_cases_carry_no_utr(self):
        drafts = RazorpaySettlementParser().parse(
            load_sample("razorpay_settlement_test.csv")
        ).drafts

        semantic_refs = {"pay_Sm1Th1cA", "pay_Pl4tFrmZ"}
        for draft in drafts:
            if draft.external_ref in semantic_refs:
                assert not draft.raw.get("utr"), (
                    "semantic cases must carry no shared identity"
                )

    def test_extra_utr_column_is_tolerated(self):
        result = RazorpaySettlementParser().parse(
            load_sample("razorpay_settlement_test.csv")
        )

        assert result.rows_rejected == 0
        assert result.rows_parsed == 7


class TestGoldenDatasetErp:
    def test_six_invoices_with_semantic_cases_lacking_payment_ref(self):
        drafts = ErpTransactionParser().parse(
            load_sample("erp_transaction_test.csv")
        ).drafts

        assert len(drafts) == 6
        by_invoice = {d.raw["invoice_no"]: d for d in drafts}
        assert by_invoice["INV-2026-0720"].raw["payment_ref"] is None
        assert by_invoice["INV-2026-0721"].raw["payment_ref"] is None
        payment_refs = {d.raw["payment_ref"] for d in drafts}
        assert "pay_Jj0k1L2m" not in payment_refs

        gross_by_ref = {
            d.raw["payment_ref"]: d.amount
            for d in drafts
            if d.raw["payment_ref"]
        }
        assert gross_by_ref["pay_Gg7h8I9j"] == Decimal("5000.00")
        assert gross_by_ref["pay_Hh8i9J0k"] == Decimal("7500.50")


class TestGoldenDatasetBank:
    def test_typoed_ref_and_clean_unmatched_ref_are_present(self):
        drafts = BankStatementParser().parse(
            load_sample("bank_statement_test.csv")
        ).drafts

        refs = {d.external_ref for d in drafts}
        assert "UTR888170260811X" in refs
        assert "UTR888170260810" in refs
        assert "UTR888170260811" not in refs
        assert "UTR888170260813" in refs

    def test_bank_charge_row_is_a_debit(self):
        drafts = BankStatementParser().parse(
            load_sample("bank_statement_test.csv")
        ).drafts

        charge = next(d for d in drafts if d.external_ref == "CHG-Q2-FY2702")
        assert charge.direction == "debit"
        assert charge.amount == Decimal("590.00")

    def test_exact_trios_align_on_amount_and_business_date(self):
        razorpay = {
            d.external_ref: d
            for d in RazorpaySettlementParser()
            .parse(load_sample("razorpay_settlement_test.csv"))
            .drafts
        }
        bank = {
            d.external_ref: d
            for d in BankStatementParser()
            .parse(load_sample("bank_statement_test.csv"))
            .drafts
        }

        trios = [
            ("pay_Gg7h8I9j", "UTR888170260810"),
            ("pay_Ii9j0K1l", "UTR888170260812"),
            ("pay_Kk1l2M3n", "UTR888170260814"),
        ]
        for payment_id, utr in trios:
            assert razorpay[payment_id].amount == bank[utr].amount, payment_id
            assert business_date(razorpay[payment_id].txn_date) == business_date(
                bank[utr].txn_date
            ), payment_id

    def test_fuzzy_pair_differs_only_by_corrupted_bank_ref(self):
        razorpay = {
            d.external_ref: d
            for d in RazorpaySettlementParser()
            .parse(load_sample("razorpay_settlement_test.csv"))
            .drafts
        }
        bank = {
            d.external_ref: d
            for d in BankStatementParser()
            .parse(load_sample("bank_statement_test.csv"))
            .drafts
        }

        rz = razorpay["pay_Hh8i9J0k"]
        bk = bank["UTR888170260811X"]
        assert rz.amount == bk.amount == Decimal("7411.99")
        assert business_date(rz.txn_date) == business_date(bk.txn_date)
