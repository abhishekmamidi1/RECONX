"""Per-batch (per-ingestion) report scope regression tests.

Contract: ``GET /api/v1/reports/{summary,export.csv,export.pdf}?ingestion_id=...``
scopes the report to ONE uploaded batch (``Transaction.ingestion_id``):

  - summary stats / exceptions / match-rate are restricted to the batch's own
    transactions — for the golden razorpay batch that is exactly the 7 razorpay
    legs (5 confirmed auto + 2 exceptions), while bank/ERP totals are ZERO,
  - the 5 confirmed razorpay match 5/7 = 71.4% with zero bank/ERP leakage,
  - matches that cross into other batches still carry full context via
    cross_batch_participants (bank & ERP legs, never razorpay),
  - CSV / PDF reuse the existing report format (same columns/layout), with a
    small ``#`` preamble naming the batch + cross-batch context.
"""

import asyncio
import csv
import io
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    ExceptionRecord,
    Ingestion,
    Match,
    MatchParticipant,
    PolicyConfig,
    Transaction,
)

EXACT_TRIOS = [
    ("pay_Gg7h8I9j", "INV-2026-0710", "UTR888170260810"),
    ("pay_Ii9j0K1l", "INV-2026-0712", "UTR888170260812"),
    ("pay_Kk1l2M3n", "INV-2026-0714", "UTR888170260814"),
]
FUZZY_PAIR = ("pay_Hh8i9J0k", "INV-2026-0711", "UTR888170260811X")
SEMANTIC_MATCH_PAIR = ("pay_Sm1Th1cA", "INV-2026-0720")
SEMANTIC_HUMAN_PAIR = ("pay_Pl4tFrmZ", "INV-2026-0721")
MISSING_ERP_REFS = ("pay_Jj0k1L2m", "UTR888170260813")
INERT_REFS = ("CHG-Q2-FY2702",)

ALL_REFS = [ref for trio in EXACT_TRIOS for ref in trio] + list(FUZZY_PAIR) + list(
    SEMANTIC_MATCH_PAIR
) + list(SEMANTIC_HUMAN_PAIR) + list(MISSING_ERP_REFS) + list(INERT_REFS)

BATCH_SPEC = {
    "razorpay": {
        "refs": {ref for ref in ALL_REFS if ref.startswith("pay_")},
        "filename": "razorpay_settlement_test.csv",
        "rows": 7,
        "matched": 5,  # 3 exact + 1 fuzzy + 1 semantic/AI
        "rate": round(5 / 7, 4),
        "exceptions": 2,  # unmatched pay_Jj0k1L2m + manual_review pay_Pl4tFrmZ
        "cross_batch": 11,  # 5 bank UTRs (incl. 813's 2-way proposal) + 6 erp invoices
    },
    "bank": {
        "refs": {ref for ref in ALL_REFS if ref.startswith("UTR") or ref in INERT_REFS},
        "filename": "bank_statement_test.csv",
        "rows": 6,
        "matched": 4,  # 3 exact UTRs + the fuzzy UTR888170260811X
        "rate": round(4 / 6, 4),
        "exceptions": 0,
        "cross_batch": 9,  # 5 razorpay pay_ refs (incl. Jj's 2-way proposal) + 4 erp
    },
}

OPERATOR = "ops-batch"


def _client() -> TestClient:
    return TestClient(app)


async def _by_ref():
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(Transaction).where(Transaction.external_ref.in_(ALL_REFS)))
        ).scalars().all()
        return {t.external_ref: t for t in rows}


async def _purge_and_thresholds(by_ref):
    from sqlalchemy import delete

    async with SessionLocal() as db:
        txn_ids = [t.id for t in by_ref.values()]
        match_ids = (
            await db.execute(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.transaction_id.in_(txn_ids)
                )
            )
        ).scalars().all()
        await db.execute(
            delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(txn_ids))
        )
        await db.execute(
            delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids))
        )
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))

        threshold = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
        original_t = threshold.value
        threshold.value = 0.10
        gate = await db.get(PolicyConfig, "matching.ai.similarity_autoresolve_min")
        original_g = gate.value
        gate.value = 0.15
        await db.commit()
        return original_t, original_g


async def _restore(originals):
    async with SessionLocal() as db:
        threshold = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
        threshold.value = originals[0]
        gate = await db.get(PolicyConfig, "matching.ai.similarity_autoresolve_min")
        gate.value = originals[1]
        await db.commit()


def _scenario(source, verify):
    """Purge the golden scope, create a batch over the chosen source, run the
    pipeline once, then call ``verify(client, batch_id, spec)`` while the batch
    is still live. The ledger is fully restored afterwards regardless of
    outcome, so the test leaves no trace.
    """
    spec = BATCH_SPEC[source]

    async def body():
        from app.services.reconciliation import run_reconciliation

        by_ref = await _by_ref()
        originals = await _purge_and_thresholds(by_ref)
        original_ingestions = {t.id: t.ingestion_id for t in by_ref.values()}
        batch_id = uuid.uuid4()

        async with SessionLocal() as db:
            batch = Ingestion(
                id=batch_id,
                source=source,
                filename=spec["filename"],
                checksum_sha256=f"test-batch-scope-{source}",
                rows_total=spec["rows"],
                rows_inserted=spec["rows"],
                rows_skipped_duplicate=0,
                rows_rejected=0,
                status="completed",
            )
            db.add(batch)
            await db.flush()
            await db.execute(
                update(Transaction)
                .where(Transaction.external_ref.in_(sorted(spec["refs"])))
                .values(ingestion_id=batch_id)
            )
            await db.commit()

        try:
            scope_ids = [t.id for ref, t in by_ref.items() if ref not in INERT_REFS]
            async with SessionLocal() as db:
                await run_reconciliation(db, actor=OPERATOR, transaction_ids=scope_ids)
                await db.commit()

            with _client() as client:
                verify(client, batch_id, spec)
            return batch_id
        finally:
            async with SessionLocal() as db:
                golden_rows = (
                    await db.execute(
                        select(Transaction).where(
                            Transaction.external_ref.in_(ALL_REFS)
                        )
                    )
                ).scalars().all()
                for txn in golden_rows:
                    txn.ingestion_id = original_ingestions[txn.id]
                ing = await db.get(Ingestion, batch_id)
                if ing is not None:
                    await db.delete(ing)
                await db.commit()
            await _restore(originals)

    return asyncio.run(body())


def test_razorpay_batch_report_is_scoped_to_its_own_transactions():
    def verify(client, batch_id, spec):
        # Summary: headline stats are THIS batch's razorpay legs and nothing else.
        summary = client.get(
            "/api/v1/reports/summary", params={"ingestion_id": batch_id}
        ).json()
        assert summary["scope"]["filename"] == "razorpay_settlement_test.csv"
        assert summary["scope"]["source"] == "razorpay"
        assert summary["scope"]["rows_total"] == 7
        assert summary["scope"]["transactions_in_batch"] == 7

        rates = {r["source"]: r for r in summary["match_rate_by_source"]}
        assert rates["razorpay"]["total_transactions"] == 7
        assert rates["razorpay"]["matched"] == spec["matched"]
        assert rates["razorpay"]["rate"] == spec["rate"]
        assert rates["bank"]["total_transactions"] == 0, "bank txns must NOT leak in"
        assert rates["erp"]["total_transactions"] == 0, "erp txns must NOT leak in"

        confirmed = [m for m in summary["matches"] if m["status"] == "confirmed"]
        assert sum(m["count"] for m in confirmed) == spec["matched"]
        assert summary["auto_resolved_total"] == spec["matched"]
        proposed = [m for m in summary["matches"] if m["status"] == "proposed"]
        assert sum(m["count"] for m in proposed) == 2, proposed
        semantic = [m for m in proposed if m["match_type"] == "semantic"]
        assert len(semantic) == 1, semantic  # manual_review pair pay_Pl4tFrmZ/INV-0721
        assert any(
            m["match_type"] == "deterministic" for m in proposed
        ), proposed  # 2-way pay_Jj0k1L2m/UTR813 candidate

        exceptions = summary["exceptions"]
        by_type = {e["exception_type"]: e["count"] for e in exceptions}
        assert by_type.get("unmatched") == 1, by_type
        assert by_type.get("manual_review_required") == 1, by_type

        # Cross-batch context: the bank/ERP legs the batch matched with.
        cross = summary["cross_batch_participants"]
        assert len(cross) == spec["cross_batch"], f"got {len(cross)} cross-batch legs"
        assert all(p["source"] in ("bank", "erp") for p in cross), [
            p["source"] for p in cross
        ]
        refs = {p["external_ref"] for p in cross}
        for expected in (
            "UTR888170260810",
            "UTR888170260811X",
            "UTR888170260813",
            "INV-2026-0720",
            "INV-2026-0721",
        ):
            assert expected in refs, f"cross-batch context missing {expected}"

        # CSV: same exception-ledger schema, prefixed with batch context.
        csv_response = client.get(
            "/api/v1/reports/export.csv", params={"ingestion_id": batch_id}
        )
        assert csv_response.status_code == 200
        assert csv_response.headers["content-disposition"] == (
            'attachment; filename="batch-report_razorpay_settlement_test.csv"'
        )
        text = csv_response.text
        assert text.startswith("# batch: razorpay_settlement_test.csv")
        assert "# cross-batch participants:" in text
        rows = list(csv.reader(io.StringIO(text)))
        data_rows = [r for r in rows if r and not r[0].startswith("#")]
        assert data_rows[0][0] == "exception_id"
        ledger = data_rows[1:]
        assert len(ledger) == spec["exceptions"], f"got {ledger}"
        assert all(r[6] == "razorpay" for r in ledger), [r[6] for r in ledger]
        assert all(r[5].startswith("pay_") for r in ledger), [r[5] for r in ledger]

        # PDF: same renderer, scoped headline banner.
        pdf_response = client.get(
            "/api/v1/reports/export.pdf", params={"ingestion_id": batch_id}
        )
        assert pdf_response.status_code == 200
        assert pdf_response.headers["content-type"] == "application/pdf"
        assert pdf_response.content[:5] == b"%PDF-"
        assert len(pdf_response.content) > 1500

        # Unknown batch -> 404, never a silent global report.
        missing = client.get(
            "/api/v1/reports/summary",
            params={"ingestion_id": str(uuid.uuid4())},
        )
        assert missing.status_code == 404

    _scenario("razorpay", verify)


def test_bank_batch_report_stays_within_bank_and_still_lists_cross_batch_context():
    def verify(client, batch_id, spec):
        summary = client.get(
            "/api/v1/reports/summary", params={"ingestion_id": batch_id}
        ).json()
        assert summary["scope"]["filename"] == "bank_statement_test.csv"
        assert summary["scope"]["source"] == "bank"
        assert summary["scope"]["transactions_in_batch"] == 6

        rates = {r["source"]: r for r in summary["match_rate_by_source"]}
        assert rates["bank"]["total_transactions"] == 6
        assert rates["bank"]["matched"] == spec["matched"]
        assert rates["bank"]["rate"] == spec["rate"]
        assert rates["razorpay"]["total_transactions"] == 0, "razorpay txns must NOT leak in"
        assert rates["erp"]["total_transactions"] == 0, "erp txns must NOT leak in"

        confirmed = [m for m in summary["matches"] if m["status"] == "confirmed"]
        assert sum(m["count"] for m in confirmed) == spec["matched"]

        cross = summary["cross_batch_participants"]
        assert len(cross) == spec["cross_batch"]
        refs = {p["external_ref"] for p in cross}
        assert all(p["source"] in ("razorpay", "erp") for p in cross), [
            p["source"] for p in cross
        ]
        for expected in ("pay_Gg7h8I9j", "pay_Jj0k1L2m", "pay_Kk1l2M3n"):
            assert expected in refs  # razorpay legs as context
        for expected in ("INV-2026-0710", "INV-2026-0714"):
            assert expected in refs  # erp legs as context
        assert all(
            ref not in refs for ref in ("INV-2026-0720", "INV-2026-0721")
        ), "semantic-only matches (no bank leg) must not leak into context"

        # The bank batch produced no exceptions on its own transactions.
        assert sum(e["count"] for e in summary["exceptions"]) == spec["exceptions"]

    _scenario("bank", verify)