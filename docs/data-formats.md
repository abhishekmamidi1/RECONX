# Source File Formats

Three ingestion formats are supported in Phase 1. Column headers are matched
case-insensitively after lowercasing and replacing spaces with underscores.

Amounts may include thousands separators and currency symbols
(`"₹1,250.00"` → `1250.00`). Dates accept `YYYY-MM-DD`, `DD-MM-YYYY`,
`DD/MM/YYYY` and common timestamp forms.

## Date semantics (normative)

All three sources carry **date-only business dates**. The ingestion
contract normalizes every parsed date to a timezone-aware `timestamptz`
at **UTC midnight**, regardless of source or input format:

    razorpay.settlement_date → 2026-08-16T00:00:00+00  (timestamptz)
    bank.date                → 2026-08-16T00:00:00+00  (timestamptz)
    erp.txn_date             → 2026-08-16T00:00:00+00  (timestamptz)

Consequences:

- Cross-source comparisons (e.g. Phase 2 date-window matching) must use
  the shared `business_date()` helper (`app/services/parsers/base.py`),
  which extracts the UTC calendar date from any tz-aware value and
  rejects naive datetimes. Matchers must never compare raw datetimes
  across sources.
- If a source ever supplies a full timestamp with a non-UTC offset, the
  UTC calendar date of that instant is the business date — the conversion
  happens once, at ingestion, not per matcher.
- The UI renders transaction dates date-only (`formatDate`), pinned to
  UTC so no locale shift can fabricate a spurious time component.

## Razorpay Settlement Report (`razorpay_settlements.csv`)

One row per payment inside a settlement payout.

| Column           | Required | Notes                                            |
| ---------------- | -------- | ------------------------------------------------ |
| `settlement_id`  | yes      | Razorpay settlement/payout batch id              |
| `settlement_date`| yes      | Date the settlement was credited                 |
| `payment_id`     | yes      | `pay_…` — used as the canonical `external_ref`   |
| `utr`            | no       | Bank payout UTR — when present it is stored in `raw` and used by the deterministic bank-linkage rule; absent columns are tolerated |
| `amount`         | yes      | Gross transaction amount                         |
| `fee`            | yes      | Razorpay fees                                    |
| `tax`            | yes      | Tax on fees                                      |
| `credit_amount`  | yes      | Net settled amount → canonical `amount`          |
| `status`         | yes      | e.g. `processed`                                 |
| `method`         | no       | `upi`, `card`, …                                 |
| `currency`       | no       | Defaults to `INR`                                |
| `order_id`       | no       | Kept in `raw` for later matching stages          |
| `customer_ref`   | no       | Kept in `raw`                                    |

**Normalization:** the canonical transaction amount is `credit_amount`
(the money that should land in the bank), direction `credit`. Gross
amount, fee, tax stay in `raw` for discrepancy analysis in later phases.

## Bank Statement (`bank_statement.csv`)

Standard two-sided (withdrawal/deposit) statement export.

| Column       | Required | Notes                                        |
| ------------ | -------- | -------------------------------------------- |
| `date`       | yes*     | Transaction date (*or `value_date`)          |
| `value_date` | no       | Value date                                   |
| `narration`  | yes      | Free text; fuzzy/semantic match input later  |
| `ref_no`     | yes      | UTR/reference — canonical `external_ref`     |
| `withdrawal` | yes†     | Debit amount                                 |
| `deposit`    | yes†     | Credit amount                                |
| `balance`    | no       | Running balance                              |
| `currency`   | no       | Defaults to `INR`                            |

† Exactly one of `withdrawal`/`deposit` must carry a value; it becomes the
canonical `amount` and sets `direction`. Rows failing this are rejected
(row-level), never fatal to the file.

## ERP Transaction Log (`erp_transactions.csv`)

One row per invoice/order recorded by the merchant ERP.

| Column          | Required | Notes                                       |
| --------------- | -------- | ------------------------------------------- |
| `invoice_no`    | yes      | Canonical `external_ref`                    |
| `txn_date`      | yes      | Invoice/booking date                        |
| `order_id`      | yes      | Merchant order id                           |
| `payment_ref`   | yes      | PSP payment id recorded by ERP (may be blank for unpaid invoices — kept in `raw`) |
| `amount`        | yes      | Invoice total                               |
| `tax`           | no       | Kept in `raw`                               |
| `status`        | yes      | `PAID`, `PENDING`, `UNPAID`, …              |
| `customer_code` | no       | Becomes `counterparty`                      |

## Upload semantics

- Endpoint: `POST /api/v1/uploads` (multipart: `source` ∈ {razorpay, bank,
  erp}, `file`).
- Idempotent: rows whose `(source, external_ref)` already exist are skipped
  and reported as `rows_skipped_duplicate`.
- Row-level problems (bad amounts/dates/refs) are rejected individually and
  summarized in the response (`rows_rejected` + preview); only structural
  failures (unreadable CSV, missing required columns, zero data rows)
  abort the whole upload with HTTP 400.
- Every completed or failed upload writes an `audit_logs` entry.

### Try it

```bash
curl -sS -X POST http://localhost:8000/api/v1/uploads \
  -F "source=razorpay" \
  -F "file=@backend/sample_data/razorpay_settlements.csv"

curl -sS -X POST http://localhost:8000/api/v1/uploads \
  -F "source=bank" \
  -F "file=@backend/sample_data/bank_statement.csv"

curl -sS -X POST http://localhost:8000/api/v1/uploads \
  -F "source=erp" \
  -F "file=@backend/sample_data/erp_transactions.csv"
```

Sample files deliberately contain cross-source overlap (same payment ids /
UTRs) plus intentional anomalies (an aggregated bank credit, a bank charge
row, an unpaid ERP invoice) so Phases 2–4 have realistic matching scenarios
out of the box.

To swap in your real exports, keep the same headers or tell us the actual
column layout and we'll adapt the parser.
