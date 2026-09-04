"""qwen3:4b comparison probe — raw evidence.

Modes:
  warm     force-load qwen3:4b in ollama and report latency.
  decide   fresh decide() runs (no app caching) for the weak + control pairs:
           3x temperature 0 (engine behavior) and 5x temperature 0.7 (sampled
           calibration), per-call latency.
  pipeline live run_reconciliation under the joint-evidence gate (real bge-m3,
           qwen3:4b, similarity_autoresolve_min=0.60, similarity_threshold
           lowered to 0.10 to reproduce the ORIGINAL audit-2647 context where
           a confident match on the 0.571 pair reached auto-resolution).

Policy is temporarily lowered for `pipeline` and restored to canonical after.
"""

import asyncio
import json
import sys
import time

import httpx
from decimal import Decimal
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import AuditLog, ExceptionRecord, Match, MatchParticipant, PolicyConfig, Transaction
from app.services.reasoning.ollama_agent import OllamaReasoningAgent
from app.services.reconciliation.engine import run_reconciliation

REFS = ["pay_Pl4tFrmZ", "INV-2026-0721", "pay_Sm1Th1cA", "INV-2026-0720"]
CANONICAL = {
    "matching.semantic.similarity_threshold": 0.82,
    "gate.ai_min_confidence_autoresolve": 0.90,
    "matching.ai.similarity_autoresolve_min": 0.60,
}


async def setup():
    s = get_settings()
    print(f"model={s.reasoning_model} timeout={s.reasoning_timeout_s}")
    async with SessionLocal() as db:
        rows = (await db.execute(select(Transaction).where(Transaction.external_ref.in_(REFS)))).scalars().all()
        by_ref = {t.external_ref: t for t in rows}
        if set(REFS) - set(by_ref):
            raise SystemExit(f"MISSING: {sorted(set(REFS) - set(by_ref))}")
        policy = {}
        for r in (await db.execute(select(PolicyConfig))).scalars().all():
            policy[r.key] = r.value
        return s, by_ref, policy


async def set_policy(updates):
    async with SessionLocal() as db:
        for key, value in updates.items():
            r = await db.get(PolicyConfig, key)
            if r is not None:
                r.value = value
        await db.commit()


async def warm(s):
    async with httpx.AsyncClient(timeout=s.reasoning_timeout_s) as client:
        t0 = time.perf_counter()
        r = await client.post(
            f"{s.reasoning_api_url}/api/generate",
            json={"model": s.reasoning_model, "prompt": "say ok", "stream": False},
        )
        r.raise_for_status()
        print(f"warm: {time.perf_counter() - t0:.2f}s ({r.json().get('eval_count')} tokens)")


async def decide_mode():
    s, by_ref, policy = await setup()
    agent = OllamaReasoningAgent(s.reasoning_api_url, s.reasoning_model, s.reasoning_timeout_s)
    from app.services.reasoning.base import build_prompt

    pairs = [
        ("weak  entity=INV-2026-0721 cand=pay_Pl4tFrmZ sim=0.571", by_ref["INV-2026-0721"], [(by_ref["pay_Pl4tFrmZ"], Decimal("0.571"))]),
        ("weak  entity=pay_Pl4tFrmZ cand=INV-2026-0721 sim=0.571", by_ref["pay_Pl4tFrmZ"], [(by_ref["INV-2026-0721"], Decimal("0.571"))]),
        ("ctrl  entity=INV-2026-0720 cand=pay_Sm1Th1cA sim=0.6644", by_ref["INV-2026-0720"], [(by_ref["pay_Sm1Th1cA"], Decimal("0.6644"))]),
        ("ctrl  entity=pay_Sm1Th1cA cand=INV-2026-0720 sim=0.6644", by_ref["pay_Sm1Th1cA"], [(by_ref["INV-2026-0720"], Decimal("0.6644"))]),
    ]
    for label, entity, cands in pairs:
        print(f"\n----- {label}")
        print("  -- 3x temperature 0 --")
        for i in range(1, 4):
            t0 = time.perf_counter()
            dec = await agent.decide(entity, cands, policy)
            print(f"  run{i}: {dec.decision!r} conf={dec.confidence} {dec.rationale!r}  [{time.perf_counter()-t0:.2f}s]")
        print("  -- 5x temperature 0.7 (sampled) --")
        system, user = build_prompt(entity, cands, policy)
        async with httpx.AsyncClient(timeout=s.reasoning_timeout_s) as client:
            for i in range(1, 6):
                t0 = time.perf_counter()
                r = await client.post(
                    f"{agent.api_url}/api/chat",
                    json={
                        "model": agent.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "stream": False,
                        "format": "json",
                        "options": {"temperature": 0.7},
                    },
                )
                r.raise_for_status()
                parsed = json.loads(r.json()["message"]["content"])
                print(
                    f"  run{i}: {parsed.get('decision')!r} conf={parsed.get('confidence')} "
                    f"{parsed.get('rationale')!r}  [{time.perf_counter()-t0:.2f}s]"
                )


async def pipeline_mode():
    s, by_ref, _ = await setup()
    async with SessionLocal() as db:
        txn_ids = [t.id for t in by_ref.values()]
        match_ids = set((await db.execute(
            select(MatchParticipant.match_id).where(MatchParticipant.transaction_id.in_(txn_ids))
        )).scalars())
        if match_ids:
            await db.execute(Match.__table__.delete().where(Match.id.in_(match_ids)))
        await db.execute(ExceptionRecord.__table__.delete().where(ExceptionRecord.transaction_id.in_(txn_ids)))
        await db.execute(AuditLog.__table__.delete().where(AuditLog.entity_id.in_(txn_ids)))
        await db.commit()
    print("scope purged")

    await set_policy({
        "matching.semantic.similarity_threshold": 0.10,
        "gate.ai_min_confidence_autoresolve": 0.90,
        "matching.ai.similarity_autoresolve_min": 0.60,
    })
    print("policy lowered to threshold=0.10 conf=0.90 gate=0.60 (audit-2647 reproduction context)")

    async with SessionLocal() as db:
        t0 = time.perf_counter()
        await run_reconciliation(db, actor="probe-4b", transaction_ids=txn_ids)
        await db.commit()
        print(f"reconciliation took {time.perf_counter()-t0:.1f}s")

        matches = (await db.execute(
            select(Match)
            .join(MatchParticipant, MatchParticipant.match_id == Match.id)
            .where(MatchParticipant.transaction_id.in_(txn_ids))
            .distinct()
        )).scalars().all()
        print("\n-- matches --")
        for m in matches:
            print(f"  {m.match_type}/{m.status} conf={m.confidence_score} by={m.decided_by}")
            print(f"    {m.rationale}")

        exceptions = (await db.execute(
            select(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(txn_ids))
        )).scalars().all()
        print("\n-- exceptions --")
        for e in exceptions:
            ref = next((r for r, t in by_ref.items() if t.id == e.transaction_id), "?")
            print(f"  {ref}: {e.exception_type} priority={e.priority}")
            op = (await db.execute(
                select(AuditLog).where(AuditLog.action == "exception.opened", AuditLog.entity_id == e.id)
            )).scalars().all()
            if op:
                print(f"    open-details: {json.dumps(op[-1].details, ensure_ascii=False)}")

        audits = (await db.execute(
            select(AuditLog).where(AuditLog.action == "ai.decision",
                                   AuditLog.entity_id.in_(txn_ids)).order_by(AuditLog.created_at)
        )).scalars().all()
        print(f"\n-- {len(audits)} ai.decision audit rows (raw) --")
        for row in audits:
            ref = next((r for r, t in by_ref.items() if t.id == row.entity_id), "?")
            print(f"\n[{row.actor} entity={ref}]")
            print(json.dumps(row.details, ensure_ascii=False, indent=2))

    await set_policy(CANONICAL)
    print("\npipeline policy restored to canonical")


async def _main(mode):
    if mode == "warm":
        s, _, _ = await setup()
        await warm(s)
    elif mode == "decide":
        await decide_mode()
    elif mode == "pipeline":
        await pipeline_mode()
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "decide"
    asyncio.run(_main(mode))