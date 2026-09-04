"""Phase 4 HITL review dashboard regression tests.

Covers the golden dataset's known human-decision cases end-to-end at the
API layer:

    pay_Jj0k1L2m (missing ERP leg) -> 'unmatched' exception in queue;
        dismissible; detail endpoint ranks UTR888170260813 as top candidate.
    pay_Pl4tFrmZ (AI needs_human)  -> 'manual_review_required' exception +
        semantic proposal in queue with full AI rationale visible.

Every human action must:
  - reuse the pipeline's own data model (matches.resolved_by='human',
    decided_by, exceptions.status transitions) - no parallel path,
  - write audit_logs rows with actor + before/after state.
"""

import asyncio
import uuid

import pytest
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
    "pay_Sm1Th1cA", "INV-2026-0720",
    "pay_Pl4tFrmZ", "INV-2026-0721",
    "pay_Jj0k1L2m", "UTR888170260813",
]
ACTOR = {"h": "X-Actor"}


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


async def _purge(refs):
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
        from sqlalchemy import delete

        await db.execute(
            delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(txn_ids))
        )
        await db.execute(
            delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids))
        )
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))
        row = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
        original_t = row.value
        row.value = 0.10
        gate_row = await db.get(PolicyConfig, "matching.ai.similarity_autoresolve_min")
        original_g = gate_row.value
        # Hermetic env uses the hashing double (low similarities); 0.15 keeps
        # the pay_Sm1Th1cA pair ai-eligible while the weak pair stays gated.
        gate_row.value = 0.15
        await db.commit()
        return original_t, original_g


async def _restore_threshold(originals):
    async with SessionLocal() as db:
        row = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
        row.value = originals[0]
        gate_row = await db.get(PolicyConfig, "matching.ai.similarity_autoresolve_min")
        gate_row.value = originals[1]
        await db.commit()


def _prepare():
    """Purge scope, drop semantic threshold, run pipeline once. Returns (ids, original_threshold)."""
    async def scenario():
        ids = await _ids_by_ref()
        original = await _purge(SCOPE_REFS)
        async with SessionLocal() as db:
            await run_reconciliation(
                db, actor="pytest-review", transaction_ids=[ids[r] for r in SCOPE_REFS]
            )
            await db.commit()
        return ids, original

    return asyncio.run(scenario())


async def _find_exception_txn(ref: str, etype: str, status: str | None = None):
    async with SessionLocal() as db:
        stmt = (
            select(ExceptionRecord)
            .join(Transaction, Transaction.id == ExceptionRecord.transaction_id)
            .where(Transaction.external_ref == ref, ExceptionRecord.exception_type == etype)
        )
        if status:
            stmt = stmt.where(ExceptionRecord.status == status)
        return str((await db.execute(stmt.order_by(ExceptionRecord.opened_at.desc()).limit(1))).scalar_one().id)


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


async def _audit_rows(entity_id: str, action: str):
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == uuid.UUID(entity_id), AuditLog.action == action)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            )
        ).scalars().all()
        return rows


def test_queue_surfaces_both_golden_cases_with_filters_and_sort():
    _ids, original = _prepare()
    try:
        with _client() as client:
            queue = client.get("/api/v1/review-queue").json()
            items = queue["items"]
            assert queue["counts"]["proposals"] >= 2

            jj0_items = [i for i in items if i["item_type"] == "exception"
                         and i["exception_type"] == "unmatched"
                         and "pay_Jj0k1L2m" in i["refs"]]
            assert jj0_items, f"pay_Jj0k1L2m unmatched missing from queue: {items}"
            assert jj0_items[0]["status"] == "open"

            pl4_items = [i for i in items if i["item_type"] == "exception"
                         and i["exception_type"] == "manual_review_required"
                         and "pay_Pl4tFrmZ" in i["refs"]]
            assert pl4_items, "pay_Pl4tFrmZ manual_review_required missing from queue"

            pl4_proposals = [i for i in items if i["item_type"] == "proposal"
                             and "pay_Pl4tFrmZ" in i["refs"] and "INV-2026-0721" in i["refs"]]
            assert pl4_proposals, "semantic proposal for pay_Pl4tFrmZ missing from queue"
            assert pl4_proposals[0]["match_type"] == "semantic"
            assert pl4_proposals[0]["rationale"], "proposal rationale must be surfaced in queue"

            filtered = client.get("/api/v1/review-queue?item_type=proposal").json()["items"]
            assert filtered and all(i["item_type"] == "proposal" for i in filtered)
            filtered = client.get(
                "/api/v1/review-queue?exception_type=unmatched&status=open"
            ).json()["items"]
            assert filtered and all(i["exception_type"] == "unmatched" for i in filtered)

            sorted_desc = client.get(
                "/api/v1/review-queue?sort_by=amount_impact&order=desc"
            ).json()["items"]
            amounts = [float(i["amount_impact"]) for i in sorted_desc if i["amount_impact"]]
            assert amounts == sorted(amounts, reverse=True), f"not amount-sorted: {amounts}"
            sorted_asc = client.get(
                "/api/v1/review-queue?sort_by=amount_impact&order=asc"
            ).json()["items"]
            amounts_asc = [float(i["amount_impact"]) for i in sorted_asc if i["amount_impact"]]
            assert amounts_asc == sorted(amounts_asc)

            priority_only = client.get("/api/v1/review-queue?priority=critical").json()["items"]
            if priority_only:
                assert all(i["priority"] == "critical" for i in priority_only)
    finally:
        asyncio.run(_restore_threshold(original))


def test_exception_detail_shows_raw_record_and_ranked_candidates():
    _ids, original = _prepare()
    try:
        with _client() as client:
            exc_id = asyncio.run(_find_exception_txn("pay_Jj0k1L2m", "unmatched"))
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()

            assert detail["transaction"]["external_ref"] == "pay_Jj0k1L2m"
            assert detail["transaction"]["raw"], "raw source payload required for side-by-side view"

            candidates = detail["candidates"]
            assert candidates, "expected ranked candidates for unmatched gateway record"
            top = candidates[0]
            assert top["external_ref"] == "UTR888170260813", (
                f"expected bank leg as top candidate, got {candidates[:3]}"
            )
            scores = [c["score"] for c in candidates]
            assert scores == sorted(scores, reverse=True)

            pl4_exc = asyncio.run(_find_exception_txn("pay_Pl4tFrmZ", "manual_review_required"))
            pl4_detail = client.get(f"/api/v1/review-queue/exceptions/{pl4_exc}").json()
            related = pl4_detail["related_matches"]
            assert related, "AI proposal must be linked to its manual_review exception"
            assert related[0]["match_type"] == "semantic"
            assert related[0]["confidence_score"] == "0.5500"
            assert related[0]["rationale"], "full AI rationale text must be exposed"
            assert "needs_human" in related[0]["rationale"].lower() or "similarity" in related[0]["rationale"].lower()
    finally:
        asyncio.run(_restore_threshold(original))


def test_approve_semantic_proposal_updates_model_and_writes_audit():
    _ids, original = _prepare()
    try:
        with _client() as client:
            proposal_id = asyncio.run(_find_proposal("pay_Pl4tFrmZ", "INV-2026-0721"))
            exc_id = asyncio.run(
                _find_exception_txn("pay_Pl4tFrmZ", "manual_review_required", "open")
            )

            response = client.post(
                f"/api/v1/review/matches/{proposal_id}/approve",
                headers={"X-Actor": "ops-tester"},
                json={"note": "verified against settlement email"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["resolved_exceptions"] >= 1

            async def check():
                async with SessionLocal() as db:
                    match = await db.get(Match, uuid.UUID(proposal_id))
                    return match

            match = asyncio.run(check())
            assert match.status == "confirmed"
            assert match.resolved_by == "human"
            assert match.decided_by == "ops-tester"
            assert match.resolved_at is not None

            async def check_exc():
                async with SessionLocal() as db:
                    exc = await db.get(ExceptionRecord, uuid.UUID(exc_id))
                    return exc

            exc = asyncio.run(check_exc())
            assert exc.status == "resolved"
            assert exc.assigned_to == "ops-tester"
            assert exc.resolved_at is not None

            approvals = asyncio.run(_audit_rows(proposal_id, "match.approved"))
            assert approvals, "match.approved audit row missing"
            entry = approvals[0]
            assert entry.actor == "ops-tester"
            assert entry.before_state["status"] == "proposed"
            assert entry.after_state["status"] == "confirmed"

            resolutions = asyncio.run(_audit_rows(exc_id, "exception.resolved"))
            assert resolutions, "exception.resolved audit row missing"

            conflict = client.post(
                f"/api/v1/review/matches/{proposal_id}/approve",
                headers={"X-Actor": "ops-tester"},
                json={},
            )
            assert conflict.status_code == 409, "approving a confirmed match must be rejected"
    finally:
        asyncio.run(_restore_threshold(original))


def test_reject_keeps_exception_open_then_escalate_and_dismiss_work():
    _ids, original = _prepare()
    try:
        with _client() as client:
            proposal_id = asyncio.run(_find_proposal("pay_Pl4tFrmZ", "INV-2026-0721"))
            exc_id = asyncio.run(
                _find_exception_txn("pay_Pl4tFrmZ", "manual_review_required", "open")
            )
            jj0_exc = asyncio.run(_find_exception_txn("pay_Jj0k1L2m", "unmatched", "open"))

            response = client.post(
                f"/api/v1/review/matches/{proposal_id}/reject",
                headers={"X-Actor": "ops-tester"},
                json={"note": "wrong counterparty"},
            )
            assert response.status_code == 200

            async def states():
                async with SessionLocal() as db:
                    match = await db.get(Match, uuid.UUID(proposal_id))
                    exc = await db.get(ExceptionRecord, uuid.UUID(exc_id))
                    return match, exc

            match, exc = asyncio.run(states())
            assert match.status == "rejected"
            assert match.decided_by == "ops-tester"
            assert exc.status == "open", "rejecting a proposal must keep the exception open"

            rejections = asyncio.run(_audit_rows(proposal_id, "match.rejected"))
            assert rejections and rejections[0].actor == "ops-tester"
            assert rejections[0].before_state["status"] == "proposed"
            assert rejections[0].after_state["status"] == "rejected"

            escalated = client.post(
                f"/api/v1/review/exceptions/{exc_id}/escalate",
                headers={"X-Actor": "ops-tester"},
                json={"note": "needs finance lead"},
            )
            assert escalated.status_code == 200
            escalations = asyncio.run(_audit_rows(exc_id, "exception.escalated"))
            assert escalations and escalations[0].after_state["status"] == "escalated"

            dismissed = client.post(
                f"/api/v1/review/exceptions/{jj0_exc}/dismiss",
                headers={"X-Actor": "ops-tester"},
                json={"note": "legitimate charge"},
            )
            assert dismissed.status_code == 200
            dismissal_rows = asyncio.run(_audit_rows(jj0_exc, "exception.dismissed"))
            assert dismissal_rows, "exception.dismissed audit row missing"
            assert dismissal_rows[0].before_state["status"] == "open"
            assert dismissal_rows[0].after_state["resolution_note"] == "legitimate charge"
    finally:
        asyncio.run(_restore_threshold(original))


def test_manual_match_is_first_class_and_fully_audited():
    _ids, original = _prepare()
    try:
        with _client() as client:
            jj0_exc = asyncio.run(_find_exception_txn("pay_Jj0k1L2m", "unmatched", "open"))
            jj0_txn = _ids["pay_Jj0k1L2m"]
            erp_candidate = _ids["INV-2026-0721"]

            response = client.post(
                "/api/v1/review/matches/manual",
                headers={"X-Actor": "ops-tester"},
                json={
                    "transaction_ids": [str(jj0_txn), str(erp_candidate)],
                    "note": "reviewer identified correct ERP invoice",
                    "replace_proposed_match_id": None,
                },
            )
            assert response.status_code == 200, response.text
            new_match_id = response.json()["match_id"]

            async def check():
                async with SessionLocal() as db:
                    match = await db.get(Match, uuid.UUID(new_match_id))
                    exc = await db.get(ExceptionRecord, uuid.UUID(jj0_exc))
                    return match, exc

            match, exc = asyncio.run(check())
            assert match.match_type == "manual"
            assert match.status == "confirmed"
            assert match.resolved_by == "human"
            assert match.decided_by == "ops-tester"
            assert exc.status == "resolved"

            creations = asyncio.run(_audit_rows(new_match_id, "match.manual_created"))
            assert creations, "match.manual_created audit row missing"
            assert creations[0].actor == "ops-tester"
            assert creations[0].after_state["members"] == ["pay_Jj0k1L2m", "INV-2026-0721"]

            conflict = client.post(
                "/api/v1/review/matches/manual",
                headers={"X-Actor": "ops-tester"},
                json={
                    "transaction_ids": [str(_ids["INV-2026-0720"]), str(_ids["UTR888170260813"])],
                },
            )
            assert conflict.status_code == 409, "double-booking a confirmed member must fail"

            summary = client.get("/api/v1/dashboard/summary").json()
            for key in (
                "open_exceptions_total",
                "exceptions_by_type",
                "exceptions_by_priority",
                "proposals_awaiting_review",
                "decisions_today_total",
                "auto_resolved_today",
                "human_resolved_today",
                "exceptions_closed_today",
            ):
                assert key in summary, f"dashboard missing {key}"
            assert isinstance(summary["decisions_today_total"], int)
            assert summary["human_resolved_today"] >= 1
    finally:
        asyncio.run(_restore_threshold(original))
