from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx

from app.models import Transaction

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def embed_text(txn: Transaction) -> str:
    parts = [
        txn.source,
        txn.narration or "",
        txn.counterparty or "",
        txn.status or "",
    ]
    raw = txn.raw or {}
    for key in ("order_id", "customer_ref", "settlement_id"):
        value = raw.get(key)
        if value:
            parts.append(str(value))
    return " | ".join(p for p in parts if p)


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    name = "hashing"

    def __init__(self, dim: int = 512):
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in _TOKEN_RE.findall(text.lower()):
                digest = hashlib.md5(token.encode()).digest()
                index = int.from_bytes(digest[:4], "little") % self.dim
                vec[index] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class OllamaEmbedder:
    name = "ollama"

    def __init__(self, api_url: str, model: str, timeout_s: float = 120.0):
        self.api_url = api_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.dim = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            for text in texts:
                response = await client.post(
                    f"{self.api_url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                response.raise_for_status()
                embedding = response.json()["embedding"]
                if not self.dim:
                    self.dim = len(embedding)
                vectors.append(embedding)
        return vectors


def get_embedder(settings) -> EmbeddingProvider:
    if settings.embedding_provider == "ollama":
        return OllamaEmbedder(settings.embedding_api_url, settings.embedding_model)
    return HashingEmbedder()
