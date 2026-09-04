from __future__ import annotations

import re
from decimal import Decimal

from app.models import Transaction
from app.services.parsers.base import business_date
from app.services.reasoning.base import ReasoningAnalysis, ReasoningDecision

_REFERENCE_KEYS = ("payment_id", "payment_ref", "utr", "ref_no")


def _tokens(txn: Transaction) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (txn.narration or "").lower()))


def _jaccard(a: Transaction, b: Transaction) -> float:
    tokens_a, tokens_b = _tokens(a), _tokens(b)
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


_BATCH_KEYWORDS = {
    "aggregate",
    "aggregated",
    "payout",
    "payouts",
    "batch",
    "combined",
    "consolidated",
    "pooled",
    "settlement",
}


def _signals_batched(bank: Transaction, components: list[Transaction]) -> bool:
    """Corroboration that a proposed exact-sum group is a *real* batch."""
    narration_signal = any(
        keyword in (bank.narration or "").lower() for keyword in _BATCH_KEYWORDS
    )
    sources = {c.source for c in components}
    return narration_signal and len(sources) == 1


class HeuristicReasoningAgent:
    name = "heuristic-offline"

    async def decide(
        self,
        txn: Transaction,
        candidates: list[tuple[Transaction, object]],
        policy: dict,
    ) -> ReasoningDecision:
        best, similarity = candidates[0]
        window_days = int(policy["matching.fuzzy.date_window_days"])
        delta = abs((business_date(best.txn_date) - business_date(txn.txn_date)).days)
        overlap = _jaccard(txn, best)
        high = max(best.amount, txn.amount)
        low = min(best.amount, txn.amount)
        ratio = float(low / high) if high > 0 else 0.0
        shared = _tokens(txn) & _tokens(best)

        economically_aligned = ratio >= 0.95 and delta <= window_days
        if economically_aligned and best.amount == txn.amount and overlap >= 0.15:
            return ReasoningDecision(
                decision="match",
                confidence=0.93,
                rationale=(
                    f"Amounts identical at INR {txn.amount}, dates {delta} day(s) apart, "
                    f"narrations share {len(shared)} distinguishing token(s) "
                    f"(jaccard={overlap:.2f}, similarity={similarity})."
                ),
            )
        if economically_aligned and overlap >= 0.20:
            return ReasoningDecision(
                decision="match",
                confidence=0.92,
                rationale=(
                    f"Amounts align within fee/tax tolerance (ratio={ratio:.4f}), dates "
                    f"{delta} day(s) apart, narrations corroborate via {len(shared)} "
                    f"shared token(s) (jaccard={overlap:.2f}, similarity={similarity})."
                ),
            )
        if economically_aligned and overlap >= 0.05:
            return ReasoningDecision(
                decision="needs_human",
                confidence=0.55,
                rationale=(
                    f"Amounts near-equal (ratio={ratio:.4f}) and narrations weakly "
                    f"corroborate (jaccard={overlap:.2f}), but no shared reference "
                    "exists and the residual gap is unexplained."
                ),
            )
        if overlap >= 0.30:
            return ReasoningDecision(
                decision="needs_human",
                confidence=0.60,
                rationale=f"Narrations overlap substantially (jaccard={overlap:.2f}) but economics disagree.",
            )
        return ReasoningDecision(
            decision="no_match",
            confidence=0.85,
            rationale=(
                f"No corroborating evidence: amounts ratio={ratio:.4f}, date delta="
                f"{delta}d, narration jaccard={overlap:.2f}."
            ),
        )

    async def analyze(
        self,
        txn: Transaction,
        policy: dict,
        *,
        missing_sources: list[str] | None = None,
        references: list[tuple[Transaction, object]] | None = None,
    ) -> ReasoningAnalysis:
        """Classify a held transaction that no matcher could pair.

        Deterministic mirror of the LLM analysis prompt: data-quality defects
        surface first, refunds are always investigations, an intact record with
        a reference anchor reads as likely-pending, and a below-threshold
        semantic near-miss (similarity >= 0.50, still under the live gate)
        escalates for an operator glance. Never proposes a match.
        """
        missing: list[str] = []
        if not (txn.narration or "").strip():
            missing.append("narration")
        if not (txn.counterparty or "").strip():
            missing.append("counterparty")
        if not (txn.status or "").strip():
            missing.append("status")
        if missing:
            return ReasoningAnalysis(
                classification="data_quality",
                confidence=0.8,
                rationale=(
                    f"{len(missing)} field(s) are blank ({', '.join(missing)}). "
                    "With no narration or counterparty the matchers have nothing "
                    "to anchor on, so this hold is most plausibly an upload "
                    "defect rather than a real pending settlement."
                ),
            )

        if getattr(txn, "transaction_type", "settlement") == "refund":
            return ReasoningAnalysis(
                classification="manual_investigation",
                confidence=0.85,
                rationale=(
                    "Refunds are deliberately held for a human to confirm the "
                    "original settlement they reverse; no matcher stage pairs "
                    "them. Verify the origin against the gateway/ERP statement."
                ),
            )

        if references:
            best = max(float(s) for _, s in references)
            if best >= 0.50:
                return ReasoningAnalysis(
                    classification="manual_investigation",
                    confidence=0.7,
                    rationale=(
                        f"The unmatched pool holds a cross-source record at "
                        f"similarity {best:.3f}, below the live "
                        f"{policy.get('matching.semantic.similarity_threshold')} "
                        "gate. Likely a near-miss worth an operator glance, but "
                        "not enough to propose a match."
                    ),
                )

        raw = txn.raw or {}
        identifier = next(
            (raw.get(key) for key in _REFERENCE_KEYS if raw.get(key)), None
        )
        if identifier is None and (txn.external_ref or "").startswith(
            ("pay_", "UTR", "INV")
        ):
            identifier = txn.external_ref

        missing_src = missing_sources or []
        if identifier:
            base = (
                f"Carries an explicit reference anchor ({identifier}) with "
                "valid narration, so this looks like a genuine transaction "
                "whose counterpart has simply not been ingested yet"
            )
            if missing_src:
                base += f"; the group is still missing source(s): {', '.join(missing_src)}"
            return ReasoningAnalysis(
                classification="likely_pending",
                confidence=0.6,
                rationale=base + ". Likely a pending match on the next cycle.",
            )

        return ReasoningAnalysis(
            classification="likely_pending",
            confidence=0.5,
            rationale=(
                "Fields are intact but no reference anchor survives in the "
                "record and no matcher found a counterpart. Most plausibly the "
                "counterpart arrives later or uses different wording; treat as "
                "pending and re-check on the next ingestion cycle."
            ),
        )

    async def decide_batch(
        self,
        bank: Transaction,
        components: list[Transaction],
        policy: dict,
    ) -> ReasoningDecision:
        """Assess a many-to-one aggregated-payout candidate group.

        Sum-matching alone is deliberately NOT enough: an exact arithmetic
        fit with no corroborating signal is treated as a coincidence risk and
        escalated (needs_human). Only an exact sum *combined with* bank-
        narration batch wording and single-source components auto-promotes.
        """
        n = len(components)
        total = sum((c.amount for c in components), Decimal("0"))
        residual = abs(total - bank.amount)
        sources = sorted({c.source for c in components})
        narration_signal = any(
            keyword in (bank.narration or "").lower() for keyword in _BATCH_KEYWORDS
        )
        same_source = len(sources) == 1
        exact = residual == 0

        if not exact:
            return ReasoningDecision(
                decision="needs_human",
                confidence=0.55,
                rationale=(
                    f"{n} component(s) sum to INR {total} vs bank INR {bank.amount} "
                    f"(residual INR {residual}); the gap looks like fees/taxes, "
                    "which is plausible but not confirmable without supporting "
                    "evidence. Escalated."
                ),
            )

        if narration_signal and same_source:
            confidence = 0.93 if n >= 3 else 0.91
            return ReasoningDecision(
                decision="match",
                confidence=confidence,
                rationale=(
                    f"Exact aggregated payout: {n} component(s) sum to INR {bank.amount} "
                    f"with zero residual; bank narration signals a batched payout "
                    f"('aggregated/payout') and all components share source "
                    f"{sources[0]}. Fits a genuine settlement batch."
                ),
            )

        if narration_signal or same_source:
            return ReasoningDecision(
                decision="needs_human",
                confidence=0.60,
                rationale=(
                    f"Exact sum of INR {bank.amount} but partial corroboration only "
                    f"(batch narration={'yes' if narration_signal else 'no'}, "
                    f"single source={'yes' if same_source else 'no'}, sources={sources}). "
                    "Cannot rule out coincidence; escalated."
                ),
            )

        return ReasoningDecision(
            decision="needs_human",
            confidence=0.50,
            rationale=(
                f"Exact arithmetic fit: {n} component(s) summing to INR {bank.amount} "
                "with zero residual, but bank narration gives no batch signal "
                "and components span mixed sources. A suspiciously-exact "
                "coincidental sum cannot be excluded; escalated."
            ),
        )
