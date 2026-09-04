from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid

import faiss
import numpy as np

from app.models import Transaction
from app.services.semantic.embeddings import EmbeddingProvider, embed_text

logger = logging.getLogger(__name__)


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _external_id(txn_id: uuid.UUID, taken: set[int]) -> int:
    candidate = int.from_bytes(hashlib.sha256(txn_id.bytes).digest()[:8], "little")
    candidate %= 2**62
    while candidate in taken:
        candidate = (candidate + 1) % (2**62)
    return candidate


class FaissVectorIndex:
    def __init__(self, path: str, provider: EmbeddingProvider):
        self.path = path
        self.meta_path = f"{path}.meta.json"
        self.provider = provider
        self._index: faiss.Index | None = None
        self._meta: dict[int, uuid.UUID] = {}
        self._load()

    @property
    def size(self) -> int:
        return 0 if self._index is None else int(self._index.ntotal)

    def known_ids(self) -> set[uuid.UUID]:
        return set(self._meta.values())

    def external_for(self, txn_id: uuid.UUID) -> int | None:
        for external, internal in self._meta.items():
            if internal == txn_id:
                return external
        return None

    async def add(self, txns: list[Transaction]) -> int:
        missing = [t for t in txns if t.id not in self._meta.values()]
        if not missing:
            if not txns:
                return 0
            # All txns are already indexed in memory, but the persisted index
            # may have been built by an embedder with a DIFFERENT dimension
            # (e.g. the test-suite hashing double, 512-d, vs live bge-m3,
            # 1024-d). detect it with a one-vector probe instead of trusting
            # the sidecar blindly.
            probe = (await self.provider.embed([embed_text(txns[0])]))[0]
            if self._index is not None and len(probe) == self._index.d:
                return 0
            logger.warning(
                "semantic index dim %s != provider probe dim %s; clearing for rebuild",
                getattr(self._index, "d", None),
                len(probe),
            )
            self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(len(probe)))
            self._meta = {}
            missing = list(txns)
        raw = await self.provider.embed([embed_text(t) for t in missing])
        matrix = _l2_normalize(np.array(raw, dtype="float32"))
        if self._index is None or self._index.d != matrix.shape[1]:
            if self._index is not None:
                logger.warning(
                    "semantic index dim %d != provider actual %d; rebuilding",
                    self._index.d,
                    matrix.shape[1],
                )
            self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(matrix.shape[1]))
            self._meta = {}
            missing = list(txns)
            raw = await self.provider.embed([embed_text(t) for t in missing])
            matrix = _l2_normalize(np.array(raw, dtype="float32"))
        ids: list[int] = []
        taken = set(self._meta.keys())
        for txn in missing:
            external = _external_id(txn.id, taken)
            taken.add(external)
            ids.append(external)
            self._meta[external] = txn.id
        self._index.add_with_ids(matrix, np.array(ids, dtype="int64"))
        self._save()
        return len(missing)

    async def search_vector(self, vector: list[float], k: int) -> list[tuple[uuid.UUID, float]]:
        if self._index is None or self._index.ntotal == 0:
            return []
        if self._index.d != len(vector):
            logger.warning(
                "search_vector dim mismatch (index=%d query=%d); refusing query until rebuild",
                self._index.d,
                len(vector),
            )
            return []
        query = _l2_normalize(np.array([vector], dtype="float32"))
        limit = min(k, int(self._index.ntotal))
        scores, indices = self._index.search(query, limit)
        results: list[tuple[uuid.UUID, float]] = []
        for external, score in zip(indices[0], scores[0]):
            external_int = int(external)
            if external_int == -1:
                continue
            member = self._meta.get(external_int)
            if member is not None:
                results.append((member, float(score)))
        return results

    async def embed_query(self, txn: Transaction) -> list[float]:
        return (await self.provider.embed([embed_text(txn)]))[0]

    def compact(self, valid_ids: set[uuid.UUID]) -> int:
        if self._index is None:
            return 0
        keep = {
            external: internal
            for external, internal in self._meta.items()
            if internal in valid_ids
        }
        removed = len(self._meta) - len(keep)
        if removed == 0:
            return 0
        rebuilt = faiss.IndexIDMap2(faiss.IndexFlatIP(self._index.d))
        if keep:
            externals = np.array(sorted(keep.keys()), dtype="int64")
            vectors = np.vstack([self._index.reconstruct(int(e)) for e in externals])
            rebuilt.add_with_ids(vectors, externals)
        self._index = rebuilt
        self._meta = keep
        self._save()
        logger.info("compacted semantic index: removed=%d kept=%d", removed, len(keep))
        return removed

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        temp_index = f"{self.path}.tmp"
        faiss.write_index(self._index, temp_index)
        os.replace(temp_index, self.path)
        temp_meta = f"{self.meta_path}.tmp"
        with open(temp_meta, "w", encoding="utf-8") as handle:
            json.dump(
                {str(external): internal.hex for external, internal in self._meta.items()},
                handle,
            )
        os.replace(temp_meta, self.meta_path)

    def _load(self) -> None:
        if not os.path.exists(self.path) or not os.path.exists(self.meta_path):
            self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(self.provider.dim))
            return
        try:
            index = faiss.read_index(self.path)
            with open(self.meta_path, encoding="utf-8") as handle:
                raw_meta = json.load(handle)
            meta = {int(external): uuid.UUID(hex_value) for external, hex_value in raw_meta.items()}
            if index.ntotal != len(meta):
                raise ValueError("index/meta size mismatch")
            self._index = index
            self._meta = meta
        except Exception as exc:
            logger.warning("discarding unreadable semantic index (%s)", exc)
            self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(self.provider.dim))
            self._meta = {}
