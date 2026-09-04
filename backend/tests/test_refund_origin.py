"""Refund-for-origin resolution for the review drawer.

A refund exception must surface the *original* settlement it reverses (via the
shared payment_id / payment_ref) as read-only context — resolved at detail time,
never created as a match. This test seeds a refund + its original settlement
directly, plus a decoy settlement, and asserts the drawer resolves the correct
one and leaves non-refund exceptions untouched.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.main import app
from app.models import ExceptionRecord, Transaction

REF = {
    "original": "pay_Or1G1nL1nk",
    "refund": "pay_Or1G1nL1nk-REFUND",
    "decoy": "pay_D3C0yN",
}


def _client() -> TestClient:
    return TestClient(app)


def _make_txn(
    external_ref: str,
    *,
    amount: str,
    direction: str,
    transaction_type: str,
    payment_id: str,
) -> Transaction:
    return Transaction(
        source="razorpay",
        external_ref=external_ref,
        amount=Decimal(amount),
        direction=direction,
        transaction_type=transaction_type,
        txn_date=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        status="processed",
        raw={"payment_id": payment_id},
    )


async def _seed():
    async with SessionLocal() as db:
        db.add_all(
            [
                _make_txn(
                    REF["original"],
                    amount="12455.75",
                    direction="credit",
                    transaction_type="settlement",
                    payment_id=REF["original"],
                ),
                _make_txn(
                    REF["refund"],
                    amount="1200.00",
                    direction="debit",
                    transaction_type="refund",
                    payment_id=REF["original"],
                ),
                _make_txn(
                    REF["decoy"],
                    amount="999.00",
                    direction="credit",
                    transaction_type="settlement",
                    payment_id=REF["decoy"],
                ),
            ]
        )
        await db.flush()
        refund = (
            await db.execute(
                select(Transaction).where(Transaction.external_ref == REF["refund"])
            )
        ).scalars().first()
        exc = ExceptionRecord(
            transaction_id=refund.id,
            exception_type="refund",
            priority="high",
            amount_impact=refund.amount,
            status="open",
        )
        db.add(exc)
        await db.commit()
        return exc.id


async def _cleanup():
    async with SessionLocal() as db:
        txn_ids = (
            await db.execute(
                select(Transaction.id).where(Transaction.external_ref.in_(REF.values()))
            )
        ).scalars().all()
        await db.execute(
            delete(ExceptionRecord).where(
                ExceptionRecord.transaction_id.in_(txn_ids)
            )
        )
        await db.execute(delete(Transaction).where(Transaction.id.in_(txn_ids)))
        await db.commit()


def test_refund_exception_detail_resolves_original_settlement():
    exc_id = asyncio.run(_seed())
    try:
        with _client() as client:
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()

            assert detail["exception_type"] == "refund"
            assert detail["transaction"]["external_ref"] == REF["refund"]

            origin = detail["original_transaction"]
            assert origin is not None, "refund drawer must resolve its original settlement"
            assert origin["external_ref"] == REF["original"]
            assert origin["source"] == "razorpay"
            assert origin["direction"] == "credit"
            assert origin["transaction_type"] == "settlement"
            assert origin["amount"] == "12455.75"
            assert origin["id"] != detail["transaction"]["id"], "origin must not be the refund itself"
    finally:
        asyncio.run(_cleanup())


def test_refund_resolution_ignores_unrelated_settlements():
    exc_id = asyncio.run(_seed())
    try:
        with _client() as client:
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()
            origin = detail["original_transaction"]
            assert origin is not None
            assert origin["external_ref"] == REF["original"]
            assert origin["external_ref"] != REF["decoy"], (
                "resolver must return the shared-payment origin, not an unrelated settlement"
            )
    finally:
        asyncio.run(_cleanup())


def test_non_refund_exception_has_no_original_transaction():
    exc_id = asyncio.run(_seed())
    try:
        async def _flip_to_unmatched():
            async with SessionLocal() as db:
                exc = await db.get(ExceptionRecord, exc_id)
                exc.exception_type = "unmatched"
                await db.commit()

        asyncio.run(_flip_to_unmatched())
        with _client() as client:
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()
            assert detail["exception_type"] == "unmatched"
            assert detail["original_transaction"] is None, (
                "only refund exceptions carry an original settlement"
            )
    finally:
        asyncio.run(_cleanup())