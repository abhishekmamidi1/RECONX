from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.models import Transaction
from app.services.parsers.base import business_date

DecisionKind = Literal["match", "no_match", "needs_human"]

AnalysisKind = Literal["likely_pending", "data_quality", "manual_investigation"]


class ReasoningDecision(BaseModel):
    decision: DecisionKind
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class ReasoningAnalysis(BaseModel):
    """Agent output for a held item with no actionable proposal.

    Unlike a decision, an analysis proposes nothing — it reads a single
    unmatched/half-matched transaction and tells a human reviewer whether the
    hold is plausibly a pending record that will pair tomorrow, a data-quality
    defect in the upload, or something that genuinely needs an operator's eyes.
    """

    classification: AnalysisKind
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


def build_prompt(
    txn: Transaction,
    candidates: list[tuple[Transaction, Decimal]],
    policy: dict,
) -> tuple[str, str]:
    system = (
        "You are a payments reconciliation auditor for a fintech company. "
        "You decide whether an unmatched transaction from one data source "
        "corresponds to a candidate record from another source. Money must "
        "never be silently written off or double-counted; when evidence is "
        "insufficient you must escalate to a human. Respond with strict JSON "
        "only, shaped exactly as: "
        '{"decision": "match" | "no_match" | "needs_human", '
        '"confidence": <float 0..1>, "rationale": "<short explanation>"}.'
    )
    policy_lines = [
        f"- semantic similarity threshold (already applied): {policy.get('matching.semantic.similarity_threshold')}",
        f"- AI auto-resolve confidence gate: {policy.get('gate.ai_min_confidence_autoresolve')}",
        f"- max absolute discrepancy allowed for auto-resolve (INR): {policy.get('materiality.max_abs_discrepancy_inr')}",
        f"- max discrepancy as fraction of transaction value: {policy.get('materiality.max_discrepancy_pct')}",
        "- amounts above the force-human threshold always require human review",
    ]
    txn_block = _format_txn("UNMATCHED TRANSACTION", txn)
    candidate_blocks = "\n".join(
        _format_txn(f"CANDIDATE {index + 1} (similarity={similarity})", candidate)
        for index, (candidate, similarity) in enumerate(candidates)
    )
    user = (
        "Policy rules:\n"
        + "\n".join(policy_lines)
        + "\n\n"
        + txn_block
        + "\n\n"
        + candidate_blocks
        + "\n\nDecide if the unmatched transaction matches any candidate. "
        "Consider amount differences (fees/taxes may explain small gaps), "
        "dates, narration wording, and counterparty hints. Output JSON only."
    )
    return system, user


def _format_txn(label: str, txn: Transaction) -> str:
    date = business_date(txn.txn_date).isoformat()
    direction = txn.direction
    return (
        f"{label}:\n"
        f"  source: {txn.source}\n"
        f"  reference: {txn.external_ref}\n"
        f"  amount: INR {txn.amount} ({direction})\n"
        f"  date: {date}\n"
        f"  narration: {txn.narration or '-'}\n"
        f"  counterparty: {txn.counterparty or '-'}\n"
        f"  status: {txn.status or '-'}"
    )


def build_batch_prompt(
    bank: Transaction,
    components: list[Transaction],
    policy: dict,
) -> tuple[str, str]:
    """Prompt for many-to-one aggregated-payout candidates.

    batch_candidate_groups() proposes that several unmatched gateway/books
    credits sum (within policy tolerance) to a single unmatched bank credit.
    The agent must judge whether that is a *genuine* batched settlement vs an
    arithmetic coincidence, and returns the same shapes as build_prompt.
    """
    system = (
        "You are a payments reconciliation auditor for a fintech company. "
        "A payment gateway sometimes batches several individual settlements "
        "into a single bank payout: one bank credit whose amount equals the "
        "sum of several gateway/ERP credits, not a 1:1 match. "
        "You are shown one unmatched bank credit and a PROPOSED set of "
        "unmatched same-source credits whose sum is close to it. Decide "
        "whether this is a genuine aggregated payout or a coincidental sum. "
        "Money must never be silently written off or double-counted; when "
        "evidence is insufficient you must escalate to a human. Respond with "
        "strict JSON only, shaped exactly as: "
        '{"decision": "match" | "no_match" | "needs_human", '
        '"confidence": <float 0..1>, "rationale": "<short explanation>"}.'
    )
    policy_lines = [
        "- a sum within batch tolerance is NOT sufficient evidence on its own: "
        "unrelated credits can coincidentally total a similar amount",
        "- bank narration words like 'AGGREGATED' / 'PAYOUT' / 'BATCH' corroborate "
        "a genuine batch; plain vendor NEFT wording does not",
        "- the component count and the spread of component dates must make sense "
        "for one settlement run; a suspiciously exact round sum from many "
        "disparate records is a coincidence risk, not a confirmation",
        f"- component date window: {policy.get('matching.batch.date_window_days')} day(s)",
        f"- batch amount tolerance: {policy.get('matching.batch.amount_tolerance_pct')}",
        f"- AI auto-resolve confidence gate: {policy.get('gate.ai_min_confidence_autoresolve')}",
        "- amounts above the force-human threshold always require human review",
    ]
    bank_block = _format_txn("BANK CREDIT (aggregated payout target)", bank)

    sum_total = sum((c.amount for c in components), Decimal("0"))
    residual = abs(sum_total - bank.amount)
    component_blocks = "\n".join(
        _format_txn(f"PROPOSED COMPONENT {index + 1}", component)
        for index, component in enumerate(components)
    )
    spread_days = 0
    if len(components) > 1:
        dates = [business_date(c.txn_date) for c in components]
        spread_days = (max(dates) - min(dates)).days

    user = (
        "Policy rules:\n"
        + "\n".join(policy_lines)
        + "\n\n"
        + bank_block
        + "\n\n"
        + component_blocks
        + "\n\n"
        f"Proposed group: {len(components)} component(s), summed "
        f"INR {sum_total}, target INR {bank.amount}, residual INR {residual}, "
        f"date spread {spread_days} day(s). "
        "Decide whether this is a genuine aggregated payout of exactly these "
        "records. Output JSON only."
    )
    return system, user


def build_analysis_prompt(
    txn: Transaction,
    policy: dict,
    *,
    missing_sources: list[str] | None = None,
    references: list[tuple[Transaction, object]] | None = None,
    hold_reason: str | None = None,
) -> tuple[str, str]:
    """Prompt for the candidate-less analysis case.

    Here there is no counterpart to decide on, so the agent cannot output a
    reasoning *decision* (match/no_match). Instead it classifies WHY the
    record is held and what a human should do, with the transaction's own
    fields (amount, date, narration, source, raw identifiers) as the evidence.
    Below-threshold semantic references, when present, are shown strictly as
    informational context ("below threshold — reference only"), never as
    candidates.
    """
    system = (
        "You are a payments reconciliation auditor for a fintech company. "
        "You are shown ONE transaction that no matcher stage could pair. "
        "You must explain the hold to a human reviewer by classifying it into "
        "exactly one of three buckets: "
        "'likely_pending' (the counterpart record probably has not been "
        "ingested yet \u2014 e.g. a settlement that lands at end of day), "
        "'data_quality' (the record itself looks defective \u2014 missing or "
        "garbled narration, empty counterparty, absent reference identifiers), "
        "'manual_investigation' (the record carries enough idiosyncrasy that a "
        "person needs to look at it). Money must never be silently written off "
        "or double-counted; when unsure, prefer manual_investigation. Respond "
        "with strict JSON only, shaped exactly as: "
        '{"classification": "likely_pending" | "data_quality" | '
        '"manual_investigation", "confidence": <float 0..1>, '
        '"rationale": "<short explanation focusing on this transaction>"}.'
    )
    from app.services.reconciliation.engine import _ALL_SOURCES

    policy_lines = [
        f"- live semantic similarity threshold (references below it are informational only): {policy.get('matching.semantic.similarity_threshold')}",
        f"- sources in scope: {sorted(_ALL_SOURCES)}",
    ]
    txn_block = _format_txn("UNMATCHED / HELD TRANSACTION", txn)
    parts = [
        "Policy rules:\n" + "\n".join(policy_lines),
        txn_block,
    ]
    if missing_sources:
        parts.append(
            "The record was linked into an incomplete group; these sources are "
            f"still missing from it: {', '.join(missing_sources)}."
        )
    if hold_reason:
        parts.append(f"Engine note: {hold_reason}")
    if references:
        parts.append(
            "BELOW-THRESHOLD REFERENCES (informational only \u2014 below the live "
            "similarity threshold, may be coincidence, do NOT treat as "
            "candidates):\n"
            + "\n".join(
                _format_txn(f"REFERENCE {index + 1} (similarity={similarity})", r)
                for index, (r, similarity) in enumerate(references)
            )
        )
    else:
        parts.append("No below-threshold references were found in the unmatched pool.")
    parts.append(
        "Explain why this transaction is held and what a reviewer should do. "
        "Consider the amount, date, narration wording, whether it carries an "
        "explicit reference identifier (payment id / UTR / invoice), whether "
        "fields appear missing or garbled, and the missing/absent sources. "
        "Output JSON only."
    )
    return system, "\n\n".join(parts)


def fallback_decision(reason: str) -> ReasoningDecision:
    return ReasoningDecision(
        decision="needs_human",
        confidence=0.0,
        rationale=f"AI reasoning unavailable; escalated to human review. ({reason})",
    )


def fallback_analysis(reason: str) -> ReasoningAnalysis:
    return ReasoningAnalysis(
        classification="manual_investigation",
        confidence=0.0,
        rationale=f"AI analysis unavailable; treat as needing manual review. ({reason})",
    )
