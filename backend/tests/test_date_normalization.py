from datetime import datetime, timedelta, timezone

import pytest

from app.services.parsers import (
    BankStatementParser,
    ErpTransactionParser,
    RazorpaySettlementParser,
)
from app.services.parsers.base import business_date
from tests.conftest import load_sample

ALL_PARSERS = [
    (RazorpaySettlementParser(), "razorpay_settlements.csv"),
    (BankStatementParser(), "bank_statement.csv"),
    (ErpTransactionParser(), "erp_transactions.csv"),
]


class TestUtcMidnightNormalizationInvariant:
    @pytest.mark.parametrize("parser,sample", ALL_PARSERS)
    def test_every_parsed_date_is_tz_aware_utc_midnight(self, parser, sample):
        result = parser.parse(load_sample(sample))

        assert result.rows_parsed > 0
        for draft in result.drafts:
            assert draft.txn_date.tzinfo is timezone.utc, (
                f"{parser.key} produced non-UTC-aware date for {draft.external_ref}"
            )
            assert (draft.txn_date.hour, draft.txn_date.minute, draft.txn_date.second) == (
                0,
                0,
                0,
            ), f"{parser.key} produced non-midnight time for {draft.external_ref}"

    def test_same_calendar_day_across_sources_yields_equal_business_dates(self):
        razorpay = RazorpaySettlementParser().parse(
            load_sample("razorpay_settlement_test.csv")
        ).drafts
        bank = BankStatementParser().parse(
            load_sample("bank_statement_test.csv")
        ).drafts

        rz = next(d for d in razorpay if d.external_ref == "pay_Gg7h8I9j")
        bk = next(d for d in bank if d.external_ref == "UTR888170260810")
        assert str(business_date(rz.txn_date)) == "2026-08-15"
        assert business_date(rz.txn_date) == business_date(bk.txn_date)

    def test_next_calendar_day_compares_as_exact_one_day_delta(self):
        razorpay = RazorpaySettlementParser().parse(
            load_sample("razorpay_settlements.csv")
        ).drafts
        bank = BankStatementParser().parse(load_sample("bank_statement.csv")).drafts

        rz = next(d for d in razorpay if d.external_ref == "pay_Cc3d4E5f6")
        bk = next(d for d in bank if d.external_ref == "UTR995310260803")

        delta = business_date(bk.txn_date) - business_date(rz.txn_date)
        assert delta.days == 1

    def test_business_date_rejects_naive_datetimes(self):
        with pytest.raises(ValueError, match="tz-aware"):
            business_date(datetime(2026, 8, 16))

    def test_business_date_converts_non_utc_offset_to_utc_calendar_day(self):
        ist_midnight_plus_5 = datetime(2026, 8, 17, 5, 30, tzinfo=timezone.utc)

        assert str(business_date(ist_midnight_plus_5)) == "2026-08-17"

        ist_tz = timezone(offset=timedelta(hours=5, minutes=30))
        late_evening_ist = datetime(2026, 8, 16, 23, 30, tzinfo=ist_tz)

        assert str(business_date(late_evening_ist)) == "2026-08-16"
