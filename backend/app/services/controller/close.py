"""Run-to-close controller pass.

The close pass is a *read-only decision layer* over the reconciliation state a
run produced. It never mutates matches or exceptions. For every scoped record
it decides:

* ``matched``   — the record is a member of a confirmed match,
* ``no_match``  — the record is an inert non-settlement bank debit/charge with
                  no proposal and no open exception,
* ``deferred``  — everything else, tagged with one of the deterministic
                  ``REASON_CODES`` derived from pipeline evidence.

The pass writes only audit rows (``controller.close_started``,
``controller.close_matched`` / ``controller.close_no_match`` /
``controller.close_deferred`` per record, ``controller.close_completed``), so
the underlying reconciliation data stays the single source of truth and the
whole decision set remains reproducible from the audit trail.
"""

from __future__ import annotations

import csv
import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, ExceptionRecord, Match, MatchParticipant, Transaction
from app.services.audit import record_audit
from app.services.reconciliation.engine import load_policy

_MODULE_ROOT = Path(__file__).resolve().parent.parent.parent  # <backend>/app
_SAMPLE_DIR = _MODULE_ROOT.parent / "sample_data"  # <backend>/sample_data
_GOLDEN_LABELS_FILE = "close_ground_truth.csv"

REASON_CODES = [
    "no_candidate",
    "below_threshold",
    "conflicting_evidence",
    "materiality",
    "data_quality",
    "manual_investigation",
]

DECISION_MATCHED = "matched"
DECISION_NO_MATCH = "no_match"
DECISION_DEFERRED = "deferred"

_REASON_ACTIONS = {
    "no_candidate": (
        "Review the unmatched record; identify and ingest the missing "
        "counterparty before closing."
    ),
    "below_threshold": (
        "Review the low-confidence semantic proposal; adjust the similarity "
        "gate or confirm manually before closing."
    ),
    "conflicting_evidence": (
        "Record appears in multiple proposed matches; resolve the conflict "
        "before closing."
    ),
    "materiality": (
        "Resolve the material amount discrepancy between the proposed members "
        "before closing."
    ),
    "data_quality": (
        "Fix the missing/blank transaction data, then re-run reconciliation "
        "and close."
    ),
    "manual_investigation": (
        "Manual investigation required before this record can be closed."
    ),
}


@dataclass(slots=True)
class CloseResultRow:
    record_ref: str
    source: str
    amount: Decimal
    decision: str
    matched_with: list[str]
    reason_code: str | None
    rationale: str
    suggested_next_action: str

    def to_dict(self) -> dict:
        return {
            "record_ref": self.record_ref,
            "source": self.source,
            "amount": str(self.amount),
            "decision": self.decision,
            "matched_with": self.matched_with or None,
            "reason_code": self.reason_code,
            "rationale": self.rationale,
            "suggested_next_action": self.suggested_next_action,
        }


def reason_code_for_exception(
    exception_type: str,
    *,
    blocked_by_similarity: bool = False,
    narration_blank: bool = False,
) -> str:
    """Map one piece of pipeline evidence to a DEFER reason code.

    Deterministic and unit-testable: the close pass never re-reasons about
    economics, it only reads the evidence the pipeline already persisted.
    """
    if exception_type in ("refund", "duplicate_suspect"):
        return "manual_investigation"
    if exception_type == "amount_mismatch":
        return "materiality"
    if exception_type == "low_confidence_ai":
        return "below_threshold"
    if exception_type == "manual_review_required":
        return "below_threshold" if blocked_by_similarity else "manual_investigation"
    if exception_type == "unmatched":
        return "data_quality" if narration_blank else "no_candidate"
    return "manual_investigation"


def _narration_blank(txn: Transaction) -> bool:
    return not (txn.narration or "").strip()


def _blocked_by_similarity(details: dict) -> bool:
    """Was the semantic proposal held below the similarity-autoresolve floor?

    The engine records the explicit flag when an AI 'match' decision trips the
    floor; when the agent itself returns ``needs_human`` for a weak candidate
    (no 'match' reached the floor check) the same conclusion is recoverable
    from the stored similarity vs ``similarity_autoresolve_min``.
    """
    if details.get("auto_resolve_blocked_by") == "similarity_autoresolve_min":
        return True
    similarity = details.get("similarity")
    floor = details.get("similarity_autoresolve_min")
    if similarity is None or floor is None:
        return False
    try:
        return Decimal(str(float(similarity))) < Decimal(str(float(floor)))
    except Exception:
        return False


def _is_inert_bank_debit(txn: Transaction) -> bool:
    return txn.source == "bank" and txn.direction == "debit"


def _load_golden_labels() -> dict[str, dict] | None:
    """Load close ground-truth labels from the golden dataset CSV.

    Returns ``None`` when the labels file is missing so production is never
    affected by the test fixture.
    """
    labels_path = _SAMPLE_DIR / _GOLDEN_LABELS_FILE
    if not labels_path.exists():
        return None
    labels: dict[str, dict] = {}
    with open(labels_path, encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
        for line in fh:
            if not line.strip():
                continue
            values = line.rstrip("\n").split(",")
            row = dict(zip(header, values))
            labels[row["record_id"]] = row
    return labels if labels else None


def _evaluate_against_ground_truth(
    decisions: list[dict], labels: dict[str, dict]
) -> dict:
    """Compare controller decisions against golden ground-truth labels.

    Returns a dict of metric values.  Only evaluated for records present
    in *both* the decisions list and the labels file so production data
    is never silently compared against stale labels.
    """
    correct = 0
    total = 0
    error_details: list[dict] = []
    matched_tp = matched_fp = matched_fn = 0
    no_match_tp = no_match_fp = no_match_fn = 0
    deferred_tp = deferred_fp = deferred_fn = 0

    for decision in decisions:
        ref = decision["record_ref"]
        if ref not in labels:
            continue
        total += 1
        expected = labels[ref]["expected_result"]
        actual = decision["decision"]

        if actual == expected:
            correct += 1
        else:
            error_details.append({"record_ref": ref, "expected": expected, "actual": actual})

        if expected == "matched":
            if actual == "matched":
                matched_tp += 1
            else:
                matched_fn += 1
        else:
            if actual == "matched":
                matched_fp += 1

        if expected == "no_match":
            if actual == "no_match":
                no_match_tp += 1
            else:
                no_match_fn += 1
        else:
            if actual == "no_match":
                no_match_fp += 1

        if expected == "deferred":
            if actual == "deferred":
                deferred_tp += 1
            else:
                deferred_fn += 1
        else:
            if actual == "deferred":
                deferred_fp += 1

    accuracy = correct / total if total > 0 else None
    matched_precision = matched_tp / (matched_tp + matched_fp) if (matched_tp + matched_fp) else None
    matched_recall = matched_tp / (matched_tp + matched_fn) if (matched_tp + matched_fn) else None
    matched_f1 = (
        2 * matched_precision * matched_recall / (matched_precision + matched_recall)
        if matched_precision is not None and matched_recall is not None and (matched_precision + matched_recall)
        else None
    )
    no_match_precision = no_match_tp / (no_match_tp + no_match_fp) if (no_match_tp + no_match_fp) else None
    no_match_recall = no_match_tp / (no_match_tp + no_match_fn) if (no_match_tp + no_match_fn) else None
    deferred_precision = deferred_tp / (deferred_tp + deferred_fp) if (deferred_tp + deferred_fp) else None
    deferred_recall = deferred_tp / (deferred_tp + deferred_fn) if (deferred_tp + deferred_fn) else None

    return {
        "decision_accuracy": round(accuracy, 4) if accuracy is not None else None,
        "matched_precision": round(matched_precision, 4) if matched_precision is not None else None,
        "matched_recall": round(matched_recall, 4) if matched_recall is not None else None,
        "matched_f1": round(matched_f1, 4) if matched_f1 is not None else None,
        "no_match_precision": round(no_match_precision, 4) if no_match_precision is not None else None,
        "no_match_recall": round(no_match_recall, 4) if no_match_recall is not None else None,
        "deferred_precision": round(deferred_precision, 4) if deferred_precision is not None else None,
        "deferred_recall": round(deferred_recall, 4) if deferred_recall is not None else None,
        "records_evaluated": total,
        "correct_predictions": correct,
        "total_errors": total - correct,
        "error_details": error_details,
    }


async def _load_scope(
    db: AsyncSession, transaction_ids: list[uuid.UUID] | None
) -> tuple[list[Transaction], dict[uuid.UUID, list[Match]], dict[uuid.UUID, ExceptionRecord], dict[uuid.UUID, dict]]:
    stmt = select(Transaction).order_by(Transaction.txn_date, Transaction.external_ref)
    if transaction_ids:
        stmt = stmt.where(Transaction.id.in_(transaction_ids))
    txns = list((await db.execute(stmt)).scalars().all())
    if not txns:
        return [], {}, {}, {}

    txn_ids = [t.id for t in txns]

    participant_rows = (
        await db.execute(
            select(MatchParticipant, Match)
            .join(Match, Match.id == MatchParticipant.match_id)
            .where(MatchParticipant.transaction_id.in_(txn_ids))
        )
    ).all()
    matches_by_txn: dict[uuid.UUID, list[Match]] = {t.id: [] for t in txns}
    for participant, match in participant_rows:
        matches_by_txn[participant.transaction_id].append(match)

    exception_rows = (
        await db.execute(
            select(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(txn_ids))
        )
    ).scalars().all()
    exceptions_by_txn: dict[uuid.UUID, ExceptionRecord] = {
        e.transaction_id: e for e in exception_rows
    }

    audit_rows = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "exception.opened",
                AuditLog.entity_type == "exception",
            )
        )
    ).scalars().all()
    opened_details: dict[uuid.UUID, dict] = {}
    for entry in audit_rows:
        if entry.details:
            opened_details[entry.entity_id] = entry.details

    return txns, matches_by_txn, exceptions_by_txn, opened_details


async def _participant_refs(
    db: AsyncSession, match_id: uuid.UUID
) -> tuple[list[Transaction]]:
    rows = (
        await db.execute(
            select(Transaction)
            .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
            .where(MatchParticipant.match_id == match_id)
        )
    ).scalars().all()
    return (list(rows),)


def _resolve_reason(
    txn: Transaction,
    proposed_matches: list[Match],
    open_exception: ExceptionRecord | None,
    opened_details: dict[uuid.UUID, dict],
    exceptions_by_txn: dict[uuid.UUID, ExceptionRecord],
    peers: list[Transaction],
) -> str:
    excluded = {"resolved", "dismissed"}
    if len(proposed_matches) > 1:
        return "conflicting_evidence"

    if open_exception is not None and open_exception.status not in excluded:
        details = opened_details.get(open_exception.id, {}) or {}
        return reason_code_for_exception(
            open_exception.exception_type,
            blocked_by_similarity=_blocked_by_similarity(details),
            narration_blank=_narration_blank(txn),
        )

    if proposed_matches:
        for peer in peers:
            if peer.id == txn.id:
                continue
            peer_exception = exceptions_by_txn.get(peer.id)
            if peer_exception is None or peer_exception.status in excluded:
                continue
            details = opened_details.get(peer_exception.id, {}) or {}
            return reason_code_for_exception(
                peer_exception.exception_type,
                blocked_by_similarity=_blocked_by_similarity(details),
                narration_blank=_narration_blank(peer),
            )
        return "manual_investigation"

    return "no_candidate"


async def close_reconciliation(
    db: AsyncSession,
    *,
    actor: str,
    transaction_ids: list[uuid.UUID] | None = None,
) -> dict:
    started = perf_counter()
    txns, matches_by_txn, exceptions_by_txn, opened_details = await _load_scope(
        db, transaction_ids
    )

    result: dict = {
        "records_scanned": len(txns),
        "matched": 0,
        "no_match": 0,
        "deferred": 0,
        "match_rate": 0.0,
        "deferred_by_reason": {code: 0 for code in REASON_CODES},
        "by_source": [],
        "decisions": [],
        "duration_ms": 0.0,
        "execution_time_seconds": 0.0,
        "throughput_records_per_second": 0.0,
        "accuracy_available": False,
    }
    if not txns:
        result["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        elapsed = perf_counter() - started
        result["execution_time_seconds"] = round(elapsed, 4)
        return result

    await record_audit(
        db,
        actor=actor,
        action="controller.close_started",
        entity_type="reconciliation",
        details={"records_scanned": len(txns), "scope_size": len(txns)},
    )

    per_source: dict[str, dict[str, int]] = {}
    for source in ("razorpay", "bank", "erp"):
        per_source[source] = {"total": 0, "matched": 0, "no_match": 0, "deferred": 0}

    for txn in txns:
        confirmed = [m for m in matches_by_txn.get(txn.id, []) if m.status == "confirmed"]
        proposed = [m for m in matches_by_txn.get(txn.id, []) if m.status == "proposed"]
        open_exception = exceptions_by_txn.get(txn.id)
        open_status = open_exception.status if open_exception else None
        unresolved = (
            open_status is not None and open_status not in ("resolved", "dismissed")
        )

        peers: list[Transaction] = []
        for match in proposed:
            member_txns, = await _participant_refs(db, match.id)
            peers.extend(t for t in member_txns if t.id != txn.id)

        per_source[txn.source]["total"] += 1

        if confirmed:
            matched_with: list[str] = []
            match_labels: list[str] = []
            for match in confirmed:
                member_txns, = await _participant_refs(db, match.id)
                matched_with.extend(t.external_ref for t in member_txns if t.id != txn.id)
                match_labels.append(
                    f"{match.match_type} (confidence {match.confidence_score})"
                )
            matched_with = sorted(set(matched_with))
            rationale = (
                f"member of confirmed match via {', '.join(match_labels)}; "
                f"linked to {', '.join(matched_with) or 'itself'}"
            )
            row = CloseResultRow(
                record_ref=txn.external_ref,
                source=txn.source,
                amount=txn.amount,
                decision=DECISION_MATCHED,
                matched_with=matched_with,
                reason_code=None,
                rationale=rationale,
                suggested_next_action=(
                    "No action required; record is matched and closed."
                ),
            )
            result["matched"] += 1
            per_source[txn.source]["matched"] += 1
            action = "controller.close_matched"
        elif (
            not proposed
            and not unresolved
            and _is_inert_bank_debit(txn)
        ):
            row = CloseResultRow(
                record_ref=txn.external_ref,
                source=txn.source,
                amount=txn.amount,
                decision=DECISION_NO_MATCH,
                matched_with=[],
                reason_code=None,
                rationale=(
                    "bank debit/charge with no proposed match and no open "
                    "exception — not a settlement inflow; excluded from "
                    "matching and closed as non-match"
                ),
                suggested_next_action=(
                    "No action required; classified as a non-settlement charge."
                ),
            )
            result["no_match"] += 1
            per_source[txn.source]["no_match"] += 1
            action = "controller.close_no_match"
        else:
            reason = _resolve_reason(
                txn, proposed, open_exception, opened_details, exceptions_by_txn, peers
            )
            result["deferred"] += 1
            per_source[txn.source]["deferred"] += 1
            result["deferred_by_reason"][reason] = (
                result["deferred_by_reason"].get(reason, 0) + 1
            )
            row = CloseResultRow(
                record_ref=txn.external_ref,
                source=txn.source,
                amount=txn.amount,
                decision=DECISION_DEFERRED,
                matched_with=[],
                reason_code=reason,
                rationale=(
                    f"deferred: {reason} — the reconciliation pipeline "
                    "produced no confirmable match; record stays open until a "
                    "human decision"
                ),
                suggested_next_action=_REASON_ACTIONS[reason],
            )
            action = "controller.close_deferred"

        result["decisions"].append(row.to_dict())

        await record_audit(
            db,
            actor=actor,
            action=action,
            entity_type="transaction",
            entity_id=txn.id,
            after_state={
                "record_ref": txn.external_ref,
                "decision": row.decision,
                "reason_code": row.reason_code,
                "matched_with": row.matched_with or None,
            },
            details={"rationale": row.rationale, "suggested_next_action": row.suggested_next_action},
        )

    if result["records_scanned"]:
        result["match_rate"] = round(
            result["matched"] / result["records_scanned"], 4
        )

    result["by_source"] = [
        {
            "source": source,
            "total_transactions": stats["total"],
            "matched": stats["matched"],
            "no_match": stats["no_match"],
            "deferred": stats["deferred"],
            "rate": round(stats["matched"] / stats["total"], 4) if stats["total"] else 0.0,
        }
        for source, stats in per_source.items()
        if stats["total"]
    ]

    elapsed = perf_counter() - started
    result["duration_ms"] = round(elapsed * 1000, 2)
    result["execution_time_seconds"] = round(elapsed, 4)
    result["throughput_records_per_second"] = (
        round(result["records_scanned"] / elapsed, 2) if elapsed > 0 else 0.0
    )

    golden_labels = _load_golden_labels()
    if golden_labels is not None:
        decision_refs = {d["record_ref"] for d in result["decisions"]}
        if decision_refs <= set(golden_labels.keys()):
            metrics = _evaluate_against_ground_truth(result["decisions"], golden_labels)
            result["accuracy_available"] = True
            result.update(metrics)
        else:
            result["accuracy_available"] = False

    await record_audit(
        db,
        actor=actor,
        action="controller.close_completed",
        entity_type="reconciliation",
        after_state={
            "records_scanned": result["records_scanned"],
            "matched": result["matched"],
            "no_match": result["no_match"],
            "deferred": result["deferred"],
            "deferred_by_reason": result["deferred_by_reason"],
            "accuracy_available": result.get("accuracy_available", False),
        },
    )
    return result