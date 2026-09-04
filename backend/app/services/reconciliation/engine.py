from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from time import perf_counter

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    ExceptionRecord,
    Match,
    MatchParticipant,
    PolicyConfig,
    Transaction,
)
from app.services.audit import record_audit
from app.services.parsers.base import business_date
from app.services.reasoning import get_agent
from app.services.reasoning.base import ReasoningDecision
from app.services.semantic.matcher import SemanticMatcher

_FUZZY_WEIGHTS = {"amount": 0.55, "date": 0.35, "text": 0.10}
_ALL_SOURCES = {"razorpay", "bank", "erp"}
_REQUIRED_CORE = {"razorpay", "erp"}
_PIPELINE_EXCEPTION_TYPES = [
    "unmatched",
    "amount_mismatch",
    "low_confidence_ai",
    "manual_review_required",
    "refund",
]


async def load_policy(db: AsyncSession) -> dict:
    rows = (await db.execute(select(PolicyConfig))).scalars().all()
    return {row.key: row.value for row in rows}


@dataclass(slots=True)
class _Leg:
    a: uuid.UUID
    b: uuid.UUID
    kind: str
    confidence: Decimal


@dataclass(slots=True)
class _Group:
    members: set[uuid.UUID] = field(default_factory=set)
    legs: list[_Leg] = field(default_factory=list)

    @property
    def kinds(self) -> set[str]:
        return {leg.kind for leg in self.legs}

    @property
    def confidence(self) -> Decimal:
        return min((leg.confidence for leg in self.legs), default=Decimal("1"))


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _within_window(a: Transaction, b: Transaction, days: int) -> bool:
    delta = abs((business_date(b.txn_date) - business_date(a.txn_date)).days)
    return delta <= days


def _erp_amount_matches(rz: Transaction, erp: Transaction) -> bool:
    candidates = {rz.amount}
    gross = (rz.raw or {}).get("gross_amount")
    if gross:
        try:
            candidates.add(Decimal(str(gross)))
        except Exception:
            pass
    return erp.amount in candidates


def pair_deterministic(
    txns: list[Transaction], policy: dict
) -> tuple[list[_Leg], list[dict]]:
    window_days = int(policy["matching.deterministic.date_window_days"])
    by_source: dict[str, list[Transaction]] = defaultdict(list)
    for txn in txns:
        by_source[txn.source].append(txn)

    legs: list[_Leg] = []
    conflicts: list[dict] = []

    erp_by_ref: dict[str, list[Transaction]] = {}
    for erp in by_source["erp"]:
        ref = (erp.raw or {}).get("payment_ref")
        if ref:
            erp_by_ref.setdefault(ref, []).append(erp)

    bank_by_ref = {b.external_ref: b for b in by_source["bank"]}

    for rz in sorted(by_source["razorpay"], key=lambda t: t.external_ref):
        claimed = erp_by_ref.get(rz.external_ref, [])
        in_window = [e for e in claimed if _within_window(rz, e, window_days)]
        if len(in_window) > 1:
            conflicts.append(
                {
                    "transaction_ref": rz.external_ref,
                    "reason": "multiple ERP invoices claim payment_ref",
                    "candidates": [e.external_ref for e in in_window],
                }
            )
            continue
        if len(in_window) == 1 and _erp_amount_matches(rz, in_window[0]):
            legs.append(_Leg(rz.id, in_window[0].id, "exact", Decimal("1")))

        utr = (rz.raw or {}).get("utr")
        bank = bank_by_ref.get(utr) if utr else None
        if (
            bank is not None
            and bank.direction == "credit"
            and bank.amount == rz.amount
            and _within_window(rz, bank, window_days)
        ):
            legs.append(_Leg(rz.id, bank.id, "exact", Decimal("1")))

    return legs, conflicts


def fuzzy_threshold(policy: dict) -> Decimal:
    return Decimal(str(float(policy["matching.fuzzy.score_threshold"]) / 100))


def fuzzy_score(
    a: Transaction,
    b: Transaction,
    policy: dict,
    amount_tolerance_pct: float | None = None,
) -> Decimal | None:
    if "debit" in (a.direction, b.direction):
        return None

    window_days = int(policy["matching.fuzzy.date_window_days"])
    if amount_tolerance_pct is None:
        amount_tolerance_pct = float(policy["matching.fuzzy.amount_tolerance_pct"])
    tolerance = amount_tolerance_pct

    delta = abs((business_date(b.txn_date) - business_date(a.txn_date)).days)
    if delta > window_days:
        return None

    high = max(a.amount, b.amount)
    low = min(a.amount, b.amount)
    ratio = float(low / high) if high > 0 else 0.0
    if ratio < 1 - tolerance:
        return None

    amount_component = 1.0 if a.amount == b.amount else ratio
    date_component = 1.0 if delta == 0 else round(1 - delta / max(window_days, 1), 4)
    text_component = 0.0
    if a.narration and b.narration:
        text_component = token_set_ratio(_norm(a.narration), _norm(b.narration)) / 100

    score = (
        _FUZZY_WEIGHTS["amount"] * amount_component
        + _FUZZY_WEIGHTS["date"] * date_component
        + _FUZZY_WEIGHTS["text"] * text_component
    )
    return Decimal(str(score)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class _BatchGroup:
    """A many-to-one candidate: one bank credit + an exact/tolerant subset sum.

    Generation is pure arithmetic (cheap, exact, auditable) — it is decision-
    neutral. The AI agent decides whether the arithmetic fit is a genuine
    aggregated payout or a coincidence.
    """

    bank: Transaction
    members: list[Transaction]
    total: Decimal
    residual: Decimal


_BATCH_POOL_LIMIT = 16
_BATCH_MAX_GROUPS = 4


def batch_candidate_groups(
    bank: Transaction,
    components: list[Transaction],
    policy: dict,
) -> list[_BatchGroup]:
    """Subset-sum candidate generation for one unmatched bank credit.

    Considers only unmatched same-window razorpay/ERP credits (2 ..= the
    max_components cap), allows a fractional tolerance on the summed total,
    and returns candidate groups ranked by (component count, residual) — the
    plainest explanation first. Exports nothing; the caller feeds the group to
    the AI reasoning agent.
    """
    if bank.source != "bank" or bank.direction != "credit":
        return []

    window_days = int(policy["matching.batch.date_window_days"])
    tolerance = Decimal(str(float(policy["matching.batch.amount_tolerance_pct"])))
    max_components = int(policy["matching.batch.max_components"])

    pool = [
        c
        for c in components
        if c.source in ("razorpay", "erp")
        and c.direction == "credit"
        and _within_window(bank, c, window_days)
    ]
    if len(pool) < 2:
        return []

    if len(pool) > _BATCH_POOL_LIMIT:
        pool.sort(
            key=lambda c: (
                abs((business_date(c.txn_date) - business_date(bank.txn_date)).days),
                c.external_ref,
            )
        )
        pool = pool[:_BATCH_POOL_LIMIT]

    pool.sort(key=lambda c: (c.source, str(c.txn_date), c.external_ref))
    n = len(pool)
    target_cents = int(bank.amount * 100)
    if tolerance > 0:
        slack_cents = int((bank.amount * tolerance) * 100)
    else:
        slack_cents = 0

    amounts_cents = [(int(c.amount * 100)) for c in pool]
    findings: list[tuple[int, list[int], int]] = []

    def visit(start: int, remaining: int, count: int, total_cents: int, chosen: list[int]) -> None:
        if total_cents > target_cents + slack_cents:
            return
        if remaining == total_cents and 2 <= count <= max_components:
            findings.append((count, list(chosen), abs(total_cents - target_cents)))
        if count >= max_components:
            return
        for index in range(start, n):
            visit(index + 1, remaining, count + 1, total_cents + amounts_cents[index], chosen + [index])

    if target_cents + slack_cents <= sum(amounts_cents):
        visit(0, target_cents, 0, 0, [])
    if not findings:
        return []

    findings.sort(key=lambda f: (f[0], f[2], f[1]))
    findings = findings[:_BATCH_MAX_GROUPS]

    groups: list[_BatchGroup] = []
    for count, chosen, residual_cents in findings:
        members = [pool[index] for index in chosen]
        total = sum((c.amount for c in members), Decimal("0"))
        residual = Decimal(residual_cents) / Decimal("100")
        groups.append(_BatchGroup(bank=bank, members=members, total=total, residual=residual))
    return groups


class _Components:
    def __init__(self, txns: list[Transaction]):
        self.groups: dict[uuid.UUID, _Group] = {}
        self.parent: dict[uuid.UUID, uuid.UUID] = {}
        for txn in txns:
            self.parent[txn.id] = txn.id
            self.groups[txn.id] = _Group(members={txn.id})

    def find(self, node: uuid.UUID) -> uuid.UUID:
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, leg: _Leg) -> None:
        root_a, root_b = self.find(leg.a), self.find(leg.b)
        if root_a == root_b:
            self.groups[root_a].legs.append(leg)
            return
        group_a, group_b = self.groups[root_a], self.groups[root_b]
        group_a.members |= group_b.members
        group_a.legs.extend(group_b.legs)
        group_a.legs.append(leg)
        del self.groups[root_b]
        self.parent[root_b] = root_a


def partition_groups(
    txns: list[Transaction], legs: list[_Leg]
) -> tuple[list[_Group], list[_Group]]:
    components = _Components(txns)
    for leg in legs:
        components.union(leg)

    multi = [g for g in components.groups.values() if len(g.members) > 1]
    singles = [g for g in components.groups.values() if len(g.members) == 1]
    multi.sort(key=lambda g: min(str(m) for m in g.members))
    singles.sort(key=lambda g: str(next(iter(g.members))))
    return multi, singles


def _single_txn(group: _Group, txns_by_id: dict[uuid.UUID, Transaction]) -> Transaction:
    return txns_by_id[next(iter(group.members))]


def _is_inert_bank_debit(txn: Transaction) -> bool:
    return txn.source == "bank" and txn.direction == "debit"


def _is_refund(txn: Transaction) -> bool:
    """A transaction classified as a refund/reversal (money flowing back out).
    Derived at ingestion from the source sign/status and surfaced via
    transaction_type; never fed into positive-credit settlement matching."""
    return getattr(txn, "transaction_type", "settlement") == "refund"


def augment_incomplete_groups(
    tentative: list[_Group],
    singles: list[_Group],
    txns_by_id: dict[uuid.UUID, Transaction],
    policy: dict,
) -> None:
    threshold = fuzzy_threshold(policy)
    taken: set[uuid.UUID] = {m for g in tentative for m in g.members}
    available = [txns_by_id[next(iter(g.members))] for g in singles]

    for group in tentative:
        sources = {txns_by_id[m].source for m in group.members}
        for missing_source in ("bank", "erp"):
            if missing_source in sources:
                continue
            anchors = [
                txns_by_id[m]
                for m in group.members
                if txns_by_id[m].source == "razorpay"
            ] or [txns_by_id[m] for m in sorted(group.members)]

            best: tuple[Decimal, Transaction, Transaction] | None = None
            for candidate in available:
                if candidate.id in taken:
                    continue
                if candidate.source != missing_source:
                    continue
                if _is_inert_bank_debit(candidate):
                    continue
                for anchor in anchors:
                    score = fuzzy_score(anchor, candidate, policy)
                    if score is None or score < threshold:
                        continue
                    if best is None or score > best[0]:
                        best = (score, anchor, candidate)
            if best is None:
                continue
            score, anchor, candidate = best
            group.members.add(candidate.id)
            group.legs.append(_Leg(anchor.id, candidate.id, "fuzzy", score))
            taken.add(candidate.id)
            sources = {txns_by_id[m].source for m in group.members}

    singles[:] = [
        g
        for g in singles
        if next(iter(g.members)) not in taken
    ]


def pair_leftover_singles(
    singles: list[_Group],
    txns_by_id: dict[uuid.UUID, Transaction],
    policy: dict,
) -> tuple[list[_Group], list[_Group]]:
    threshold = fuzzy_threshold(policy)
    count = len(singles)
    used: set[int] = set()
    paired: list[_Group] = []

    for i in range(count):
        if i in used:
            continue
        anchor_txn = _single_txn(singles[i], txns_by_id)
        if _is_inert_bank_debit(anchor_txn):
            continue
        best: tuple[Decimal, int] | None = None
        for j in range(i + 1, count):
            if j in used:
                continue
            other_txn = _single_txn(singles[j], txns_by_id)
            if other_txn.source == anchor_txn.source:
                continue
            if _is_inert_bank_debit(other_txn):
                continue
            score = fuzzy_score(
                anchor_txn, other_txn, policy, amount_tolerance_pct=0.0
            )
            if score is None or score < threshold:
                continue
            if best is None or score > best[0]:
                best = (score, j)
        if best is None:
            continue
        score, j = best
        merged = _Group(
            members=singles[i].members | singles[j].members,
            legs=[_Leg(anchor_txn.id, _single_txn(singles[j], txns_by_id).id, "fuzzy", score)],
        )
        used.update({i, j})
        paired.append(merged)

    leftovers = [singles[i] for i in range(count) if i not in used]
    return paired, leftovers


def _ordered_members(group: _Group, txns_by_id: dict[uuid.UUID, Transaction]) -> list[uuid.UUID]:
    def sort_key(member_id: uuid.UUID):
        txn = txns_by_id[member_id]
        rank = {"razorpay": 0, "erp": 1, "bank": 2}.get(txn.source, 3)
        return (rank, str(txn.txn_date), txn.external_ref)

    return sorted(group.members, key=sort_key)


def _rationale(
    group: _Group,
    txns_by_id: dict[uuid.UUID, Transaction],
    status: str,
    missing: list[str] | None,
    policy: dict,
) -> str:
    exact_legs = sum(1 for leg in group.legs if leg.kind == "exact")
    fuzzy_legs = sum(1 for leg in group.legs if leg.kind == "fuzzy")
    parts: list[str] = []
    if exact_legs:
        parts.append(f"{exact_legs} exact identity leg(s)")
    if fuzzy_legs:
        parts.append(
            f"{fuzzy_legs} fuzzy leg(s) at/above threshold {policy['matching.fuzzy.score_threshold']}"
        )
    refs = [txns_by_id[m].external_ref for m in _ordered_members(group, txns_by_id)]
    base = f"linked {len(refs)} records ({', '.join(refs)}) via {', '.join(parts)}"
    if status == "confirmed":
        participating_sources = sorted(
            {txns_by_id[m].source for m in group.members}
        )
        base += (
            f"; complete {len(participating_sources)}-source agreement auto-resolved"
        )
    else:
        base += f"; incomplete group, missing source(s): {', '.join(missing or ['unknown'])}"
    return base


async def _persist_group(
    db: AsyncSession,
    group: _Group,
    txns_by_id: dict[uuid.UUID, Transaction],
    status: str,
    missing: list[str] | None,
    actor: str,
    policy: dict,
    match_type_override: str | None = None,
    confidence_override: Decimal | None = None,
    rationale_override: str | None = None,
    participant_roles: dict[uuid.UUID, str] | None = None,
    member_order: list[uuid.UUID] | None = None,
) -> Match:
    match_type = match_type_override or (
        "fuzzy" if "fuzzy" in group.kinds else "deterministic"
    )
    confidence = confidence_override or group.confidence
    rationale = rationale_override or _rationale(
        group, txns_by_id, status, missing, policy
    )
    match = Match(
        match_type=match_type,
        confidence_score=confidence.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        status=status,
        resolved_by="auto" if status == "confirmed" else None,
        decided_by=actor if status == "confirmed" else None,
        rationale=rationale,
        policy_snapshot={
            key: value
            for key, value in policy.items()
            if key.startswith(("matching.", "gate.", "materiality.", "review."))
        },
    )
    if status == "confirmed":
        match.resolved_at = dt.datetime.now(dt.timezone.utc)
    db.add(match)
    await db.flush()

    ordered = member_order or _ordered_members(group, txns_by_id)
    for member_id in ordered:
        txn = txns_by_id[member_id]
        role = (participant_roles or {}).get(member_id)
        if role is None:
            role = "primary" if txn.source == "razorpay" else "participant"
        db.add(MatchParticipant(match_id=match.id, transaction_id=member_id, role=role))

    await record_audit(
        db,
        actor=actor,
        action="match.auto_resolved" if status == "confirmed" else "match.proposed",
        entity_type="match",
        entity_id=match.id,
        after_state={
            "match_type": match.match_type,
            "confidence_score": float(match.confidence_score),
            "status": status,
            "members": [txns_by_id[m].external_ref for m in ordered],
        },
        details={"rationale": match.rationale},
    )
    return match


def evaluate_ai_route(
    decision_confidence: Decimal,
    decision_kind: str,
    discrepancy: Decimal,
    value: Decimal,
    policy: dict,
    *,
    similarity: Decimal | None = None,
) -> str:
    """Route an AI decision to auto-resolve / hold / no_match.

    Joint-evidence gate: a 1:1 semantic 'match' may auto-resolve only if BOTH
    conditions hold — best-candidate similarity >= matching.ai.
    similarity_autoresolve_min AND confidence >= gate.ai_.
    min_confidence_autoresolve. A weak-similarity 'match' (e.g. a small model
    claiming confidence ~1.0 on a 0.571-similarity pair) is forced to
    needs_human regardless of the model's confidence, closing the
    small-model-overconfidence failure mode.
    """
    if decision_kind == "no_match":
        return "no_match"
    if decision_kind == "needs_human":
        return "needs_human"
    sim_floor_raw = policy.get("matching.ai.similarity_autoresolve_min")
    if decision_kind == "match" and similarity is not None and sim_floor_raw is not None:
        sim_floor = Decimal(str(float(sim_floor_raw)))
        if similarity < sim_floor:
            return "needs_human"
    gate = Decimal(str(float(policy["gate.ai_min_confidence_autoresolve"])))
    confidence = Decimal(str(round(decision_confidence, 4)))
    if _breaches_materiality(discrepancy, value, policy):
        return "hold_materiality"
    if confidence >= gate:
        return "auto_resolve"
    return "hold_low_confidence"


def _priority_for(amount: Decimal, policy: dict) -> str:
    force_above = Decimal(str(policy.get("review.force_human_above_inr", 0)))
    material_abs = Decimal(str(policy.get("materiality.max_abs_discrepancy_inr", 0)))
    if amount >= force_above:
        return "critical"
    if amount >= material_abs:
        return "high"
    return "medium"


async def _open_exception(
    db: AsyncSession,
    txn: Transaction,
    details: dict,
    policy: dict,
    actor: str,
    exception_type: str = "unmatched",
) -> ExceptionRecord:
    exception = ExceptionRecord(
        transaction_id=txn.id,
        exception_type=exception_type,
        priority=_priority_for(txn.amount, policy),
        amount_impact=txn.amount,
        status="open",
    )
    db.add(exception)
    await db.flush()
    await record_audit(
        db,
        actor=actor,
        action="exception.opened",
        entity_type="exception",
        entity_id=exception.id,
        after_state={"status": "open", "exception_type": exception_type},
        details={"transaction_ref": txn.external_ref, **details},
    )
    return exception


def _money_discrepancy(
    group: _Group, txns_by_id: dict[uuid.UUID, Transaction]
) -> Decimal | None:
    bank_total = Decimal("0")
    gateway_total = Decimal("0")
    has_bank = False
    has_gateway = False
    for member_id in group.members:
        txn = txns_by_id[member_id]
        if txn.source == "bank" and txn.direction == "credit":
            bank_total += txn.amount
            has_bank = True
        elif txn.source == "razorpay":
            gateway_total += txn.amount
            has_gateway = True
    if not has_bank or not has_gateway:
        return None
    return abs(bank_total - gateway_total)


def _breaches_materiality(discrepancy: Decimal, group_value: Decimal, policy: dict) -> bool:
    abs_limit = Decimal(str(policy["materiality.max_abs_discrepancy_inr"]))
    pct_limit = Decimal(str(policy["materiality.max_discrepancy_pct"]))
    if discrepancy > abs_limit:
        return True
    return group_value > 0 and (discrepancy / group_value) > pct_limit


async def _reset_proposed_state(db: AsyncSession, scope_ids: list[uuid.UUID]) -> None:
    await db.execute(
        delete(Match).where(
            Match.status == "proposed",
            Match.id.in_(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.transaction_id.in_(scope_ids)
                )
            ),
        )
    )

    await db.execute(
        delete(Match).where(
            Match.status == "proposed",
            ~select(MatchParticipant.match_id)
            .where(MatchParticipant.match_id == Match.id)
            .exists(),
        )
    )

    await db.execute(
        delete(ExceptionRecord).where(
            ExceptionRecord.transaction_id.in_(scope_ids),
            ExceptionRecord.exception_type.in_(_PIPELINE_EXCEPTION_TYPES),
            ExceptionRecord.status == "open",
        )
    )


async def _confirmed_member_ids(
    db: AsyncSession, scope_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    query = (
        select(MatchParticipant.transaction_id)
        .join(Match, Match.id == MatchParticipant.match_id)
        .where(
            MatchParticipant.transaction_id.in_(scope_ids),
            Match.status == "confirmed",
        )
    )
    result = await db.execute(query)
    return set(result.scalars().all())


async def _run_semantic_ai_stage(
    db: AsyncSession,
    singles: list[_Group],
    txns_by_id: dict[uuid.UUID, Transaction],
    policy: dict,
    actor: str,
    summary: dict,
) -> set[uuid.UUID]:
    settings = get_settings()
    if not policy.get("matching.semantic.enabled", True):
        return set()
    if not policy.get("gate.ai_reasoning_enabled", True):
        return set()

    eligible = [
        _single_txn(group, txns_by_id)
        for group in singles
        if not _is_inert_bank_debit(_single_txn(group, txns_by_id))
    ]
    if len(eligible) < 2:
        return set()

    matcher = SemanticMatcher(settings)
    matcher.compact_if_stale(set(txns_by_id.keys()))
    await matcher.ensure_indexed(eligible)
    agent = get_agent(settings)

    consumed: set[uuid.UUID] = set()
    ordered = sorted(eligible, key=lambda t: (t.txn_date, t.external_ref))
    for txn in ordered:
        if txn.id in consumed:
            continue
        pool = [
            t
            for t in eligible
            if t.id != txn.id and t.id not in consumed
        ]
        candidates = await matcher.top_candidates(txn, pool, policy)
        if not candidates:
            continue

        decision = await agent.decide(txn, candidates, policy)
        best, similarity = candidates[0]
        discrepancy = abs(best.amount - txn.amount)
        value = max(best.amount, txn.amount)

        await record_audit(
            db,
            actor=actor,
            action="ai.decision",
            entity_type="transaction",
            entity_id=txn.id,
            details={
                "model": agent.name,
                "decision": decision.decision,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "best_candidate": {
                    "ref": best.external_ref,
                    "source": best.source,
                    "similarity": float(similarity),
                },
                "all_candidates": [
                    {"ref": c.external_ref, "similarity": float(s)}
                    for c, s in candidates
                ],
                "similarity_autoresolve_min": policy.get(
                    "matching.ai.similarity_autoresolve_min"
                ),
            },
        )
        summary["ai_candidates_evaluated"] += 1

        route = evaluate_ai_route(
            Decimal(str(decision.confidence)), decision.decision, discrepancy, value, policy,
            similarity=similarity,
        )
        if route == "no_match":
            summary["ai_no_match"] += 1
            continue

        group = _Group(
            members={txn.id, best.id},
            legs=[_Leg(txn.id, best.id, "semantic", similarity)],
        )
        confidence_override = Decimal(str(decision.confidence))
        rationale_override = (
            f"AI ({agent.name}) {decision.decision} vs {best.external_ref} "
            f"(similarity={similarity}): {decision.rationale}"
        )

        if route == "auto_resolve":
            await _persist_group(
                db, group, txns_by_id, "confirmed", None, actor, policy,
                match_type_override="ai",
                confidence_override=confidence_override,
                rationale_override=rationale_override,
            )
            summary["ai_auto_resolved"] += 1
        else:
            exception_type = {
                "hold_low_confidence": "low_confidence_ai",
                "hold_materiality": "amount_mismatch",
                "needs_human": "manual_review_required",
            }[route]
            details: dict = {
                "stage": "semantic_ai",
                "group_refs": [txn.external_ref, best.external_ref],
                "ai_decision": decision.decision,
                "ai_confidence": decision.confidence,
                "ai_rationale": decision.rationale,
                "similarity": float(similarity),
            }
            sim_floor = policy.get("matching.ai.similarity_autoresolve_min")
            if sim_floor is not None:
                details["similarity_autoresolve_min"] = sim_floor
            if (
                route == "needs_human"
                and decision.decision == "match"
                and sim_floor is not None
                and Decimal(str(float(similarity))) < Decimal(str(float(sim_floor)))
            ):
                details["auto_resolve_blocked_by"] = "similarity_autoresolve_min"
            if route == "hold_materiality":
                details["materiality"] = {
                    "discrepancy": str(discrepancy),
                    "abs_limit": str(policy["materiality.max_abs_discrepancy_inr"]),
                    "pct_limit": str(policy["materiality.max_discrepancy_pct"]),
                }
            await _persist_group(
                db, group, txns_by_id, "proposed", None, actor, policy,
                match_type_override="ai" if route in ("hold_low_confidence", "hold_materiality") else "semantic",
                confidence_override=confidence_override,
                rationale_override=rationale_override,
            )
            primary = txns_by_id[txn.id] if txn.source == "razorpay" else txns_by_id[best.id]
            await _open_exception(
                db, primary, details, policy, actor, exception_type=exception_type
            )
            summary["ai_proposed"] += 1
            summary["exceptions_opened"] += 1

        consumed.update({txn.id, best.id})

    return consumed


async def _run_batch_stage(
    db: AsyncSession,
    singles: list[_Group],
    txns_by_id: dict[uuid.UUID, Transaction],
    policy: dict,
    actor: str,
    summary: dict,
) -> set[uuid.UUID]:
    """Many-to-one aggregated-payout stage (runs last, over true leftovers).

    Runs after deterministic/fuzzy/semantic passes and only ever consumes
    records those stages left unmatched. Candidate *generation* (exact subset
    sums) is decision-neutral; the AI agent judges whether a candidate group
    is a genuine batched settlement. Sum-matching alone never auto-resolves.
    Every evaluated group is written to the audit trail, so rejected candidate
    sets remain visible. Bank credits above the force-human threshold are
    always demoted to human review even on a confident auto route.
    """
    if not policy.get("matching.batch.enabled", True):
        return set()
    if not policy.get("gate.ai_reasoning_enabled", True):
        return set()

    banks = sorted(
        (
            _single_txn(g, txns_by_id)
            for g in singles
            if _single_txn(g, txns_by_id).source == "bank"
            and _single_txn(g, txns_by_id).direction == "credit"
        ),
        key=lambda t: (t.txn_date, t.external_ref),
    )
    components = [
        _single_txn(g, txns_by_id)
        for g in singles
        if _single_txn(g, txns_by_id).source in ("razorpay", "erp")
        and _single_txn(g, txns_by_id).direction == "credit"
    ]
    if not banks or len(components) < 2:
        return set()

    agent = get_agent(get_settings())
    force_human = Decimal(str(policy.get("review.force_human_above_inr", 0)))
    consumed: set[uuid.UUID] = set()

    for bank in banks:
        if bank.id in consumed:
            continue
        available = [c for c in components if c.id not in consumed]
        groups = batch_candidate_groups(bank, available, policy)
        summary["batch_candidates_generated"] += len(groups)
        if not groups:
            continue

        accepted: _BatchGroup | None = None
        accepted_decision: ReasoningDecision | None = None
        route: str | None = None
        held_by_force = False
        evaluated_here = 0

        for group in groups:
            decision = await agent.decide_batch(bank, group.members, policy)
            evaluated_here += 1
            summary["batch_ai_evaluated"] += 1
            await record_audit(
                db,
                actor=actor,
                action="ai.decision",
                entity_type="transaction",
                entity_id=bank.id,
                details={
                    "model": agent.name,
                    "stage": "batch",
                    "decision": decision.decision,
                    "confidence": decision.confidence,
                    "rationale": decision.rationale,
                    "bank": {
                        "ref": bank.external_ref,
                        "amount": str(bank.amount),
                        "date": business_date(bank.txn_date).isoformat(),
                    },
                    "candidates": [
                        {
                            "ref": c.external_ref,
                            "source": c.source,
                            "amount": str(c.amount),
                            "date": business_date(c.txn_date).isoformat(),
                        }
                        for c in group.members
                    ],
                    "group_total": str(group.total),
                    "residual": str(group.residual),
                    "window_days": str(policy["matching.batch.date_window_days"]),
                    "max_components": str(policy["matching.batch.max_components"]),
                    "tolerance_pct": str(policy["matching.batch.amount_tolerance_pct"]),
                },
            )

            route = evaluate_ai_route(
                Decimal(str(decision.confidence)),
                decision.decision,
                group.residual,
                bank.amount,
                policy,
            )
            if route == "no_match":
                summary["batch_no_match"] += 1
                continue
            if route == "auto_resolve" and bank.amount >= force_human:
                route = "needs_human"
                held_by_force = True
            accepted = group
            accepted_decision = decision
            break

        if accepted is None:
            await _open_exception(
                db,
                bank,
                {
                    "stage": "batch",
                    "note": "no batch candidate group accepted by AI reasoning; "
                    "every evaluated group is in the ai.decision audit trail",
                    "candidate_groups_evaluated": evaluated_here,
                },
                policy,
                actor,
            )
            summary["exceptions_opened"] += 1
            consumed.add(bank.id)
            continue

        member_refs = [c.external_ref for c in accepted.members]
        roles = {bank.id: "primary"}
        roles.update({c.id: "participant" for c in accepted.members})
        order_ids = [bank.id, *(c.id for c in accepted.members)]
        linked = _Group(members={bank.id})
        linked.members.update(c.id for c in accepted.members)
        linked.legs.append(
            _Leg(bank.id, accepted.members[0].id, "batch", Decimal(str(accepted_decision.confidence)))
        )
        confidence_override = Decimal(str(accepted_decision.confidence))
        rationale_override = (
            f"AI ({agent.name}) batch {accepted_decision.decision} for "
            f"{len(accepted.members)} component(s) summing to INR {accepted.total} vs "
            f"bank INR {bank.amount} (residual INR {accepted.residual}): "
            f"{accepted_decision.rationale}"
        )

        if route == "auto_resolve":
            await _persist_group(
                db, linked, txns_by_id, "confirmed", None, actor, policy,
                match_type_override="batch",
                confidence_override=confidence_override,
                rationale_override=rationale_override,
                participant_roles=roles,
                member_order=order_ids,
            )
            summary["batch_auto_resolved"] += 1
        else:
            exception_type = {
                "hold_low_confidence": "low_confidence_ai",
                "hold_materiality": "amount_mismatch",
                "needs_human": "manual_review_required",
            }[route]
            details: dict = {
                "stage": "batch",
                "group_refs": [bank.external_ref] + member_refs,
                "ai_decision": accepted_decision.decision,
                "ai_confidence": accepted_decision.confidence,
                "ai_rationale": accepted_decision.rationale,
                "group_total": str(accepted.total),
                "residual": str(accepted.residual),
                "candidates": member_refs,
            }
            if route == "hold_materiality":
                details["materiality"] = {
                    "discrepancy": str(accepted.residual),
                    "abs_limit": str(policy["materiality.max_abs_discrepancy_inr"]),
                    "pct_limit": str(policy["materiality.max_discrepancy_pct"]),
                }
            if held_by_force:
                details["force_human"] = {
                    "threshold_inr": str(force_human),
                    "note": "bank amount exceeds force-human threshold; "
                    "auto-resolve demoted to human review",
                }
            await _persist_group(
                db, linked, txns_by_id, "proposed", None, actor, policy,
                match_type_override="batch",
                confidence_override=confidence_override,
                rationale_override=rationale_override,
                participant_roles=roles,
                member_order=order_ids,
            )
            await _open_exception(
                db, bank, details, policy, actor, exception_type=exception_type
            )
            summary["batch_proposed"] += 1
            summary["exceptions_opened"] += 1

        consumed.update({bank.id, *(c.id for c in accepted.members)})

    return consumed


async def run_reconciliation(
    db: AsyncSession,
    *,
    actor: str,
    transaction_ids: list[uuid.UUID] | None = None,
) -> dict:
    started = perf_counter()
    policy = await load_policy(db)

    stmt = select(Transaction).order_by(Transaction.txn_date, Transaction.external_ref)
    if transaction_ids:
        stmt = stmt.where(Transaction.id.in_(transaction_ids))
    scoped = list((await db.execute(stmt)).scalars().all())
    scope_ids = [t.id for t in scoped]

    summary: dict = {
        "transactions_scanned": len(scoped),
        "exact_auto_resolved": 0,
        "fuzzy_auto_resolved": 0,
        "incomplete_proposed": 0,
        "exceptions_opened": 0,
        "conflicts": [],
        "duration_ms": 0.0,
        "ai_candidates_evaluated": 0,
        "ai_auto_resolved": 0,
        "ai_proposed": 0,
        "ai_no_match": 0,
        "batch_candidates_generated": 0,
        "batch_ai_evaluated": 0,
        "batch_auto_resolved": 0,
        "batch_proposed": 0,
        "batch_no_match": 0,
    }
    if not scope_ids:
        summary["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        return summary

    confirmed_ids = await _confirmed_member_ids(db, scope_ids)
    await _reset_proposed_state(db, scope_ids)
    working = [t for t in scoped if t.id not in confirmed_ids]
    # Refund/reversal rows are their own case, never part of the positive-credit
    # settlement math: excluding them here keeps the deterministic/fuzzy/batch/
    # semantic passes from linking a refund's payment_id back to its original
    # invoice. They surface as labelled refund exceptions instead.
    refund_txns = [t for t in working if _is_refund(t)]
    working = [t for t in working if not _is_refund(t)]
    txns_by_id = {t.id: t for t in working}

    legs, conflicts = pair_deterministic(working, policy)
    summary["conflicts"] = conflicts

    tentative, singles = partition_groups(working, legs)
    augment_incomplete_groups(tentative, singles, txns_by_id, policy)
    fuzzy_pairs, singles = pair_leftover_singles(singles, txns_by_id, policy)
    tentative.extend(fuzzy_pairs)

    ai_consumed = await _run_semantic_ai_stage(
        db, singles, txns_by_id, policy, actor, summary
    )
    if ai_consumed:
        singles[:] = [
            g for g in singles if next(iter(g.members)) not in ai_consumed
        ]

    batch_consumed = await _run_batch_stage(
        db, singles, txns_by_id, policy, actor, summary
    )
    if batch_consumed:
        singles[:] = [
            g for g in singles if next(iter(g.members)) not in batch_consumed
        ]

    for group in tentative:
        sources = {txns_by_id[m].source for m in group.members}
        missing_core = sorted(_REQUIRED_CORE - sources)
        all_missing = sorted(_ALL_SOURCES - sources)

        discrepancy = _money_discrepancy(group, txns_by_id)
        group_value = max((txns_by_id[m].amount for m in group.members), default=Decimal("0"))
        held_by_materiality = (
            not missing_core
            and discrepancy is not None
            and _breaches_materiality(discrepancy, group_value, policy)
        )

        if not missing_core and not held_by_materiality:
            match = await _persist_group(
                db, group, txns_by_id, "confirmed", None, actor, policy
            )
            if match.match_type == "deterministic":
                summary["exact_auto_resolved"] += 1
            else:
                summary["fuzzy_auto_resolved"] += 1
        else:
            await _persist_group(
                db,
                group,
                txns_by_id,
                "proposed",
                all_missing or ["materiality"],
                actor,
                policy,
            )
            summary["incomplete_proposed"] += 1
            primary = txns_by_id[_ordered_members(group, txns_by_id)[0]]
            details = {
                "stage": "pipeline",
                "group_refs": [
                    txns_by_id[m].external_ref for m in _ordered_members(group, txns_by_id)
                ],
                "missing_sources": all_missing,
            }
            exception_type = "unmatched"
            if held_by_materiality:
                exception_type = "amount_mismatch"
                details["materiality"] = {
                    "discrepancy": str(discrepancy),
                    "abs_limit": str(policy["materiality.max_abs_discrepancy_inr"]),
                    "pct_limit": str(policy["materiality.max_discrepancy_pct"]),
                }
            await _open_exception(
                db,
                primary,
                details,
                policy,
                actor,
                exception_type=exception_type,
            )
            summary["exceptions_opened"] += 1

    for single in singles:
        txn = _single_txn(single, txns_by_id)
        if _is_inert_bank_debit(txn):
            continue
        await _open_exception(
            db,
            txn,
            {"stage": "sweep", "note": "no candidate produced by any matcher stage"},
            policy,
            actor,
        )
        summary["exceptions_opened"] += 1

    # Refunds are intentionally excluded from settlement matching; surface each
    # one as its own clearly-labelled refund exception (a pending human case)
    # rather than a cryptic unmatched or a wrong auto-match.
    for refund in refund_txns:
        if refund.id in confirmed_ids:
            continue
        await _open_exception(
            db,
            refund,
            {
                "stage": "refund",
                "note": (
                    "refund/reversal row — not matched to a positive settlement "
                    "by design; pending a human decision"
                ),
            },
            policy,
            actor,
            exception_type="refund",
        )
        summary["exceptions_opened"] += 1

    summary["duration_ms"] = round((perf_counter() - started) * 1000, 2)
    return summary
