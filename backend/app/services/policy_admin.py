"""Policy engine administration.

Every editable tunable in policy_config gets:
  - presentation metadata (group, label, description, unit) so reviewers
    never need to know internal key names,
  - a validator so a broken config can never be saved,
  - an audited update path (policy.updated with before/after values).

Unknown keys are read-only: editing them via this API is rejected rather
than silently accepted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, PolicyConfig
from app.services.audit import record_audit


@dataclass(frozen=True)
class PolicyFieldMeta:
    key: str
    group: str
    label: str
    description: str
    unit: str  # "", "days", "inr", "pct", "count", "bool", "score"
    value_type: type  # bool / int / float


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    raise ValueError("expected true or false")


def _int_range(lo: float, hi: float) -> Callable[[Any], Any]:
    def check(v: Any) -> int:
        try:
            n = int(float(str(v)))
        except (TypeError, ValueError):
            raise ValueError("expected an integer")
        if not lo <= n <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return n

    return check


def _num_range(lo: float, hi: float) -> Callable[[Any], Any]:
    def check(v: Any) -> float:
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            raise ValueError("expected a number")
        if not lo <= float(d) <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        # JSONB storage goes through json.dumps — Decimals are not serialisable.
        return float(d)

    return check


POLICY_FIELDS: dict[str, PolicyFieldMeta] = {
    meta.key: meta
    for meta in [
        # ── Matching thresholds ──────────────────────────────
        PolicyFieldMeta(
            "matching.deterministic.enabled", "Matching thresholds",
            "Deterministic matcher", "Master switch for exact identity matching (payment ref / UTR).",
            "bool", bool,
        ),
        PolicyFieldMeta(
            "matching.deterministic.date_window_days", "Matching thresholds",
            "Exact-match date window", "Allowed day delta between dates for an exact identity match.",
            "days", int,
        ),
        PolicyFieldMeta(
            "matching.fuzzy.enabled", "Matching thresholds",
            "Fuzzy matcher", "Master switch for fuzzy reference/narration matching.",
            "bool", bool,
        ),
        PolicyFieldMeta(
            "matching.fuzzy.score_threshold", "Matching thresholds",
            "Fuzzy score threshold", "RapidFuzz score above which a candidate is proposed.",
            "score (0-100)", float,
        ),
        PolicyFieldMeta(
            "matching.fuzzy.amount_tolerance_pct", "Matching thresholds",
            "Fuzzy amount tolerance", "Fractional tolerance on amount for fuzzy pairs (0.001 = 0.1%).",
            "pct (0-0.1)", float,
        ),
        PolicyFieldMeta(
            "matching.fuzzy.date_window_days", "Matching thresholds",
            "Fuzzy date window", "Date window +/- days within which fuzzy candidates are considered.",
            "days", int,
        ),
        PolicyFieldMeta(
            "matching.semantic.enabled", "Matching thresholds",
            "Semantic matcher", "Master switch for embedding-similarity candidate retrieval.",
            "bool", bool,
        ),
        PolicyFieldMeta(
            "matching.semantic.similarity_threshold", "Matching thresholds",
            "Semantic similarity threshold", "Minimum cosine similarity to surface a semantic candidate.",
            "score (0-1)", float,
        ),
        PolicyFieldMeta(
            "matching.semantic.top_k", "Matching thresholds",
            "Semantic candidates per record", "Number of nearest neighbours retrieved per unmatched record.",
            "count", int,
        ),
        # ── Auto-resolve gates ───────────────────────────────
        PolicyFieldMeta(
            "gate.auto_resolve_enabled", "Auto-resolve gates",
            "Auto-resolve master switch", "Global kill switch — confidence alone is never sufficient.",
            "bool", bool,
        ),
        PolicyFieldMeta(
            "gate.ai_min_confidence_autoresolve", "Auto-resolve gates",
            "AI confidence cutoff", "Minimum AI-agent confidence for auto-resolve eligibility.",
            "score (0-1)", float,
        ),
        PolicyFieldMeta(
            "matching.ai.similarity_autoresolve_min", "Auto-resolve gates",
            "AI similarity floor", "Minimum semantic similarity for an AI 'match' to auto-resolve. Below this, even a confidently-declared match falls to human review (joint-evidence gate).",
            "score (0-1)", float,
        ),
        PolicyFieldMeta(
            "review.force_human_above_inr", "Auto-resolve gates",
            "Force human review above", "Amounts at or above this always route to human review regardless of confidence.",
            "inr", float,
        ),
        # ── Materiality rules ────────────────────────────────
        PolicyFieldMeta(
            "materiality.max_abs_discrepancy_inr", "Materiality rules",
            "Max absolute discrepancy", "Auto-resolve forbidden when the absolute gap exceeds this INR value.",
            "inr", float,
        ),
        PolicyFieldMeta(
            "materiality.max_discrepancy_pct", "Materiality rules",
            "Max discrepancy %", "Auto-resolve forbidden when the gap exceeds this fraction of transaction value.",
            "fraction (0-1)", float,
        ),
        # ── Batch grouping (deferred stage) ──────────────────
        PolicyFieldMeta(
            "matching.batch.enabled", "Batch grouping",
            "Batch matcher switch", "Many-to-one aggregated-payout grouping stage.",
            "bool", bool,
        ),
        PolicyFieldMeta(
            "matching.batch.max_components", "Batch grouping",
            "Max components per batch", "Maximum gateway/ERP credits allowed to aggregate into one bank credit.",
            "count", int,
        ),
        PolicyFieldMeta(
            "matching.batch.date_window_days", "Batch grouping",
            "Batch date window", "Every batch component must fall within this window of the bank credit date.",
            "days", int,
        ),
        PolicyFieldMeta(
            "matching.batch.amount_tolerance_pct", "Batch grouping",
            "Batch amount tolerance", "Fractional tolerance between summed components and the bank credit amount.",
            "pct (0-0.1)", float,
        ),
    ]
}

_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "matching.deterministic.enabled": _bool,
    "matching.deterministic.date_window_days": _int_range(0, 60),
    "matching.fuzzy.enabled": _bool,
    "matching.fuzzy.score_threshold": _num_range(0, 100),
    "matching.fuzzy.amount_tolerance_pct": _num_range(0, 0.1),
    "matching.fuzzy.date_window_days": _int_range(0, 90),
    "matching.semantic.enabled": _bool,
    "matching.semantic.similarity_threshold": _num_range(0, 1),
    "matching.semantic.top_k": _int_range(1, 50),
    "gate.auto_resolve_enabled": _bool,
    "gate.ai_min_confidence_autoresolve": _num_range(0, 1),
    "matching.ai.similarity_autoresolve_min": _num_range(0, 1),
    "review.force_human_above_inr": _num_range(0, 100_000_000),
    "materiality.max_abs_discrepancy_inr": _num_range(0, 100_000_000),
    "materiality.max_discrepancy_pct": _num_range(0, 1),
    "matching.batch.enabled": _bool,
    "matching.batch.max_components": _int_range(2, 100),
    "matching.batch.date_window_days": _int_range(0, 90),
    "matching.batch.amount_tolerance_pct": _num_range(0, 0.5),
}

GROUP_ORDER = [
    "Matching thresholds",
    "Auto-resolve gates",
    "Materiality rules",
    "Batch grouping",
]


class PolicyError(ValueError):
    """Invalid key or value; routers map this to HTTP 400."""


def validate_value(key: str, value: Any) -> Any:
    validator = _VALIDATORS.get(key)
    if validator is None:
        raise PolicyError(f"'{key}' is not an editable policy key")
    try:
        return validator(value)
    except ValueError as exc:
        raise PolicyError(f"invalid value for '{key}': {exc}") from exc


async def list_policy(db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(select(PolicyConfig).order_by(PolicyConfig.key))
    ).scalars().all()

    last_change: dict[str, dict | None] = {}
    history_rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "policy.updated")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200)
        )
    ).scalars().all()
    for entry in history_rows:
        details = entry.details or {}
        key = details.get("key")
        if key and key not in last_change:
            last_change[key] = {
                "actor": entry.actor,
                "at": entry.created_at.isoformat(),
                "before": details.get("before"),
                "after": details.get("after"),
            }

    fields = []
    known_keys = set(POLICY_FIELDS)
    for row in rows:
        meta = POLICY_FIELDS.get(row.key)
        if meta is None:
            # Seeded but not yet registered: still show, read-only.
            fields.append(
                {
                    "key": row.key,
                    "group": "Other",
                    "label": row.key,
                    "description": row.description,
                    "unit": "",
                    "value_type": "unknown",
                    "value": row.value,
                    "editable": False,
                    "last_changed": last_change.get(row.key),
                    "updated_by": row.updated_by,
                }
            )
            continue
        known_keys.discard(row.key)
        fields.append(
            {
                "key": meta.key,
                "group": meta.group,
                "label": meta.label,
                "description": meta.description,
                "unit": meta.unit,
                "value_type": meta.value_type.__name__,
                "value": row.value,
                "editable": True,
                "last_changed": last_change.get(row.key),
                "updated_by": row.updated_by,
            }
        )
    for orphan in sorted(known_keys):
        meta = POLICY_FIELDS[orphan]
        fields.append(
            {
                "key": meta.key,
                "group": meta.group,
                "label": meta.label,
                "description": meta.description,
                "unit": meta.unit,
                "value_type": meta.value_type.__name__,
                "value": None,
                "editable": True,
                "last_changed": None,
                "updated_by": None,
            }
        )

    ordered = [f for g in GROUP_ORDER for f in fields if f["group"] == g]
    ordered += [f for f in fields if f["group"] not in GROUP_ORDER]
    return ordered


async def update_policy(db: AsyncSession, *, key: str, value: Any, actor: str) -> dict:
    checked = validate_value(key, value)

    row = await db.get(PolicyConfig, key)
    if row is None:
        raise PolicyError(f"unknown policy key '{key}'")

    before = row.value
    if before == checked:
        return {"key": key, "value": row.value, "changed": False}

    row.value = checked
    row.updated_by = actor
    await db.flush()
    await record_audit(
        db,
        actor=actor,
        action="policy.updated",
        entity_type="policy",
        entity_id=None,
        before_state={"key": key, "value": before},
        after_state={"key": key, "value": checked},
        details={"key": key, "before": before, "after": checked},
    )
    return {"key": key, "value": checked, "before": before, "changed": True}


async def key_history(db: AsyncSession, key: str, limit: int = 20) -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.action == "policy.updated",
            AuditLog.details["key"].astext == key,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    return list(rows.scalars())


def coerce_out(value: Any) -> Any:
    """JSONB scalars may come back as strings; normalise for the client."""
    if isinstance(value, Decimal):
        return float(value)
    return value
