from __future__ import annotations

import uuid
from decimal import Decimal

from app.models import Transaction
from app.services.semantic.embeddings import get_embedder
from app.services.semantic.index import FaissVectorIndex


class SemanticMatcher:
    def __init__(self, settings):
        self.settings = settings
        self.provider = get_embedder(settings)
        self.index = FaissVectorIndex(settings.semantic_index_path, self.provider)

    async def ensure_indexed(self, txns: list[Transaction]) -> int:
        return await self.index.add(txns)

    async def top_candidates(
        self,
        txn: Transaction,
        pool: list[Transaction],
        policy: dict,
    ) -> list[tuple[Transaction, Decimal]]:
        threshold = Decimal(str(float(policy["matching.semantic.similarity_threshold"])))
        top_k = int(policy["matching.semantic.top_k"])
        cross_source = [
            candidate
            for candidate in pool
            if candidate.source != txn.source and candidate.id != txn.id
        ]
        if not cross_source:
            return []

        query_vector = await self.index.embed_query(txn)
        hits = await self.index.search_vector(query_vector, max(top_k * 4, len(pool)))

        by_id = {candidate.id: candidate for candidate in cross_source}
        above_threshold: list[tuple[Transaction, Decimal]] = []
        for member_id, score in hits:
            candidate = by_id.get(member_id)
            if candidate is None:
                continue
            similarity = Decimal(str(round(score, 4)))
            if similarity < threshold:
                break
            above_threshold.append((candidate, similarity))

        above_threshold.sort(key=lambda item: (-item[1], item[0].external_ref))
        return above_threshold[:top_k]

    def compact_if_stale(self, valid_ids: set[uuid.UUID]) -> int:
        stale = len(self.index.known_ids() - valid_ids)
        if stale and self.index.size and stale >= self.index.size // 2:
            return self.index.compact(valid_ids)
        return 0
