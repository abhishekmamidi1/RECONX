from app.services.parsers.base import (
    SourceParser,
    TransactionDraft,
    optional_amount,
)


class BankStatementParser(SourceParser):
    key = "bank"
    label = "Bank Statement"

    required_columns = frozenset(
        {"date", "narration", "ref_no", "withdrawal", "deposit"}
    )
    optional_columns = frozenset({"value_date", "balance", "currency"})

    def _build(self, row: dict) -> TransactionDraft:
        ref_no = self._require_ref(row, "ref_no")
        withdrawal = optional_amount(row["withdrawal"]) or None
        deposit = optional_amount(row["deposit"]) or None
        if deposit is not None and withdrawal is not None:
            raise ValueError("both withdrawal and deposit present in a single row")
        if deposit is None and withdrawal is None:
            raise ValueError("no withdrawal or deposit amount on row")
        narration = self._text(row, "narration")
        balance = self._text(row, "balance")
        value_date = self._text(row, "value_date")
        if deposit is not None:
            amount, direction = deposit, "credit"
            transaction_type = "settlement"
        else:
            amount, direction = withdrawal, "debit"
            # Debits driven by refund/reversal returns (money flowing out to the
            # payer) are classified refund so they never enter positive-credit
            # settlement matching.
            marker = (narration or "").upper()
            refund = any(k in marker for k in ("REFUND", "REVERSAL", "CHARGEBACK"))
            transaction_type = "refund" if refund else "settlement"
        return TransactionDraft(
            source=self.key,
            external_ref=ref_no,
            amount=amount,
            direction=direction,
            txn_date=self._txn_date(row),
            currency=self._text(row, "currency").upper() or "INR",
            narration=narration or None,
            counterparty=None,
            status="posted",
            transaction_type=transaction_type,
            raw={
                "ref_no": ref_no,
                "withdrawal": str(withdrawal) if withdrawal else None,
                "deposit": str(deposit) if deposit else None,
                "balance": balance or None,
                "value_date": value_date or None,
            },
        )

    def _txn_date(self, row: dict):
        from app.services.parsers.base import clean_datetime

        date_value = self._text(row, "date")
        if date_value:
            return clean_datetime(date_value)
        value_date = self._text(row, "value_date")
        if value_date:
            return clean_datetime(value_date)
        raise ValueError("missing date")
