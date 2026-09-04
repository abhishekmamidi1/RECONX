"""Regression: the similarity-band gate must block overconfident weak matches.

The canonical failure: pay_Pl4tFrmZ / INV-2026-0721 have no shared reference,
near-but-not-equal amounts (0.90 INR), weak narration corroboration and bge-m3
similarity ~0.571. qwen3:0.6b declared "match" at confidence 0.9-1.0 there.
This test drives the FULL pipeline with an agent that ALWAYS says high-
confidence "match", then asserts the joint-evidence gate still forces
manual_review_required for the weak pair while leaving the genuine pair
(pay_Sm1Th1cA / INV-2026-0720, higher similarity) auto-resolvable.
"""

import asyncio
from decimal import Decimal

from app.db.session import SessionLocal
from app.models import AuditLog, ExceptionRecord, Match, MatchParticipant, PolicyConfig, Transaction
from app.services.reasoning.base import ReasoningDecision
from app.services.reconciliation import run_reconciliation
from sqlalchemy import delete, select

SCOPE_REFS = [
    "pay_Pl4tFrmZ", "INV-2026-0721",
    "pay_Sm1Th1cA", "INV-2026-0720",
]


class AlwaysMatchAgent:
    name = "ollama:qwen3:0.6b-probe"

    async def decide(self, txn, candidates, policy):
        return ReasoningDecision(
            decision="match",
            confidence=0.99,
            rationale="(regression probe) model confidently declares a match",
        )


async def _ids():
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Transaction).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        found = {t.external_ref: t for t in rows}
        missing = set(SCOPE_REFS) - set(found)
        assert not missing, f"dataset not ingested: {sorted(missing)}"
        return {ref: t.id for ref, t in found.items()}


async def _purge(db, ids):
    match_ids = (
        await db.execute(
            select(MatchParticipant.match_id).where(MatchParticipant.transaction_id.in_(ids))
        )
    ).scalars().all()
    if match_ids:
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))
    await db.execute(delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(ids)))
    await db.commit()


class TestSimilarityGate:
    def test_overconfident_agent_is_still_blocked_on_the_weak_pair(self, monkeypatch):
        import app.services.reconciliation.engine as engine

        async def scenario():
            ids = await _ids()
            by_ref = {ref: _id for ref, _id in ids.items()}

            async with SessionLocal() as db:
                await _purge(db, list(ids.values()))
                t = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
                g = await db.get(PolicyConfig, "matching.ai.similarity_autoresolve_min")
                orig_t, orig_g = t.value, g.value
                t.value = 0.10
                g.value = 0.15  # hashing double similarities are much lower than bge-m3
                await db.commit()

            try:
                async with SessionLocal() as db:
                    summary = await run_reconciliation(
                        db, actor="pytest-sim-gate", transaction_ids=list(ids.values())
                    )
                    await db.commit()

                async with SessionLocal() as db:
                    # pay_Pl4tFrmZ must be held for human review, NOT ai-resolved.
                    pl4t_exceptions = (
                        await db.execute(
                            select(ExceptionRecord).where(
                                ExceptionRecord.transaction_id == by_ref["pay_Pl4tFrmZ"]
                            )
                        )
                    ).scalars().all()
                    assert pl4t_exceptions, "weak pair must open an exception"
                    for exc in pl4t_exceptions:
                        assert exc.exception_type == "manual_review_required", (
                            f"expected manual_review_required, got {exc.exception_type}"
                        )
                    from app.models import AuditLog

                    # The blocker reason is captured on the exception.opened audit row.
                    open_audits = (
                        await db.execute(
                            select(AuditLog).where(
                                AuditLog.action == "exception.opened",
                                AuditLog.details["transaction_ref"].astext == "pay_Pl4tFrmZ",
                            ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                        )
                    ).scalars().all()
                    assert open_audits, "exception.opened audit must exist for the weak pair"
                    assert (open_audits[0].details or {}).get("auto_resolve_blocked_by") == (
                        "similarity_autoresolve_min"
                    ), f"gate must be the blocker: {open_audits[0].details}"

                    # No AI-confirmed match may touch the weak pair.
                    weak_matches = (
                        await db.execute(
                            select(Match)
                            .join(
                                MatchParticipant,
                                MatchParticipant.match_id == Match.id,
                            )
                            .where(
                                MatchParticipant.transaction_id.in_(
                                    [by_ref["pay_Pl4tFrmZ"], by_ref["INV-2026-0721"]]
                                )
                            )
                        )
                    ).scalars().unique().all()
                    ai_confirmed_weak = [
                        m
                        for m in weak_matches
                        if m.match_type == "ai" and m.status == "confirmed"
                    ]
                    assert not ai_confirmed_weak, "weak pair must NEVER auto-resolve"

                    # Positive control: the genuine pair still auto-resolves.
                    s2_matches = (
                        await db.execute(
                            select(Match)
                            .join(
                                MatchParticipant,
                                MatchParticipant.match_id == Match.id,
                            )
                            .where(
                                MatchParticipant.transaction_id.in_(
                                    [by_ref["pay_Sm1Th1cA"], by_ref["INV-2026-0720"]]
                                )
                            )
                        )
                    ).scalars().unique().all()
                    ai_confirmed_s2 = [
                        m
                        for m in s2_matches
                        if m.match_type == "ai" and m.status == "confirmed"
                    ]
                    assert ai_confirmed_s2, "genuine high-similarity pair must still auto-resolve"
                    assert Decimal(str(ai_confirmed_s2[0].confidence_score)) >= Decimal("0.90")

                    # Audit trail records the joint gate for the blocked pair.
                    ai_rows = (
                        await db.execute(
                            select(AuditLog).where(
                                AuditLog.action == "ai.decision",
                                AuditLog.entity_id.in_(
                                    [by_ref["pay_Pl4tFrmZ"], by_ref["INV-2026-0721"]]
                                ),
                            )
                        )
                    ).scalars().all()
                    assert any(
                        (r.details or {}).get("similarity_autoresolve_min") is not None
                        for r in ai_rows
                    ), "ai.decision audit must record the similarity floor"

            finally:
                async with SessionLocal() as db:
                    t = await db.get(PolicyConfig, "matching.semantic.similarity_threshold")
                    g = await db.get(PolicyConfig, "matching.ai.similarity_autoresolve_min")
                    t.value = orig_t
                    g.value = orig_g
                    await db.commit()
            return summary

        monkeypatch.setattr(engine, "get_agent", lambda settings: AlwaysMatchAgent())
        summary = asyncio.run(scenario())
        assert summary["ai_auto_resolved"] == 1, f"only the genuine pair resolves: {summary}"
        assert summary["ai_proposed"] >= 1, "the weak pair must land as a proposal, not resolve"