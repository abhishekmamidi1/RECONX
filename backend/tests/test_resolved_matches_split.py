"""Resolved-matches split regression tests.

UI contract under test (GET /api/v1/matches/resolved) — split the two kinds of
decisions so reviewers can separate "the pipeline settled this" from "a human
decided this":

  auto  = matches.status == 'confirmed' AND matches.resolved_by == 'auto'
          (stage = the matcher that produced them: deterministic/fuzzy/semantic/ai)
  human = matches.resolved_by == 'human' (status confirmed OR rejected — a
          rejection is a decision too), with actor + action attributed from
          audit_logs ('match.approved' / 'match.manual_created' / 'match.rejected')

The two lists must never overlap and must categorize the golden suite exactly:
3 deterministic + 1 fuzzy + 1 AI auto-resolves, then the reviewer decisions on
the two open goldens land in the human view with correct attribution.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    ExceptionRecord,
    Match,
    MatchParticipant,
    PolicyConfig,
    Transaction,
)
from app.services.reconciliation import run_reconciliation

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

AUTO_REF_PAIRS = [
    list(EXACT_TRIOS[0]),
    list(EXACT_TRIOS[1]),
    list(EXACT_TRIOS[2]),
    list(FUZZY_PAIR),
    list(SEMANTIC_MATCH_PAIR),
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


def _prepare():
    """Purge the golden scope, run the pipeline once. Returns (ids, originals)."""

    async def scenario():
        by_ref = await _load_scope()
        originals = await _purge_scope(by_ref)
        scope_ids = [
            t.id
            for ref, t in by_ref.items()
            if ref not in INERT_REFS
        ]
        async with SessionLocal() as db:
            await run_reconciliation(
                db, actor="pytest-resolved", transaction_ids=scope_ids
            )
            await db.commit()
        return by_ref, originals

    return asyncio.run(scenario())


async def _confirmed_auto_match_ids():
    """Golden-scope matches the pipeline confirmed on its own."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Match)
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
                .where(
                    Transaction.external_ref.in_(ALL_REFS),
                    Match.status == "confirmed",
                    Match.resolved_by == "auto",
                )
            )
        ).scalars().unique().all()
        return {str(m.id): m for m in rows}


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


def test_resolved_split_categorizes_golden_matches_and_attributes_human_actions():
    by_ref, originals = _prepare()
    try:
        with _client() as client:
            auto_payload = client.get(
                "/api/v1/matches/resolved", params={"resolution": "auto", "limit": 250}
            ).json()
            human_payload = client.get(
                "/api/v1/matches/resolved", params={"resolution": "human", "limit": 250}
            ).json()

            # Every pipeline-confirmed golden match lands in the AUTO view ...
            golden_auto = asyncio.run(_confirmed_auto_match_ids())
            assert len(golden_auto) == 5, f"expected 5 auto-resolved goldens, got {list(golden_auto)}"
            auto_by_id = {item["match_id"]: item for item in auto_payload["items"]}
            assert golden_auto.keys() <= auto_by_id.keys(), (
                "auto view must contain every confirmed auto-resolved golden match"
            )
            for match_id, match in golden_auto.items():
                item = auto_by_id[match_id]
                assert item["status"] == "confirmed"
                assert item["resolved_by"] == "auto"
                assert item["stage"] == match.match_type, (
                    f"auto view stage must reflect the matcher, got {item['stage']}"
                )
                assert item["resolved_at"] is not None
                assert item["action"] is None and item["actor"] is None

            stages = {item["stage"] for item in auto_by_id.values() if item["match_id"] in golden_auto}
            assert stages == {"deterministic", "fuzzy", "ai"}, stages
            stage_breakdown = {}
            for m in golden_auto.values():
                stage_breakdown[m.match_type] = stage_breakdown.get(m.match_type, 0) + 1
            assert stage_breakdown == {"deterministic": 3, "fuzzy": 1, "ai": 1}, stage_breakdown

            # ... and NONE of them are mislabeled as human decisions.
            human_ids = {item["match_id"] for item in human_payload["items"]}
            assert not (golden_auto.keys() & human_ids), (
                "a match cannot appear in both the auto and the human view"
            )

            # Reviewer actions on the two still-open goldens.
            proposal_id = asyncio.run(_find_proposal(*SEMANTIC_HUMAN_PAIR))
            approved = client.post(
                f"/api/v1/review/matches/{proposal_id}/approve",
                headers={"X-Actor": "ops-reviewer"},
                json={"note": "approved against settlement email"},
            )
            assert approved.status_code == 200, approved.text

            manual = client.post(
                "/api/v1/review/matches/manual",
                headers={"X-Actor": "ops-cop"},
                json={
                    "transaction_ids": [
                        str(by_ref["pay_Jj0k1L2m"].id),
                        str(by_ref["UTR888170260813"].id),
                    ],
                    "note": "reviewer matched the gateway record to its bank leg",
                },
            )
            assert manual.status_code == 200, manual.text
            manual_match_id = manual.json()["match_id"]

            refreshed_human = client.get(
                "/api/v1/matches/resolved", params={"resolution": "human", "limit": 250}
            ).json()
            human_by_id = {item["match_id"]: item for item in refreshed_human["items"]}

            approved_item = human_by_id.get(proposal_id)
            assert approved_item, "human view must show the approved semantic proposal"
            assert approved_item["status"] == "confirmed"
            assert approved_item["resolved_by"] == "human"
            assert approved_item["action"] == "approved"
            assert approved_item["actor"] == "ops-reviewer"
            assert approved_item["note"] == "approved against settlement email"

            manual_item = human_by_id.get(manual_match_id)
            assert manual_item, "human view must show the manual match"
            assert manual_item["action"] == "manually matched"
            assert manual_item["actor"] == "ops-cop"
            assert manual_item["match_type"] == "manual"
            assert manual_item["note"] == "reviewer matched the gateway record to its bank leg"

            # Auto view unchanged: still only the 5; never the human decisions.
            refreshed_auto = client.get(
                "/api/v1/matches/resolved", params={"resolution": "auto", "limit": 250}
            ).json()
            refreshed_auto_ids = {item["match_id"] for item in refreshed_auto["items"]}
            assert {proposal_id, manual_match_id}.isdisjoint(refreshed_auto_ids)
            assert golden_auto.keys() <= refreshed_auto_ids

            # Filters: action + actor narrow the human view.
            action_filtered = client.get(
                "/api/v1/matches/resolved",
                params={"resolution": "human", "action": "approved", "limit": 250},
            ).json()
            action_ids = {item["match_id"] for item in action_filtered["items"]}
            assert proposal_id in action_ids and manual_match_id not in action_ids

            actor_filtered = client.get(
                "/api/v1/matches/resolved",
                params={"resolution": "human", "actor": "ops-cop", "limit": 250},
            ).json()
            actor_ids = {item["match_id"] for item in actor_filtered["items"]}
            assert manual_match_id in actor_ids and proposal_id not in actor_ids

            # Lifetime totals exposed alongside.
            assert refreshed_auto["auto"] >= 5
            assert refreshed_auto["human"] >= 2
            assert refreshed_auto["resolution"] == "auto"
            assert refreshed_human["resolution"] == "human"

            # Sortable by confidence both ways.
            sorted_desc = client.get(
                "/api/v1/matches/resolved",
                params={"resolution": "auto", "sort_by": "confidence_score", "order": "desc", "limit": 250},
            ).json()["items"]
            confs = [float(i["confidence_score"]) for i in sorted_desc]
            assert confs == sorted(confs, reverse=True), "auto view not confidence-sorted"
    finally:
        asyncio.run(_restore(originals))