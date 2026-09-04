"""Scoped probe: run the REAL Ollama reasoning+embedding path end-to-end.

Identity: prints the model labels and the raw LLM rationale recorded for every
AI decision, so we can prove the real Qwen3 path executed (vs heuristic-offline).
"""

import asyncio
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import AuditLog, ExceptionRecord, Match, Transaction, MatchParticipant
from app.services.reconciliation.engine import run_reconciliation

SCOPE_REFS = [
    "pay_Hh8i9J0k", "INV-2026-0711", "UTR888170260811X",
    "pay_Sm1Th1cA", "INV-2026-0720",
    "pay_Pl4tFrmZ", "INV-2026-0721",
    "pay_Jj0k1L2m", "UTR888170260813",
    "pay_Kk2m3N4o", "INV-2026-0722",
    "pay_Ll3n4O5p", "INV-2026-0723",
]


async def main():
    s = get_settings()
    print(f"EMBEDDING_PROVIDER={s.embedding_provider} model={s.embedding_model} url={s.embedding_api_url}")
    print(f"REASONING_PROVIDER={s.reasoning_provider} model={s.reasoning_model} url={s.reasoning_api_url}")

    async with SessionLocal() as db:
        rows = (await db.execute(select(Transaction).where(Transaction.external_ref.in_(SCOPE_REFS)))).scalars()
        txns = {t.external_ref: t for t in rows}
        missing = set(SCOPE_REFS) - set(txns)
        print(f"found={len(txns)} missing={sorted(missing)}")
        ids = list(txns.values())

        # Purge prior matches/exceptions/audit ONLY for the scoped txns.
        match_ids = set(
            (await db.execute(
                select(MatchParticipant.match_id).where(MatchParticipant.transaction_id.in_([t.id for t in ids]))
            )).scalars()
        )
        if match_ids:
            await db.execute(Match.__table__.delete().where(Match.id.in_(match_ids)))
        await db.execute(ExceptionRecord.__table__.delete().where(ExceptionRecord.transaction_id.in_([t.id for t in ids])))
        await db.execute(AuditLog.__table__.delete().where(AuditLog.entity_id.in_([t.id for t in ids])))
        await db.commit()
        print(f"purged {len(match_ids)} matches, exceptions, audit for scope")

        summary = await run_reconciliation(
            db,
            actor="real-ollama-probe",
            transaction_ids=[t.id for t in ids],
        )

        # Everything the run recorded as AI decisions.
        ai_rows = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "ai.decision",
                    AuditLog.entity_type == "transaction",
                    AuditLog.entity_id.in_([t.id for t in ids]),
                ).order_by(AuditLog.created_at)
            )
        ).scalars()
        print("\n=== ai.decision audit rows ===")
        for row in ai_rows:
            t = txns_by_ref(row.entity_id, txns)
            d = row.details or {}
            print(f"- {t} model={d.get('model')}")
            print(f"    decision={d.get('decision')} confidence={d.get('confidence')}")
            print(f"    rationale={d.get('rationale')}")
            print(f"    best_candidate={d.get('best_candidate')}")
            print(f"    all_candidates={d.get('all_candidates')}")

        # All matches produced, with label in rationale.
        new_matches = (
            await db.execute(
                select(Match).where(Match.id.in_(match_ids))
            )
        ).scalars().all() if match_ids else []
        # matches created this run are not in match_ids; fetch by joining participants inside scope
        created = (
            await db.execute(
                select(Match)
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .where(MatchParticipant.transaction_id.in_([t.id for t in ids]))
                .distinct()
            )
        ).scalars()
        print("\n=== matches for scope ===")
        for m in created:
            print(f"- {m.match_type} {m.status} conf={m.confidence_score} decided_by={m.decided_by} resolved_by={m.resolved_by}")
            if m.rationale:
                print(f"    rationale={m.rationale}")

        print("\n=== summary counters ===")
        for k, v in sorted(summary.items()):
            print(f"  {k}={v}")


def txns_by_ref(entity_id, txns_by_refdict):
    for ref, t in txns_by_refdict.items():
        if t.id == entity_id:
            return ref
    return str(entity_id)


if __name__ == "__main__":
    asyncio.run(main())