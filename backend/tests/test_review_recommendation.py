"""Recommendation block + four-action audit tests for the review drawer.

The drawer contract under test (GET /api/v1/review-queue/exceptions/{id} and
GET /api/v1/review-queue/matches/{id}) now carries a prebuilt `recommendation`
block:

  AI-shaped evidence (semantic / ai)  -> verdict, confidence, similarity vs.
     matching.ai.similarity_autoresolve_min floor, blocked_reason, rationale.
  Rule-shaped evidence (deterministic/fuzzy/batch/sweep) -> stage + why the
     group is incomplete, e.g. "missing source(s): erp".

Every reviewer action (approve / reject / manually match / dismiss) must log
the note to the audit trail and, where the semantics say so, persist it to the
exception's resolution_note so the Human-Reviewed view shows it afterward.

Pending rule-shaped items additionally get an `analysis` block (label "AI
Analysis"): the reasoning agent classifies a hold with no actionable proposal
as likely_pending / data_quality / manual_investigation. It is generated once
per exception on first drawer open, cached in an `ai.analysis` audit row, and
never generated for items that already carry AI/semantic evidence or for items
already resolved/dismissed.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

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

SEMANTIC_HUMAN_PAIR = ("pay_Pl4tFrmZ", "INV-2026-0721")
MISSING_ERP_REFS = ("pay_Jj0k1L2m", "UTR888170260813")

# Test-only zero-candidate ref: a lone bank credit nothing can pair with.
ZERO_CANDIDATE_REF = "UTR888170260849"

ALL_REFS = ["pay_Gg7h8I9j", "INV-2026-0710", "UTR888170260810"] + [
    "pay_Ii9j0K1l", "INV-2026-0712", "UTR888170260812"
] + ["pay_Kk1l2M3n", "INV-2026-0714", "UTR888170260814"] + [
    "pay_Hh8i9J0k", "INV-2026-0711", "UTR888170260811X"
] + ["pay_Sm1Th1cA", "INV-2026-0720"] + list(SEMANTIC_HUMAN_PAIR) + list(MISSING_ERP_REFS) + [
    "CHG-Q2-FY2702"
]


def _client() -> TestClient:
    return TestClient(app)


async def _load_scope():
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(Transaction).where(Transaction.external_ref.in_(ALL_REFS)))
        ).scalars().all()
        found = {t.external_ref: t for t in rows}
        missing = set(ALL_REFS) - set(found)
        assert not missing, f"golden dataset not ingested, missing {sorted(missing)}"
        return found


async def _purge_scope(by_ref):
    """Remove matches/exceptions for the golden refs, drop thresholds for this run."""
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


def _prepare():
    """Purge the golden scope, run the pipeline once. Returns (by_ref, originals)."""

    async def scenario():
        by_ref = await _load_scope()
        originals = await _purge_scope(by_ref)
        scope_ids = [t.id for ref, t in by_ref.items() if ref != "CHG-Q2-FY2702"]
        async with SessionLocal() as db:
            await run_reconciliation(
                db, actor="pytest-rec", transaction_ids=scope_ids
            )
            await db.commit()
        return by_ref, originals

    return asyncio.run(scenario())


async def _find_proposal(*refs) -> str:
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


async def _find_exception(ref: str, exception_type: str, status: str = "open") -> str:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(ExceptionRecord)
                .join(Transaction, Transaction.id == ExceptionRecord.transaction_id)
                .where(
                    Transaction.external_ref == ref,
                    ExceptionRecord.exception_type == exception_type,
                )
            )
        ).scalars().first()
        assert row is not None, f"no '{exception_type}' exception for {ref}"
        assert row.status == status, f"exception for {ref} is '{row.status}', expected '{status}'"
        return str(row.id)


async def _audit_details(entity_type: str, entity_id: uuid.UUID, action: str) -> dict:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == entity_type,
                    AuditLog.entity_id == entity_id,
                    AuditLog.action == action,
                )
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        ).scalars().all()
        assert rows, f"no audit row for {entity_type}:{action}"
        return (rows[0].details or {}) if rows else {}


async def _exception_record(ref: str) -> tuple[uuid.UUID, str]:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(ExceptionRecord)
                .join(Transaction, Transaction.id == ExceptionRecord.transaction_id)
                .where(Transaction.external_ref == ref)
            )
        ).scalars().first()
        assert row is not None, f"no exception for {ref}"
        return row.id, row.status


def test_recommendation_block_on_ai_exception():
    """pay_Pl4tFrmZ surfaces the AI verdict with similarity vs. floor context."""
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            exc_id = asyncio.run(
                _find_exception("pay_Pl4tFrmZ", "manual_review_required")
            )
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()

            rec = detail["recommendation"]
            assert rec is not None, "exception detail must carry a recommendation block"
            assert rec["verdict"] == "needs_human"
            assert rec["stage"] == "semantic"
            assert rec["confidence_score"] is not None
            assert rec["similarity"] is not None
            assert rec["similarity_autoresolve_min"] == 0.15
            assert rec["floor_met"] is False, (
                "similarity below the auto-resolve floor must be flagged"
            )
            assert rec["blocked_reason"] in (None, "similarity_autoresolve_min"), (
                "block reason is best-effort; the similarity/floor signal is the contract"
            )
            assert rec["rationale"]
            assert rec["incomplete_reason"] is None

            # The same AI-shaped proposal shows up in the linked matches.
            assert any(
                m["status"] == "proposed" and m["match_type"] == "semantic"
                for m in detail["related_matches"]
            )
    finally:
        asyncio.run(_restore(originals))


def test_recommendation_block_on_rule_exception():
    """pay_Jj0k1L2m (deterministic, incomplete group) gets stage + why."""
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            exc_id = asyncio.run(_find_exception("pay_Jj0k1L2m", "unmatched"))
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()

            rec = detail["recommendation"]
            assert rec is not None
            assert rec["verdict"] is None, "a rule-produced proposal has no AI verdict"
            assert rec["stage"] == "deterministic"
            assert rec["similarity"] is None
            assert rec["floor_met"] is None
            assert rec["blocked_reason"] is None
            assert rec["incomplete_reason"] == "missing source(s): erp"
            assert rec["rationale"] and "missing source(s): erp" in rec["rationale"]

            # Stage confidence mirrors the persisted match's score.
            related = detail["related_matches"]
            assert related and related[0]["status"] == "proposed"
            assert rec["confidence_score"] == related[0]["confidence_score"]
    finally:
        asyncio.run(_restore(originals))


def test_recommendation_block_on_match_detail_uses_ai_evidence():
    """Opening the semantic proposal directly still yields the AI sub-95 floor verdict."""
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            proposal_id = asyncio.run(_find_proposal(*SEMANTIC_HUMAN_PAIR))
            detail = client.get(f"/api/v1/review-queue/matches/{proposal_id}").json()

            rec = detail["recommendation"]
            assert rec is not None
            assert rec["verdict"] == "needs_human"
            assert rec["stage"] == "semantic"
            assert rec["confidence_score"] is not None
            assert rec["similarity"] is not None
            assert rec["floor_met"] is False
            assert rec["blocked_reason"] in (None, "similarity_autoresolve_min")
            assert rec["rationale"]
    finally:
        asyncio.run(_restore(originals))


def test_four_actions_notes_and_audit_trail():
    """Approve / Reject / Manually Match / Dismiss persist notes + audit entries."""
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            # Dismiss — unmatched exception, no match involved.
            unmatched_id = asyncio.run(
                _find_exception("pay_Jj0k1L2m", "unmatched")
            )
            note_dismiss = "legitimate bank charge, flag was spurious"
            resp = client.post(
                f"/api/v1/review/exceptions/{unmatched_id}/dismiss",
                headers={"X-Actor": "ops-cop"},
                json={"note": note_dismiss},
            )
            assert resp.status_code == 200, resp.text
            exc_id, exc_status = asyncio.run(_exception_record("pay_Jj0k1L2m"))
            assert exc_status == "dismissed"
            audit = asyncio.run(
                _audit_details("exception", exc_id, "exception.dismissed")
            )
            assert audit["note"] == note_dismiss

            # Reject — the deterministic 2-way proposal is not enough.
            deterministic_id = asyncio.run(_find_proposal(*MISSING_ERP_REFS))
            note_reject = "two-way match cannot be confirmed without erp leg"
            resp = client.post(
                f"/api/v1/review/matches/{deterministic_id}/reject",
                headers={"X-Actor": "ops-reviewer"},
                json={"note": note_reject},
            )
            assert resp.status_code == 200, resp.text
            audit = asyncio.run(
                _audit_details("match", uuid.UUID(deterministic_id), "match.rejected")
            )
            assert audit["note"] == note_reject

            # Approve — the semantic proposal resolves its exception with the note.
            semantic_id = asyncio.run(_find_proposal(*SEMANTIC_HUMAN_PAIR))
            note_approve = "approved against settlement email"
            resp = client.post(
                f"/api/v1/review/matches/{semantic_id}/approve",
                headers={"X-Actor": "ops-reviewer"},
                json={"note": note_approve},
            )
            assert resp.status_code == 200, resp.text

            pl4_id, pl4_status = asyncio.run(
                _exception_record("pay_Pl4tFrmZ")
            )
            assert pl4_status == "resolved"
            async def _resolution_note():
                async with SessionLocal() as db:
                    row = await db.get(ExceptionRecord, pl4_id)
                    return row.resolution_note if row else None
            assert asyncio.run(_resolution_note()) == note_approve
            audit = asyncio.run(
                _audit_details("exception", pl4_id, "exception.resolved")
            )
            assert audit["note"] == note_approve
            match_audit = asyncio.run(
                _audit_details("match", uuid.UUID(semantic_id), "match.approved")
            )
            assert match_audit["note"] == note_approve

            # Manually match — reviewer pairs the gateway record to its bank leg.
            note_manual = "reviewer matched the gateway record to its bank leg"
            resp = client.post(
                "/api/v1/review/matches/manual",
                headers={"X-Actor": "ops-cop"},
                json={
                    "transaction_ids": [
                        str(by_ref["pay_Jj0k1L2m"].id),
                        str(by_ref["UTR888170260813"].id),
                    ],
                    "note": note_manual,
                },
            )
            assert resp.status_code == 200, resp.text
            manual_id = resp.json()["match_id"]
            audit = asyncio.run(
                _audit_details("match", uuid.UUID(manual_id), "match.manual_created")
            )
            assert audit["note"] == note_manual
    finally:
        asyncio.run(_restore(originals))


def _txn(source: str, ref: str, amount: str, **kw) -> Transaction:
    return Transaction(
        source=source,
        external_ref=ref,
        amount=Decimal(amount),
        direction=kw.get("direction", "credit"),
        transaction_type=kw.get("transaction_type", "settlement"),
        currency="INR",
        txn_date=kw.get("txn_date", datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)),
        narration=kw.get("narration"),
        status=kw.get("status", "processed"),
        raw=kw.get("raw", {}),
    )


async def _seed_zero_candidate(ref: str) -> uuid.UUID:
    async with SessionLocal() as db:
        txn = _txn(
            "bank",
            ref,
            "731.25",
            narration=f"NEFT-CR LONE PEAKS ELECTRONICS PVT LTD {ref}",
        )
        db.add(txn)
        await db.commit()
        return txn.id


async def _reconcile_singleton(txn_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await run_reconciliation(db, actor="pytest-rec", transaction_ids=[txn_id])
        await db.commit()


async def _purge_one(ref: str) -> None:
    async with SessionLocal() as db:
        txn = (
            await db.execute(select(Transaction).where(Transaction.external_ref == ref))
        ).scalars().first()
        if txn is None:
            return
        match_ids = (
            await db.execute(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.transaction_id == txn.id
                )
            )
        ).scalars().all()
        await db.execute(delete(ExceptionRecord).where(ExceptionRecord.transaction_id == txn.id))
        await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))
        await db.execute(delete(Transaction).where(Transaction.id == txn.id))
        await db.commit()


async def _count_analysis(exc_id: uuid.UUID) -> int:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "exception",
                    AuditLog.entity_id == exc_id,
                    AuditLog.action == "ai.analysis",
                )
            )
        ).scalars().all()
        return len(rows)


def test_ai_analysis_on_incomplete_rule_exception():
    """pay_Jj0k1L2m (deterministic 2-way group, erp leg missing) gets an AI
    Analysis on first open: missing source surfaced, cached once, identical on
    reopen."""
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            exc_id = asyncio.run(_find_exception("pay_Jj0k1L2m", "unmatched"))
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()

            rec = detail["recommendation"]
            assert rec is not None
            assert rec["verdict"] is None
            assert rec["stage"] == "deterministic"
            assert rec["incomplete_reason"] == "missing source(s): erp"

            # On first open the analysis is generated off the request path: the
            # drawer returns immediately with status "pending" and no analysis.
            assert detail["analysis_status"] == "pending"
            assert rec["analysis"] is None, (
                "first open must return fast; the analysis streams in separately"
            )

            # The background task has run by now (TestClient drains it), so the
            # analysis endpoint serves the generated + cached read.
            poll = client.get(f"/api/v1/review-queue/exceptions/{exc_id}/analysis").json()
            assert poll is not None
            assert poll["label"] == "AI Analysis"
            assert poll["classification"] in (
                "likely_pending",
                "data_quality",
                "manual_investigation",
            )
            assert poll["rationale"]
            assert poll["model"] == "heuristic-offline"
            assert poll["missing_sources"] == ["erp"]

            # The AI Analysis is cached: reopening returns the same payload as
            # "ready" and does not re-call the model (exactly one ai.analysis
            # audit row).
            again = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()
            assert again["analysis_status"] == "ready"
            analysis_again = again["recommendation"]["analysis"]
            assert analysis_again["classification"] == poll["classification"]
            assert analysis_again["rationale"] == poll["rationale"]
            assert asyncio.run(_count_analysis(uuid.UUID(exc_id))) == 1
    finally:
        asyncio.run(_restore(originals))


def test_ai_analysis_on_zero_candidate_exception():
    """A lone unmatched bank credit (sweep, no proposal, no candidates) gets an
    AI Analysis: every other source is missing, cached once per exception."""
    by_ref, originals = _prepare()
    try:
        singleton_id = asyncio.run(_seed_zero_candidate(ZERO_CANDIDATE_REF))
        asyncio.run(_reconcile_singleton(singleton_id))
        with _client() as client:
            exc_id = asyncio.run(_find_exception(ZERO_CANDIDATE_REF, "unmatched"))
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()

            rec = detail["recommendation"]
            assert rec is not None
            assert rec["stage"] == "sweep"
            assert rec["verdict"] is None
            assert rec["incomplete_reason"] == "no candidate produced by any matcher stage"

            # Generated off the request path: pending on first open, then
            # served by the analysis endpoint once the background task lands.
            assert detail["analysis_status"] == "pending"
            assert rec["analysis"] is None
            analysis = client.get(
                f"/api/v1/review-queue/exceptions/{exc_id}/analysis"
            ).json()
            assert analysis is not None
            assert analysis["label"] == "AI Analysis"
            assert analysis["classification"] in (
                "likely_pending",
                "data_quality",
                "manual_investigation",
            )
            assert analysis["model"] == "heuristic-offline"
            assert analysis["missing_sources"] == ["erp", "razorpay"]
            assert isinstance(analysis["below_threshold_candidates"], list)

            again = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()
            assert again["analysis_status"] == "ready"
            assert (
                again["recommendation"]["analysis"]["classification"]
                == analysis["classification"]
            )
            assert asyncio.run(_count_analysis(uuid.UUID(exc_id))) == 1
    finally:
        asyncio.run(_purge_one(ZERO_CANDIDATE_REF))
        asyncio.run(_restore(originals))


def test_ai_analysis_not_generated_for_semantic_exception():
    """pay_Pl4tFrmZ already carries AI/semantic evidence — the recommendation
    block is left untouched and no ai.analysis is ever generated."""
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            exc_id = asyncio.run(
                _find_exception("pay_Pl4tFrmZ", "manual_review_required")
            )
            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()

            rec = detail["recommendation"]
            assert rec is not None
            assert rec["stage"] == "semantic"
            assert rec["verdict"] == "needs_human"
            assert rec["analysis"] is None, (
                "AI-shaped items keep their recommendation block untouched"
            )
            assert asyncio.run(_count_analysis(uuid.UUID(exc_id))) == 0
    finally:
        asyncio.run(_restore(originals))


def test_ai_analysis_not_generated_after_dismiss():
    """The cache/generation contract is strictly for pending items: a dismissed
    exception is never analyzed, now or later."""
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            exc_id = asyncio.run(_find_exception("pay_Jj0k1L2m", "unmatched"))
            resp = client.post(
                f"/api/v1/review/exceptions/{exc_id}/dismiss",
                headers={"X-Actor": "ops-cop"},
                json={"note": "legitimate bank charge, flag was spurious"},
            )
            assert resp.status_code == 200, resp.text

            detail = client.get(f"/api/v1/review-queue/exceptions/{exc_id}").json()
            assert detail["status"] == "dismissed"
            rec = detail["recommendation"]
            assert rec["analysis"] is None, "resolved/dismissed items are never analyzed"
            assert asyncio.run(_count_analysis(uuid.UUID(exc_id))) == 0
    finally:
        asyncio.run(_restore(originals))