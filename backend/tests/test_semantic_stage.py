import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Transaction
from app.services.reconciliation.engine import evaluate_ai_route
from app.services.reasoning import get_agent
from app.services.semantic.embeddings import HashingEmbedder
from app.services.semantic.index import FaissVectorIndex


def _txn(source: str, ref: str, amount: str, day: int, narration: str) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        source=source,
        external_ref=ref,
        amount=Decimal(amount),
        direction="credit",
        currency="INR",
        txn_date=datetime(2026, 8, day, tzinfo=timezone.utc),
        narration=narration,
        raw={},
    )


POLICY = {
    "matching.semantic.similarity_threshold": 0.30,
    "matching.semantic.top_k": 3,
    "gate.ai_min_confidence_autoresolve": 0.90,
    "materiality.max_abs_discrepancy_inr": "500.00",
    "materiality.max_discrepancy_pct": "0.02",
    "matching.fuzzy.date_window_days": 5,
}


class TestHashingEmbedder:
    def test_deterministic_and_normalized(self):
        embedder = HashingEmbedder(dim=128)
        vectors = asyncio.run(
            embedder.embed(["razorpay settlement arvind kumar", "razorpay settlement arvind kumar"])
        )

        assert vectors[0] == vectors[1]
        norm = sum(v * v for v in vectors[0]) ** 0.5
        assert abs(norm - 1.0) < 1e-6

    def test_shared_vocabulary_scores_above_disjoint(self):
        embedder = HashingEmbedder(dim=128)
        vectors = asyncio.run(
            embedder.embed(
                [
                    "upi payment received from arvind kumar enterprises august invoice settlement",
                    "tax invoice arvind kumar ent august supplies",
                    "monthly saas subscription charge platform fees wallet credits batch",
                ]
            )
        )

        def dot(a, b):
            return sum(x * y for x, y in zip(vectors[a], vectors[b]))

        assert dot(0, 1) > dot(0, 2)


class TestFaissVectorIndex:
    def test_add_search_persist_and_compact(self, tmp_path):
        async def scenario():
            index = FaissVectorIndex(str(tmp_path / "idx.faiss"), HashingEmbedder(dim=128))
            rz = _txn("razorpay", "pay_A", "100.00", 10, "arvind kumar august invoice settlement upi payment")
            erp = _txn("erp", "INV-A", "105.00", 10, "tax invoice arvind kumar august supplies")
            other = _txn("erp", "INV-B", "900.00", 12, "monthly saas subscription platform fees")

            added = await index.add([rz, erp, other])
            assert added == 3
            assert index.size == 3

            query_vector = await index.embed_query(rz)
            hits = await index.search_vector(query_vector, 3)
            assert hits[0][0] == rz.id
            assert hits[0][1] > 0.99

            reloaded = FaissVectorIndex(str(tmp_path / "idx.faiss"), HashingEmbedder(dim=128))
            assert reloaded.size == 3
            assert reloaded.known_ids() == {rz.id, erp.id, other.id}

            removed = reloaded.compact({rz.id})
            assert removed == 2
            assert reloaded.size == 1
            assert reloaded.known_ids() == {rz.id}

        asyncio.run(scenario())


class TestHeuristicAgent:
    def test_semantic_match_pair_resolves_confidently(self):
        agent = get_agent(type("S", (), {"reasoning_provider": "heuristic"}))
        rz = _txn("razorpay", "pay_Sm1Th1cA", "4321.09", 20, "UPI payment received from Arvind Kumar Enterprises for August invoice settlement")
        erp = _txn("erp", "INV-2026-0720", "4500.00", 20, "Tax invoice ARVIND KUMAR ENT AUGUST supplies")
        weak = _txn("erp", "INV-2026-0721", "2495.00", 21, "Monthly SaaS subscription charge platform fees")

        decision = asyncio.run(agent.decide(rz, [(erp, Decimal("0.45")), (weak, Decimal("0.11"))], POLICY))

        assert decision.decision == "match"
        assert decision.confidence >= 0.90

    def test_ambiguous_pair_escalates_to_human(self):
        agent = get_agent(type("S", (), {"reasoning_provider": "heuristic"}))
        rz = _txn("razorpay", "pay_Pl4tFrmZ", "2494.10", 21, "Wallet top-up via platform credits adjustment batch")
        erp = _txn("erp", "INV-2026-0721", "2495.00", 21, "Monthly SaaS subscription charge platform fees")

        decision = asyncio.run(agent.decide(rz, [(erp, Decimal("0.13"))], POLICY))

        assert decision.decision == "needs_human"
        assert decision.confidence < 0.90


class TestAiRouting:
    ROUTE_POLICY = {
        "gate.ai_min_confidence_autoresolve": 0.90,
        "matching.ai.similarity_autoresolve_min": 0.60,
        "materiality.max_abs_discrepancy_inr": "500.00",
        "materiality.max_discrepancy_pct": "0.02",
    }

    def test_high_confidence_clean_pair_auto_resolves(self):
        route = evaluate_ai_route(Decimal("0.93"), "match", Decimal("0"), Decimal("4500"), self.ROUTE_POLICY)
        assert route == "auto_resolve"

    def test_similarity_floor_blocks_overconfident_weak_pair(self):
        # Regression pin: pay_Pl4tFrmZ/INV-2026-0721 at bge-m3 similarity 0.571.
        # A small model claiming 0.99 confidence must still be forced to human
        # review — weak semantic agreement alone can never auto-resolve.
        route = evaluate_ai_route(
            Decimal("0.99"), "match", Decimal("0.90"), Decimal("2495.00"),
            self.ROUTE_POLICY, similarity=Decimal("0.571"),
        )
        assert route == "needs_human"

    def test_similarity_floor_allows_genuine_high_similarity_match(self):
        # Control: pay_Sm1Th1cA/INV-2026-0720 (bge-m3 ~0.66) passes the joint gate.
        route = evaluate_ai_route(
            Decimal("0.99"), "match", Decimal("78.91"), Decimal("4500.00"),
            self.ROUTE_POLICY, similarity=Decimal("0.6644"),
        )
        assert route == "auto_resolve"

    def test_similarity_floor_skipped_when_similarity_absent(self):
        # Batch path is many-to-one (no single best similarity): gate must not apply.
        route = evaluate_ai_route(Decimal("0.95"), "match", Decimal("10"), Decimal("5000"), self.ROUTE_POLICY)
        assert route == "auto_resolve"

    def test_similarity_floor_still_defers_to_confidence_and_materiality(self):
        low_conf = evaluate_ai_route(
            Decimal("0.50"), "match", Decimal("0"), Decimal("4500"),
            self.ROUTE_POLICY, similarity=Decimal("0.90"),
        )
        assert low_conf == "hold_low_confidence"
        breach = evaluate_ai_route(
            Decimal("0.99"), "match", Decimal("600"), Decimal("4500"),
            self.ROUTE_POLICY, similarity=Decimal("0.90"),
        )
        assert breach == "hold_materiality"

    def test_materiality_breach_holds_even_at_max_confidence(self):
        route = evaluate_ai_route(Decimal("0.99"), "match", Decimal("600"), Decimal("4500"), self.ROUTE_POLICY)
        assert route == "hold_materiality"

    def test_percentage_breach_holds_small_absolute_amounts(self):
        route = evaluate_ai_route(Decimal("0.99"), "match", Decimal("30"), Decimal("1000"), self.ROUTE_POLICY)
        assert route == "hold_materiality"

    def test_low_confidence_holds(self):
        route = evaluate_ai_route(Decimal("0.70"), "match", Decimal("0"), Decimal("4500"), self.ROUTE_POLICY)
        assert route == "hold_low_confidence"

    def test_needs_human_and_no_match_pass_through(self):
        assert evaluate_ai_route(Decimal("0.55"), "needs_human", Decimal("0"), Decimal("100"), self.ROUTE_POLICY) == "needs_human"
        assert evaluate_ai_route(Decimal("0.85"), "no_match", Decimal("0"), Decimal("100"), self.ROUTE_POLICY) == "no_match"
