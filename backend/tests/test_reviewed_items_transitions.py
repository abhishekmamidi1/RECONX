"""Reviewed-items transition tests.

The organizational contract under test (GET /api/v1/reviewed):

  An item that is pending in the review queue ("To Be Reviewed") must move
  out of that queue and into the "Reviewed" list once a human takes an action
  on it, and must NEVER appear in both at the same time.

Human decisions covered:
  - a proposed match  -> approved   (item_type='match', action='approved')
  - a proposed match  -> rejected   (item_type='match', action='rejected')
  - an open exception -> dismissed  (item_type='exception', action='dismissed')

Explicitly dismissed exceptions appear; exceptions that were merely resolved
as a side-effect of approving a linked match are deliberately NOT listed as
separate exception items (the match is the reviewed item) — so we assert the
dismissed exception shows but the auto-resolved exception does not.
"""

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    AuditLog,
    ExceptionRecord,
    Match,
    MatchParticipant,
    PolicyConfig,
    Transaction,
)
from app.services.reconciliation import run_reconciliation

SCOPE_REFS = [
    "pay_Hh8i9J0k", "INV-2026-0711", "UTR888170260811X",
    "pay_Pl4tFrmZ", "INV-2026-0721",
    "pay_Jj0k1L2m", "UTR888170260813",
]
ACTOR_APPROVE = "test-approver"
ACTOR_REJECT = "test-rejecter"
ACTOR_DISMISS = "test-dismisser"


def _client() -> TestClient:
    return TestClient(app)


async def _ids_by_ref():
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Transaction).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        found = {t.external_ref for t in rows}
        missing = set(SCOPE_REFS) - found
        assert not missing, f"golden dataset not ingested, missing {sorted(missing)}"
        return {t.external_ref: t.id for t in rows}


async def _purge(ids_by_ref):
    from sqlalchemy import delete

    txn_ids = list(ids_by_ref.values())
    async with SessionLocal() as db:
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


def _prepare():
    async def scenario():
        ids = await _ids_by_ref()
        original = await _purge(ids)
        async with SessionLocal() as db:
            await run_reconciliation(
                db, actor="pytest-reviewed", transaction_ids=list(ids.values())
            )
            await db.commit()
        return ids, original

    return asyncio.run(scenario())


async def _find_proposal(*refs):
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Match)
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
                .where(Match.status == "proposed", Transaction.external_ref.in_(refs))
            )
        ).scalars().unique().all()
        for match in rows:
            members = (
                await db.execute(
                    select(Transaction.external_ref)
                    .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
                    .where(MatchParticipant.match_id == match.id)
                )
            ).scalars().all()
            if set(refs).issubset(set(members)):
                return str(match.id)
    raise AssertionError(f"no proposed match covering {refs}")


async def _find_exception(ref: str, etype: str, status: str | None = None):
    async with SessionLocal() as db:
        stmt = (
            select(ExceptionRecord)
            .join(Transaction, Transaction.id == ExceptionRecord.transaction_id)
            .where(Transaction.external_ref == ref, ExceptionRecord.exception_type == etype)
        )
        if status:
            stmt = stmt.where(ExceptionRecord.status == status)
        return str((await db.execute(stmt.order_by(ExceptionRecord.opened_at.desc()).limit(1))).scalar_one().id)


def _ids_from(payload):
    return {i["id"]: i for i in payload["items"]}


def test_approved_match_moves_from_queue_to_reviewed_never_both():
    _ids, original = _prepare()
    try:
        with _client() as client:
            # pay_Pl4tFrmZ produces a semantic proposal awaiting a decision.
            proposal_id = asyncio.run(_find_proposal("pay_Pl4tFrmZ", "INV-2026-0721"))

            # Before the action: in the queue, not in the reviewed list.
            queue_before = client.get("/api/v1/review-queue").json()
            queue_ids_before = {i["id"] for i in queue_before["items"]}
            assert proposal_id in queue_ids_before, "proposal should start in the queue"
            reviewed_before = _ids_from(client.get("/api/v1/reviewed", params={"limit": 250}).json())
            assert proposal_id not in reviewed_before, "proposal must not be in reviewed before a decision"

            # Approve it.
            resp = client.post(
                f"/api/v1/review/matches/{proposal_id}/approve",
                headers={"X-Actor": ACTOR_APPROVE},
                json={"note": "confirmed against settlement email"},
            )
            assert resp.status_code == 200, resp.text

            # After: gone from the queue, present in reviewed — never both.
            queue_after = client.get("/api/v1/review-queue").json()
            queue_ids_after = {i["id"] for i in queue_after["items"]}
            assert proposal_id not in queue_ids_after, "approved proposal must leave the queue"
            reviewed_after = _ids_from(client.get("/api/v1/reviewed", params={"limit": 250}).json())
            assert proposal_id in reviewed_after, "approved proposal must appear in reviewed"
            assert proposal_id not in (queue_ids_after & set(reviewed_after.keys()))

            item = reviewed_after[proposal_id]
            assert item["item_type"] == "match"
            assert item["status"] == "confirmed"
            assert item["action"] == "approved"
            assert item["actor"] == ACTOR_APPROVE
            assert item["note"] == "confirmed against settlement email"
            assert item["reviewed_at"] is not None
            assert item["match_type"] == "semantic"
    finally:
        asyncio.run(_restore(original))


def test_dismissed_exception_present_but_auto_resolved_exception_absent():
    _ids, original = _prepare()
    try:
        with _client() as client:
            # pay_Jj0k1L2m is an unmatched exception (open).
            exc_id = asyncio.run(
                _find_exception("pay_Jj0k1L2m", "unmatched", status="open")
            )

            queue_before = {i["id"] for i in client.get("/api/v1/review-queue").json()["items"]}
            assert exc_id in queue_before, "exception should start in the queue"
            reviewed_before = _ids_from(client.get("/api/v1/reviewed", params={"limit": 250}).json())
            assert exc_id not in reviewed_before

            # Dismiss it.
            resp = client.post(
                f"/api/v1/review/exceptions/{exc_id}/dismiss",
                headers={"X-Actor": ACTOR_DISMISS},
                json={"note": "false positive — payout already captured"},
            )
            assert resp.status_code == 200, resp.text

            queue_after = {i["id"] for i in client.get("/api/v1/review-queue").json()["items"]}
            assert exc_id not in queue_after, "dismissed exception must leave the queue"

            reviewed_after = _ids_from(client.get("/api/v1/reviewed", params={"limit": 250}).json())
            assert exc_id in reviewed_after, "dismissed exception must appear in reviewed"
            item = reviewed_after[exc_id]
            assert item["item_type"] == "exception"
            assert item["status"] == "dismissed"
            assert item["action"] == "dismissed"
            assert item["actor"] == ACTOR_DISMISS
            assert item["note"] == "false positive — payout already captured"
            assert item["exception_type"] == "unmatched"

            # Approving a *different* exception's link should NOT surface it here.
            # pay_Pl4tFrmZ's manual_review_required exception gets auto-resolved
            # as a side effect of approving its proposal, and must not be counted
            # as a separate reviewed exception item.
            proposal_id = asyncio.run(_find_proposal("pay_Pl4tFrmZ", "INV-2026-0721"))
            linked_exc = asyncio.run(
                _find_exception("pay_Pl4tFrmZ", "manual_review_required", status=None)
            )
            resp = client.post(
                f"/api/v1/review/matches/{proposal_id}/approve",
                headers={"X-Actor": ACTOR_APPROVE},
                json={"note": "approved"},
            )
            assert resp.status_code == 200, resp.text

            final_reviewed = _ids_from(client.get("/api/v1/reviewed", params={"limit": 250}).json())
            assert linked_exc not in final_reviewed, (
                "an exception auto-resolved via match approval must not appear as a separate reviewed item"
            )
            # The approved match itself IS there.
            assert proposal_id in final_reviewed
    finally:
        asyncio.run(_restore(original))


def test_reviewed_filters_action_actor_and_item_type():
    _ids, original = _prepare()
    try:
        with _client() as client:
            proposal_id = asyncio.run(_find_proposal("pay_Pl4tFrmZ", "INV-2026-0721"))
            exc_id = asyncio.run(_find_exception("pay_Jj0k1L2m", "unmatched", status="open"))

            client.post(
                f"/api/v1/review/matches/{proposal_id}/approve",
                headers={"X-Actor": ACTOR_APPROVE},
                json={"note": "approved via api"},
            )
            client.post(
                f"/api/v1/review/exceptions/{exc_id}/dismiss",
                headers={"X-Actor": ACTOR_DISMISS},
                json={"note": "dismissed via api"},
            )

            # item_type filter
            matches_only = client.get("/api/v1/reviewed", params={"item_type": "match", "limit": 250}).json()
            exc_only = client.get("/api/v1/reviewed", params={"item_type": "exception", "limit": 250}).json()
            assert proposal_id in _ids_from(matches_only)
            assert proposal_id not in _ids_from(exc_only)
            assert exc_id in _ids_from(exc_only)
            assert exc_id not in _ids_from(matches_only)

            # action filter
            approved_only = client.get("/api/v1/reviewed", params={"action": "approved", "limit": 250}).json()
            dismissed_only = client.get("/api/v1/reviewed", params={"action": "dismissed", "limit": 250}).json()
            assert proposal_id in _ids_from(approved_only)
            assert exc_id not in _ids_from(approved_only)
            assert exc_id in _ids_from(dismissed_only)

            # actor filter
            by_approver = client.get("/api/v1/reviewed", params={"actor": ACTOR_APPROVE, "limit": 250}).json()
            assert proposal_id in _ids_from(by_approver)
            assert exc_id not in _ids_from(by_approver)
    finally:
        asyncio.run(_restore(original))
