"""Evidence harness for the pay_Pl4tFrmZ / INV-2026-0721 overconfidence question.

1. Raw transaction data (amount/date/narration/raw payload) for both sides.
2. Raw ai.decision audit rows from audit_logs for both entities (full JSON).
3. Fresh model runs, no app caching:
   - 3 runs at temperature 0 (exactly what the engine uses -> deterministic)
   - 5 runs at temperature 0.7 (sampled -> exposes calibration noise determinism hides)
"""

import asyncio
import json

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import AuditLog, PolicyConfig, Transaction

REFS = ["pay_Pl4tFrmZ", "INV-2026-0721"]


def _dump_txn(t: Transaction) -> str:
    from app.services.parsers.base import business_date

    return (
        f"external_ref={t.external_ref!r}\n"
        f"  source={t.source!r} direction={t.direction!r} status={t.status!r}\n"
        f"  amount=INR {t.amount}\n"
        f"  txn_date={t.txn_date} business_date={business_date(t.txn_date).isoformat()}\n"
        f"  narration={t.narration!r}\n"
        f"  counterparty={t.counterparty!r}\n"
        f"  raw={json.dumps(t.raw or {}, ensure_ascii=False)}"
    )


async def _transactions(db):
    rows = (
        await db.execute(select(Transaction).where(Transaction.external_ref.in_(REFS)))
    ).scalars().all()
    by_ref = {t.external_ref: t for t in rows}
    missing = set(REFS) - set(by_ref)
    if missing:
        raise SystemExit(f"MISSING TXNS: {sorted(missing)}")
    return by_ref


async def _policy(db) -> dict:
    keys = [
        "matching.semantic.similarity_threshold",
        "gate.ai_min_confidence_autoresolve",
        "materiality.max_abs_discrepancy_inr",
        "materiality.max_discrepancy_pct",
        "matching.deterministic.force_human_amount_inr",
    ]
    out = {}
    for k in keys:
        row = await db.get(PolicyConfig, k)
        out[k] = None if row is None else row.value
    return out


async def _audit_rows(db, ids):
    rows = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "ai.decision",
                AuditLog.entity_id.in_(ids),
            )
            .order_by(AuditLog.created_at)
        )
    ).scalars().all()
    return rows


async def main():
    s = get_settings()
    print(f"REASONING_PROVIDER={s.reasoning_provider} model={s.reasoning_model}")
    async with SessionLocal() as db:
        txns = await _transactions(db)
        print("\n================ RAW TRANSACTION DATA ================")
        for ref in REFS:
            print(f"\n-- {ref} --\n{_dump_txn(txns[ref])}")

        p1, p2 = txns[REFS[0]], txns[REFS[1]]
        delta = abs(p1.amount - p2.amount)
        print("\n-- independent arithmetic --")
        print(f"  |amount diff| = INR {delta}")
        print(f"  delta as %% of INV-2026-0721 = {float(delta / p2.amount) * 100:.4f}%")

        print("\n================ RAW ai.decision AUDIT ROWS ================")
        policy = await _policy(db)
        print("policy:", json.dumps({k: str(v) for k, v in policy.items()}, indent=2))
        audit = await _audit_rows(db, [t.id for t in txns.values()])
        real = [r for r in audit if (r.details or {}).get("model", "").startswith("ollama:")]
        print(f"\n{len(audit)} ai.decision rows total; {len(real)} from the REAL model (ollama:).")
        for r in real:
            print(f"\n[audit id={r.id} created_at={r.created_at}]")
            print(f"entity_id={r.entity_id} ({next((k for k, v in txns.items() if v.id == r.entity_id), '?')})")
            print(f"actor={r.actor}")
            print("details=" + json.dumps(r.details, ensure_ascii=False, indent=2))
        if not real:
            print("!! no REAL-model audit rows found (purged by later test runs)")

        print("\n================ FRESH MODEL RUNS (no caching) ================")
        from app.services.reasoning.base import build_prompt
        from app.services.reasoning.ollama_agent import OllamaReasoningAgent

        agent = OllamaReasoningAgent(s.reasoning_api_url, s.reasoning_model, s.reasoning_timeout_s)

        # Work on INV-2026-0721 as the decided entity (mirrors the probe row),
        # and ALSO pay_Pl4tFrmZ as entity (mirrors the golden needs_human case).
        for entity_name in ("INV-2026-0721", "pay_Pl4tFrmZ"):
            entity = txns[entity_name]
            other_ref = next(r for r in REFS if r != entity_name)
            other = txns[other_ref]
            cands = [(other, 0.571)]
            system, user = build_prompt(entity, cands, policy)
            print(f"\n----- entity={entity_name} candidates=[({other_ref}, 0.571)] -----")
            print("PROMPT (reconstructed verbatim):")
            print(system)
            print("---")
            print(user)

            print("\n  -- 3 runs @ temperature 0 (engine behavior, deterministic) --")
            for i in range(1, 4):
                dec = await agent.decide(entity, cands, policy)
                print(
                    f"  run{i}: decision={dec.decision!r} confidence={dec.confidence} "
                    f"rationale={dec.rationale!r}"
                )

            print("  -- 5 runs @ temperature 0.7 (sampled, exposes calibration) --")
            for i in range(1, 6):
                system, user = build_prompt(entity, cands, policy)
                async with httpx.AsyncClient(timeout=s.reasoning_timeout_s) as client:
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
                    content = r.json()["message"]["content"]
                    parsed = json.loads(content)
                print(
                    f"  run{i}: decision={parsed.get('decision')!r} "
                    f"confidence={parsed.get('confidence')} "
                    f"rationale={parsed.get('rationale')!r}"
                )

    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())