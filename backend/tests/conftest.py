import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")

# The test suite is hermetic: it must exercise the deterministic doubles
# (`hashing` embeddings, `heuristic` reasoning agent), never the real LLM.
# get_settings() is lru_cached and first read occurs after this module loads,
# so plain (non-setdefault) assignment reliably overrides values exported from
# the live .env (e.g. EMBEDDING_PROVIDER=ollama / REASONING_PROVIDER=ollama).
# To run the suite against the real model instead, override these two vars
# here (and watch for divergent outcomes on pay_Pl4tFrmZ in the golden test).
os.environ["EMBEDDING_PROVIDER"] = "hashing"
os.environ["REASONING_PROVIDER"] = "heuristic"
os.environ["EMBEDDING_API_URL"] = ""
os.environ["EMBEDDING_MODEL"] = ""
os.environ["REASONING_API_URL"] = "http://localhost:9"
os.environ["REASONING_MODEL"] = ""
os.environ["REASONING_TIMEOUT_S"] = "5"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

SAMPLE_DIR = BACKEND_ROOT / "sample_data"

# Canonical policy values (docker/postgres/init.sql). Individual tests override
# these inside their function body (e.g. a 0.15 similarity-autoresolve gate for
# the hashing double); the autouse fixture below resets them to canonical AFTER
# every test, even when a test errors mid-body. This keeps a crashed/aborted
# suite from leaking overridden policy into the live DB or subsequent runs.
_CANONICAL_POLICY = {
    "matching.semantic.similarity_threshold": 0.82,
    "gate.ai_min_confidence_autoresolve": 0.90,
    "matching.ai.similarity_autoresolve_min": 0.60,
}


@pytest.fixture(autouse=True)
def _reset_policy_to_canonical():
    yield
    import asyncio

    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import PolicyConfig

    async def reset():
        async with SessionLocal() as db:
            rows = (await db.execute(select(PolicyConfig))).scalars().all()
            by_key = {r.key: r for r in rows}
            for key, value in _CANONICAL_POLICY.items():
                row = by_key.get(key)
                if row is None:
                    continue
                row.value = value
            await db.commit()

    asyncio.run(reset())


def load_sample(name: str) -> bytes:
    return (SAMPLE_DIR / name).read_bytes()
