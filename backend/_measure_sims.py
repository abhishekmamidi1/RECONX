"""Measure similarities for the two pairs under both embedders (hashing vs bge-m3)."""

import asyncio
import math
from decimal import Decimal

from app.db.session import SessionLocal
from app.models import Transaction
from app.services.semantic.embeddings import HashingEmbedder, OllamaEmbedder, embed_text
from app.services.semantic.index import _l2_normalize
import numpy as np

PAIRS = [
    ("pay_Pl4tFrmZ", "INV-2026-0721"),
    ("pay_Sm1Th1cA", "INV-2026-0720"),
]


async def main():
    async with SessionLocal() as db:
        refs = [r for pair in PAIRS for r in pair]
        rows = (await db.execute(
            __import__("sqlalchemy").select(Transaction).where(Transaction.external_ref.in_(refs))
        )).scalars().all()
        by_ref = {t.external_ref: t for t in rows}

        embedders = [
            ("hashing512", HashingEmbedder()),
            ("bge-m3", OllamaEmbedder("http://host.docker.internal:11434", "bge-m3")),
        ]
        for label, emb in embedders:
            print(f"\n== embedder: {label} ==")
            for r1, r2 in PAIRS:
                t1, t2 = by_ref[r1], by_ref[r2]
                vecs = await emb.embed([embed_text(t1), embed_text(t2)])
                a, b = _l2_normalize(np.array(vecs, dtype="float32"))
                sim = float(np.dot(a, b))
                print(f"  {r1} <-> {r2}: cosine={sim:.4f}")


if __name__ == "__main__":
    asyncio.run(main())