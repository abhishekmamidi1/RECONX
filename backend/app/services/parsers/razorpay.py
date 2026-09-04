from app.services.parsers.base import (
    SourceParser,
    TransactionDraft,
    clean_signed_amount,
    clean_datetime,
)

_GROSS_FALLBACK_NOTE = "credit_amount missing; fell back to gross amount"

_REFUND_STATUSES = frozenset({"refunded", "refund", "reversed", "reversal"})


class RazorpaySettlementParser(SourceParser):
    key = "razorpay"
    label = "Razorpay Settlement Report"

    required_columns = frozenset(
        {
            "settlement_id",
            "settlement_date",
            "payment_id",
            "amount",
            "fee",
            "tax",
            "credit_amount",
            "status",
        }
    )
    optional_columns = frozenset(
        {"method", "currency", "order_id", "customer_ref", "utr", "description"}
    )

    def _build(self, row: dict) -> TransactionDraft:
        payment_id = self._require_ref(row, "payment_id")
        settlement_id = self._text(row, "settlement_id")
        gross, gross_sign = clean_signed_amount(row["amount"])
        credit_raw = self._text(row, "credit_amount")
        notes: list[str] = []
        if credit_raw == "":
            settled = gross
            notes.append(_GROSS_FALLBACK_NOTE)
        else:
            settled_mag, credit_sign = clean_signed_amount(credit_raw)
            settled = settled_mag
            if credit_sign < 0:
                notes.append("credit_amount negative; stored magnitude")
        method = self._text(row, "method") or None
        order_id = self._text(row, "order_id") or None
        customer_ref = self._text(row, "customer_ref") or None
        utr = self._text(row, "utr") or None
        description = self._text(row, "description") or None
        status = self._text(row, "status") or None
        currency = self._text(row, "currency").upper() or "INR"

        refund = gross_sign < 0 or (status or "").lower() in _REFUND_STATUSES
        transaction_type = "refund" if refund else "settlement"
        direction = "debit" if refund else "credit"
        # A refund shares the original payment's payment_id; namespace the ref
        # so it cannot collide with (or shadow) the original settlement row
        # under uq_transactions_source_external_ref.
        external_ref = f"{payment_id}-REFUND" if refund else payment_id
        narration = (
            f"Razorpay {'refund' if refund else 'settlement'} {settlement_id} "
            f"for payment {payment_id}"
        )
        if description:
            narration = f"{narration} | {description}"

        raw = {
            "settlement_id": settlement_id,
            "payment_id": payment_id,
            "utr": utr,
            "gross_amount": str(gross),
            "fee": self._text(row, "fee"),
            "tax": self._text(row, "tax"),
            "method": method,
            "order_id": order_id,
            "customer_ref": customer_ref,
            "description": description,
            "parser_notes": notes,
        }
        if gross_sign < 0:
            raw["amount_sign"] = "negative"
        if refund:
            raw["parser_notes"] = [*notes, "classified refund from negative amount/status"]

        return TransactionDraft(
            source=self.key,
            external_ref=external_ref,
            amount=settled,
            direction=direction,
            transaction_type=transaction_type,
            txn_date=clean_datetime(row["settlement_date"]),
            currency=currency,
            narration=narration,
            counterparty=None,
            status=status,
            raw=raw,
        )
