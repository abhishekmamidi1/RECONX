"""Round-trip regression for the fuzzy amount tolerance (0.1%).

Covers the live customer scenario that was left unmatched before the fix:

  UTR888170260832  (bank,    1482.31)  -- rounding test case
  pay_Rr6TtUuVv    (razorpay, 1482.30)  -- net settlement, gross 1500.00
  INV-2026-0731    (erp,      1500.00)  -- invoice for that payment

With a 0.1% amount tolerance the fuzzy leg forms during group augmentation,
so all three sources merge and auto-resolve within materiality (0.01 gap).
The negative case proves unrelated amounts further apart than the tolerance
still never get fuzzy-paired.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models import (
    ExceptionRecord,
    Match,
    MatchParticipant,
    PolicyConfig,
    Transaction,
)

reconciliation = pytest.importorskip(
    "app.services.reconciliation",
    reason="activates automatically once the Phase 2 reconciliation pipeline exists",
)

# Uses test-only refs (no collision with real customer data that may already
# be present in the dev database).
TRIO = {
    "razorpay": {
        "ref": "pay_T3s7F1x0K9",
        "amount": "1482.30",
        "narration": "Razorpay settlement setl_Nx4003 for payment pay_T3s7F1x0K9",
        "raw": {"payment_id": "pay_T3s7F1x0K9", "gross_amount": "1500.00", "utr": None},
    },
    "erp": {
        "ref": "INV-2026-0999",
        "amount": "1500.00",
        "raw": {"payment_ref": "pay_T3s7F1x0K9"},
    },
    "bank": {
        "ref": "UTR888170339999",
        "amount": "1482.31",
        "narration": "NEFT-CR RAZORPAY SOFTWARE PVT LTD UTR888170339999",
    },
}

# Similar-but-unrelated: 0.15% gap (> 0.1% tolerance), no shared identity.
NEGATIVE = [
    {
        "source": "razorpay",
        "ref": "pay_T0L3R4NCE",
        "amount": "998.50",
        "narration": "Razorpay settlement setl_XN3 for payment pay_T0L3R4NCE",
        "raw": {"payment_id": "pay_T0L3R4NCE"},
    },
    {
        "source": "bank",
        "ref": "UTR888170260899",
        "amount": "1000.00",
        "narration": "NEFT-CR UNRELATED PAYER UTR888170260899",
    },
]

# Unrelated singles 0.036% apart (2494.10 vs 2495.00) that share heavy
# "platform credits" wording. Mirrors golden pay_Pl4tFrmZ / INV-2026-0721,
# which must keep flowing to the semantic/AI human-review gates, never be
# auto-paired by the tolerance.
SEPARATE_SINGLES = [
    {
        "source": "razorpay",
        "ref": "pay_Wl7tTn5pA",
        "amount": "2494.10",
        "narration": (
            "Razorpay settlement setl_Nx3002 for payment pay_Wl7tTn5pA | "
            "Wallet top-up adjustment processed through platform credits batch"
        ),
    },
    {
        "source": "erp",
        "ref": "INV-2026-0721X",
        "amount": "2495.00",
        "narration": (
            "ERP invoice INV-2026-0721X paid via unknown | Platform credits "
            "settlement queue monthly subscription charge"
        ),
    },
]

TOLERANCE_KEY = "matching.fuzzy.amount_tolerance_pct"
FIX_TOLERANCE = 0.001


def _txn(source: str, ref: str, amount: str, **kw) -> Transaction:
    return Transaction(
        source=source,
        external_ref=ref,
        amount=Decimal(amount),
        direction=kw.get("direction", "credit"),
        transaction_type=kw.get("transaction_type", "settlement"),
        currency="INR",
        txn_date=kw.get("txn_date", datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)),
        narration=kw.get("narration"),
        status=kw.get("status", "processed"),
        raw=kw.get("raw", {}),
    )


async def _purge_refs(refs):
    async with SessionLocal() as db:
        txn_ids = (
            await db.execute(
                select(Transaction.id).where(Transaction.external_ref.in_(refs))
            )
        ).scalars().all()
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
        await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))
        await db.execute(delete(Transaction).where(Transaction.id.in_(txn_ids)))
        await db.commit()


async def _seed_trio():
    await _purge_refs(list(TRIO[k]["ref"] for k in TRIO))
    async with SessionLocal() as db:
        rz = _txn("razorpay", TRIO["razorpay"]["ref"], TRIO["razorpay"]["amount"],
                  narration=TRIO["razorpay"]["narration"], raw=TRIO["razorpay"]["raw"])
        erp = _txn("erp", TRIO["erp"]["ref"], TRIO["erp"]["amount"], raw=TRIO["erp"]["raw"])
        bank = _txn("bank", TRIO["bank"]["ref"], TRIO["bank"]["amount"],
                    narration=TRIO["bank"]["narration"])
        db.add_all([rz, erp, bank])
        await db.commit()
        return {t.external_ref: t.id for t in (rz, erp, bank)}


async def _seed_negative():
    await _purge_refs([n["ref"] for n in NEGATIVE])
    async with SessionLocal() as db:
        txns = [
            _txn(n["source"], n["ref"], n["amount"], narration=n["narration"], raw=n.get("raw", {}))
            for n in NEGATIVE
        ]
        db.add_all(txns)
        await db.commit()
        return {t.external_ref: t.id for t in txns}


async def _seed_singles():
    await _purge_refs([n["ref"] for n in SEPARATE_SINGLES])
    async with SessionLocal() as db:
        txns = [
            _txn(n["source"], n["ref"], n["amount"], narration=n["narration"])
            for n in SEPARATE_SINGLES
        ]
        db.add_all(txns)
        await db.commit()
        return {t.external_ref: t.id for t in txns}


async def _set_tolerance(value):
    async with SessionLocal() as db:
        row = await db.get(PolicyConfig, TOLERANCE_KEY)
        original = row.value
        row.value = value
        await db.commit()
        return original


async def _cleanup(refs):
    async with SessionLocal() as db:
        txn_ids = (
            await db.execute(
                select(Transaction.id).where(Transaction.external_ref.in_(refs))
            )
        ).scalars().all()
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
        await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))
        await db.execute(delete(Transaction).where(Transaction.id.in_(txn_ids)))
        await db.commit()


def test_rounding_trio_now_matches_all_three_sources():
    async def scenario():
        by_ref = await _seed_trio()
        refs = list(by_ref)
        original = await _set_tolerance(FIX_TOLERANCE)
        try:
            async with SessionLocal() as db:
                summary = await reconciliation.run_reconciliation(
                    db, actor="pytest-fuzzy-tolerance", transaction_ids=list(by_ref.values())
                )
                await db.commit()

            async with SessionLocal() as db:
                matches = (
                    await db.execute(
                        select(Match)
                        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                        .where(MatchParticipant.transaction_id.in_(list(by_ref.values())))
                    )
                ).scalars().unique().all()
                confirmed = [m for m in matches if m.status == "confirmed"]
                assert len(confirmed) == 1, (
                    f"rounding trio must produce exactly 1 confirmed match, got {len(confirmed)}"
                )
                match = confirmed[0]
                participants = (
                    await db.execute(
                        select(Transaction.external_ref)
                        .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
                        .where(MatchParticipant.match_id == match.id)
                    )
                ).scalars().all()
                assert set(participants) == set(refs), (
                    f"match must cover all three sources, got {sorted(participants)}"
                )
                assert "3-source agreement" in (match.rationale or "")

                exceptions = (
                    await db.execute(
                        select(ExceptionRecord).where(
                            ExceptionRecord.transaction_id.in_(list(by_ref.values()))
                        )
                    )
                ).scalars().all()
                assert not exceptions, "fully-matched trio must open no exceptions"
            return summary
        finally:
            await _set_tolerance(original)
            await _purge_refs(refs)

    summary = asyncio.run(scenario())
    assert summary["transactions_scanned"] == 3
    assert summary["exact_auto_resolved"] + summary["fuzzy_auto_resolved"] == 1


def test_unrelated_similar_amounts_are_not_fuzzy_matched():
    async def scenario():
        by_ref = await _seed_negative()
        refs = list(by_ref)
        original = await _set_tolerance(FIX_TOLERANCE)
        try:
            async with SessionLocal() as db:
                await reconciliation.run_reconciliation(
                    db, actor="pytest-fuzzy-tolerance", transaction_ids=list(by_ref.values())
                )
                await db.commit()

            async with SessionLocal() as db:
                ids = list(by_ref.values())
                matches = (
                    await db.execute(
                        select(Match)
                        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                        .where(MatchParticipant.transaction_id.in_(ids))
                    )
                ).scalars().unique().all()
                assert not matches, (
                    "unrelated amounts 0.15% apart must never be paired, "
                    "even under 0.1% tolerance"
                )

                exceptions = (
                    await db.execute(
                        select(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(ids))
                    )
                ).scalars().all()
                unmatched = [e for e in exceptions if e.exception_type == "unmatched"]
                assert len(unmatched) == 2, (
                    f"each unrelated txn must surface as 'unmatched', got {len(unmatched)}"
                )
        finally:
            await _set_tolerance(original)
            await _purge_refs(refs)

    asyncio.run(scenario())


def test_singles_pairing_does_not_apply_amount_tolerance():
    async def scenario():
        by_ref = await _seed_singles()
        refs = list(by_ref)
        original = await _set_tolerance(FIX_TOLERANCE)
        try:
            async with SessionLocal() as db:
                await reconciliation.run_reconciliation(
                    db, actor="pytest-fuzzy-tolerance", transaction_ids=list(by_ref.values())
                )
                await db.commit()

            async with SessionLocal() as db:
                ids = list(by_ref.values())
                matches = (
                    await db.execute(
                        select(Match)
                        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                        .where(MatchParticipant.transaction_id.in_(ids))
                    )
                ).scalars().unique().all()
                fuzzy = [m for m in matches if m.match_type == "fuzzy"]
                confirmed = [m for m in matches if m.status == "confirmed"]
                assert not fuzzy, (
                    "a brand-new singles pair 0.036% apart must never form "
                    "under the 0.1% tolerance"
                )
                assert not confirmed, (
                    "near-equal unrelated singles must never skip the semantic/"
                    "AI human-review gates to auto-resolve"
                )
        finally:
            await _set_tolerance(original)
            await _purge_refs(refs)

    asyncio.run(scenario())