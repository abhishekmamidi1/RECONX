from decimal import Decimal

import pytest

from app.services.parsers import (
    BankStatementParser,
    ErpTransactionParser,
    RazorpaySettlementParser,
)
from app.services.parsers.base import ParserError
from tests.conftest import load_sample


class TestRazorpaySettlementParser:
    def test_parses_sample_file(self):
        result = RazorpaySettlementParser().parse(load_sample("razorpay_settlements.csv"))

        assert result.source == "razorpay"
        assert result.rows_total == 6
        assert result.rows_parsed == 6
        assert result.rows_rejected == 0
        assert len(result.drafts) == 6

    def test_normalizes_credit_amount_as_canonical(self):
        drafts = RazorpaySettlementParser().parse(
            load_sample("razorpay_settlements.csv")
        ).drafts

        first = next(d for d in drafts if d.external_ref == "pay_Aa1b2C3d4")
        assert first.amount == Decimal("12455.75")
        assert first.direction == "credit"
        assert first.raw["gross_amount"] == "12500.00"
        assert first.raw["fee"] == "37.50"

    def test_handles_thousands_separator_formatting(self):
        drafts = RazorpaySettlementParser().parse(
            load_sample("razorpay_settlements.csv")
        ).drafts

        formatted = next(d for d in drafts if d.external_ref == "pay_Ff6g7H8i9")
        assert formatted.amount == Decimal("1245.57")

    def test_missing_required_columns_raises_parser_error(self):
        csv_bytes = b"payment_id,amount\npay_1,100.00\n"

        with pytest.raises(ParserError, match="missing required columns"):
            RazorpaySettlementParser().parse(csv_bytes)

    def test_accepts_negative_refund_row_as_debit_magnitude(self):
        # Exact structure from the failing ingest: a refund row sharing the
        # original payment's settlement but carrying a negative amount and
        # status=refunded. Previously rejected with "negative amount".
        csv_bytes = (
            b"settlement_id,settlement_date,payment_id,amount,fee,tax,credit_amount,"
            b"status,method,currency,order_id,customer_ref,utr,description\n"
            b"setl_202608,2026-08-10,pay_PAY100,5000.00,40.00,10.00,4950.00,"
            b"settled,upi,INR,ord_1,cust_1,UTR100,payment\n"
            b"setl_202608,2026-08-10,pay_REFUND,-1200.00,0.00,0.00,-1200.00,"
            b"refunded,upi,INR,ord_1,cust_1,UTRREF,refund of pay_REFUND\n"
        )

        result = RazorpaySettlementParser().parse(csv_bytes)

        assert result.rows_total == 2
        assert result.rows_parsed == 2
        assert result.rows_rejected == 0
        refund = next(d for d in result.drafts if d.external_ref == "pay_REFUND-REFUND")
        assert refund.amount == Decimal("1200.00")
        assert refund.direction == "debit"
        assert refund.transaction_type == "refund"
        assert refund.raw["amount_sign"] == "negative"
        assert any("classified refund" in note for note in refund.raw["parser_notes"])
        assert refund.raw["gross_amount"] == "1200.00"
        settled = next(d for d in result.drafts if d.external_ref == "pay_PAY100")
        assert settled.transaction_type == "settlement"
        assert settled.direction == "credit"
        assert settled.amount == Decimal("4950.00")
        assert settled.direction == "credit"
        assert settled.amount == Decimal("4950.00")

    def test_refund_typed_by_status_with_positive_amount(self):
        csv_bytes = (
            b"settlement_id,settlement_date,payment_id,amount,fee,tax,credit_amount,"
            b"status,method,currency,order_id,customer_ref,utr,description\n"
            b"setl_1,2026-08-10,pay_R2,999.00,0.00,0.00,999.00,"
            b"refunded,card,INR,ord_2,cust_2,UTR2,refund\n"
        )

        result = RazorpaySettlementParser().parse(csv_bytes)

        refund = result.drafts[0]
        assert refund.amount == Decimal("999.00")
        assert refund.direction == "debit"
        assert refund.transaction_type == "refund"


class TestBankStatementParser:
    def test_splits_credit_and_debit_directions(self):
        result = BankStatementParser().parse(load_sample("bank_statement.csv"))

        assert result.rows_total == 5
        assert result.rows_parsed == 5
        credits = [d for d in result.drafts if d.direction == "credit"]
        debits = [d for d in result.drafts if d.direction == "debit"]
        assert len(credits) == 4
        assert len(debits) == 1
        assert debits[0].external_ref == "CHG-Q1-FY2701"
        assert debits[0].amount == Decimal("590.00")

    def test_rejects_row_with_no_amount(self):
        csv_bytes = (
            b"date,narration,ref_no,withdrawal,deposit,balance\n"
            b"2026-08-02,BROKEN ROW,REF-X,,,100.00\n"
            b"2026-08-03,GOOD ROW,REF-Y,,50.00,150.00\n"
        )

        result = BankStatementParser().parse(csv_bytes)

        assert result.rows_rejected == 1
        assert result.rows_parsed == 1
        assert "row 2" in result.rejections[0]

    def test_rejects_row_with_both_sides_present(self):
        csv_bytes = (
            b"date,narration,ref_no,withdrawal,deposit,balance\n"
            b"2026-08-02,BAD,REF-Z,10.00,20.00,\n"
        )

        result = BankStatementParser().parse(csv_bytes)

        assert result.rows_rejected == 1
        assert "both withdrawal and deposit" in result.rejections[0]

    def test_missing_ref_is_row_level_rejection_not_fatal(self):
        csv_bytes = (
            b"date,narration,ref_no,withdrawal,deposit,balance\n"
            b"2026-08-02,NO REF,,10.00,,\n"
        )

        result = BankStatementParser().parse(csv_bytes)

        assert result.rows_rejected == 1
        assert "missing ref_no" in result.rejections[0]


class TestErpTransactionParser:
    def test_parses_sample_file(self):
        result = ErpTransactionParser().parse(load_sample("erp_transactions.csv"))

        assert result.rows_total == 7
        assert result.rows_parsed == 7
        unpaid = [d for d in result.drafts if d.status == "UNPAID"]
        assert len(unpaid) == 1
        assert unpaid[0].external_ref == "INV-2026-0707"
        assert unpaid[0].raw["payment_ref"] is None

    def test_keeps_payment_ref_for_future_matching(self):
        drafts = ErpTransactionParser().parse(load_sample("erp_transactions.csv")).drafts

        paid = next(d for d in drafts if d.external_ref == "INV-2026-0701")
        assert paid.raw["payment_ref"] == "pay_Aa1b2C3d4"
        assert paid.raw["order_id"] == "ord_Q1w2E3r4"
