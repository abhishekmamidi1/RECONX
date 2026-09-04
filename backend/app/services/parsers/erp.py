from app.services.parsers.base import (
    SourceParser,
    TransactionDraft,
    clean_amount,
    clean_datetime,
)


class ErpTransactionParser(SourceParser):
    key = "erp"
    label = "ERP Transaction Log"

    required_columns = frozenset(
        {"invoice_no", "txn_date", "order_id", "payment_ref", "amount", "status"}
    )
    optional_columns = frozenset({"customer_code", "currency", "tax", "description"})

    def _build(self, row: dict) -> TransactionDraft:
        invoice_no = self._require_ref(row, "invoice_no")
        payment_ref = self._text(row, "payment_ref") or None
        order_id = self._text(row, "order_id") or None
        customer_code = self._text(row, "customer_code") or None
        tax = self._text(row, "tax")
        description = self._text(row, "description") or None
        amount = clean_amount(row["amount"])
        status = self._text(row, "status") or None
        refund = (status or "").upper() in ("REFUNDED", "REVERSED", "REFUND", "REVERSAL")
        transaction_type = "refund" if refund else "settlement"
        direction = "debit" if refund else "credit"
        narration = f"ERP invoice {invoice_no} paid via {payment_ref or 'unknown'}"
        if refund:
            narration = f"ERP refund for invoice {invoice_no}"
        if description:
            narration = f"{narration} | {description}"
        return TransactionDraft(
            source=self.key,
            external_ref=invoice_no,
            amount=amount,
            direction=direction,
            txn_date=clean_datetime(row["txn_date"]),
            currency=self._text(row, "currency").upper() or "INR",
            narration=narration,
            counterparty=customer_code,
            status=status,
            transaction_type=transaction_type,
            raw={
                "invoice_no": invoice_no,
                "payment_ref": payment_ref,
                "order_id": order_id,
                "tax": tax,
                "description": description,
            },
        )
