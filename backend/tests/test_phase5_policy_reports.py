"""Phase 5 regression tests: policy admin, reports, ERP webhook delivery.

The critical wiring proof: changing matching.semantic.similarity_threshold
through the ADMIN API (PATCH /api/v1/policy/{key}) must change what the
matcher produces on the NEXT reconciliation run — proving the policy engine
is live config, not cosmetic.
"""

import asyncio
import csv
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import AuditLog, ExceptionRecord, Match, MatchParticipant, PolicyConfig, Transaction
from app.services.reconciliation import run_reconciliation

SCOPE_REFS = [
    "pay_Hh8i9J0k", "INV-2026-0711", "UTR888170260811X",
    "pay_Sm1Th1cA", "INV-2026-0720",
    "pay_Pl4tFrmZ", "INV-2026-0721",
    "pay_Jj0k1L2m", "UTR888170260813",
]
SEMANTIC_KEY = "matching.semantic.similarity_threshold"
ACTOR = "ops-phase5"


def _client() -> TestClient:
    return TestClient(app)


async def _ref_ids():
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Transaction).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        missing = set(SCOPE_REFS) - {t.external_ref for t in rows}
        assert not missing, f"golden dataset not ingested: {sorted(missing)}"
        return {t.external_ref: t.id for t in rows}


async def _purge_scope():
    from sqlalchemy import delete

    async with SessionLocal() as db:
        txn_ids = (
            await db.execute(
                select(Transaction.id).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        match_ids = (
            await db.execute(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.transaction_id.in_(txn_ids)
                )
            )
        ).scalars().all()
        await db.execute(delete(ExceptionRecord).where(ExceptionRecord.transaction_id.in_(txn_ids)))
        await db.execute(delete(MatchParticipant).where(MatchParticipant.match_id.in_(match_ids)))
        await db.execute(delete(Match).where(Match.id.in_(match_ids)))
        await db.commit()

    async with SessionLocal() as db:
        txn_ids = (
            await db.execute(
                select(Transaction.id).where(Transaction.external_ref.in_(SCOPE_REFS))
            )
        ).scalars().all()
        leftover = (
            await db.execute(
                select(Match.id, Match.status, Match.match_type)
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .where(MatchParticipant.transaction_id.in_(txn_ids))
            )
        ).all()
        if leftover:
            print("\nPURGE LEFTOVER MATCHES:", leftover)
        assert not leftover, f"purge failed, matches remain: {leftover}"


async def _patch_threshold(client: TestClient, value) -> dict:
    response = client.patch(
        f"/api/v1/policy/{SEMANTIC_KEY}",
        headers={"X-Actor": ACTOR},
        json={"value": value},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _run_pipeline(ids):
    async with SessionLocal() as db:
        summary = await run_reconciliation(
            db, actor="pytest-phase5", transaction_ids=[ids[r] for r in SCOPE_REFS]
        )
        await db.commit()
        return summary


async def _proposals_containing(*refs):
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Match)
                .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                .join(Transaction, Transaction.id == MatchParticipant.transaction_id)
                .where(Match.status == "proposed", Transaction.external_ref.in_(refs))
            )
        ).scalars().unique().all()
        out = []
        for match in rows:
            members = (
                await db.execute(
                    select(Transaction.external_ref)
                    .join(MatchParticipant, MatchParticipant.transaction_id == Transaction.id)
                    .where(MatchParticipant.match_id == match.id)
                )
            ).scalars().all()
            if set(refs).issubset(set(members)):
                out.append(match)
        return out


async def _open_exception_type_for(ref: str) -> str | None:
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(ExceptionRecord.exception_type)
                .join(Transaction, Transaction.id == ExceptionRecord.transaction_id)
                .where(
                    Transaction.external_ref == ref,
                    ExceptionRecord.status.in_(["open", "in_review", "escalated"]),
                )
                .order_by(ExceptionRecord.opened_at.desc())
                .limit(1)
            )
        ).first()
        return row[0] if row else None


def test_policy_patch_validates_audits_and_rejects_unknown_keys():
    with _client() as client:
        # invalid range rejected, value untouched
        bad = client.patch(
            f"/api/v1/policy/{SEMANTIC_KEY}", headers={"X-Actor": ACTOR}, json={"value": 5}
        )
        assert bad.status_code == 400
        assert "between 0 and 1" in bad.json()["detail"]

        # wrong type rejected
        bad_type = client.patch(
            f"/api/v1/policy/{SEMANTIC_KEY}", headers={"X-Actor": ACTOR}, json={"value": "banana"}
        )
        assert bad_type.status_code == 400

        # unknown key rejected
        unknown = client.patch(
            "/api/v1/policy/matching.nonexistent.key", headers={"X-Actor": ACTOR}, json={"value": 1}
        )
        assert unknown.status_code == 400

        # read-only key (registered nowhere) rejected even if it existed in DB
        ro = client.patch(
            "/api/v1/policy/some.unregistered.key", headers={"X-Actor": ACTOR}, json={"value": 1}
        )
        assert ro.status_code == 400

        # valid change persists + audits
        async def current_value():
            async with SessionLocal() as db:
                row = await db.get(PolicyConfig, SEMANTIC_KEY)
                return float(row.value)

        before = asyncio.run(current_value())
        new_value = 0.42 if before != 0.42 else 0.43
        patched = client.patch(
            f"/api/v1/policy/{SEMANTIC_KEY}", headers={"X-Actor": ACTOR}, json={"value": new_value}
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["changed"] is True and body["before"] == before and body["value"] == new_value

        async def audit_rows(action):
            async with SessionLocal() as db:
                rows = (
                    await db.execute(
                        select(AuditLog)
                        .where(AuditLog.action == action)
                        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                        .limit(5)
                    )
                ).scalars().all()
                return rows

        entries = asyncio.run(audit_rows("policy.updated"))
        assert entries, "policy.updated audit row missing"
        latest = entries[0]
        assert latest.actor == ACTOR
        assert latest.before_state["key"] == SEMANTIC_KEY
        assert latest.after_state["value"] == new_value

        # history endpoint surfaces the change for this key specifically
        history = client.get(f"/api/v1/policy/{SEMANTIC_KEY}/history").json()
        assert len(history) >= 1
        assert all(h["details"]["key"] == SEMANTIC_KEY for h in history)

        # no-op save reports changed=false and writes nothing
        noop = client.patch(
            f"/api/v1/policy/{SEMANTIC_KEY}", headers={"X-Actor": ACTOR}, json={"value": new_value}
        )
        assert noop.json()["changed"] is False

        # policy list shows grouped metadata + last-changed attribution
        listing = client.get("/api/v1/policy").json()
        groups = {f["group"] for f in listing}
        assert {"Matching thresholds", "Auto-resolve gates", "Materiality rules"}.issubset(groups)
        field = next(f for f in listing if f["key"] == SEMANTIC_KEY)
        assert field["editable"] is True
        assert field["last_changed"]["actor"] == ACTOR


def test_admin_threshold_change_changes_matcher_behavior_next_run():
    """The Phase 5 wiring proof: admin API -> policy_config -> matcher output."""

    async def scenario():
        ids = await _ref_ids()
        async with SessionLocal() as db:
            original = float((await db.get(PolicyConfig, SEMANTIC_KEY)).value)

        # Start from a pristine semantic index: the persisted cache may hold
        # vectors from arbitrary earlier runs/processes; retrieval must depend
        # on policy, not on index history.
        import os

        from app.core.config import get_settings

        index_path = get_settings().semantic_index_path
        for stale in (index_path, f"{index_path}.meta.json"):
            if os.path.exists(stale):
                os.remove(stale)

        try:
            await _purge_scope()
            with _client() as client:
                await _patch_threshold(client, 0.99)

            await _run_pipeline(ids)

            blocked = await _proposals_containing("pay_Pl4tFrmZ", "INV-2026-0721")
            assert not blocked, "semantic proposal must NOT exist at threshold 0.99"
            exc_type = await _open_exception_type_for("pay_Pl4tFrmZ")
            assert exc_type == "unmatched", (
                f"with semantics blocked, Pl4tFrmZ should be plain unmatched, got {exc_type}"
            )

            with _client() as client:
                await _patch_threshold(client, 0.10)

            await _run_pipeline(ids)

            allowed = await _proposals_containing("pay_Pl4tFrmZ", "INV-2026-0721")
            assert allowed, "semantic proposal must reappear at threshold 0.10"
            exc_type = await _open_exception_type_for("pay_Pl4tFrmZ")
            assert exc_type == "manual_review_required", (
                f"expected AI needs_human exception after semantic candidate returns, got {exc_type}"
            )
        finally:
            async with SessionLocal() as db:
                row = await db.get(PolicyConfig, SEMANTIC_KEY)
                row.value = original
                await db.commit()

    asyncio.run(scenario())


def test_reports_csv_pdf_summary_with_date_filtering():
    with _client() as client:
        csv_response = client.get(
            "/api/v1/reports/export.csv",
            headers={"X-Actor": ACTOR},
        )
        assert csv_response.status_code == 200
        assert csv_response.headers["content-type"].startswith("text/csv")
        rows = list(csv.reader(io.StringIO(csv_response.text)))
        assert rows[0][0] == "exception_id"
        assert len(rows) >= 3, "expected exception ledger rows"
        joined = "\n".join(",".join(r) for r in rows)
        assert "manual_review_required" in joined or "unmatched" in joined

        future_csv = client.get(
            "/api/v1/reports/export.csv",
            headers={"X-Actor": ACTOR},
            params={"from": "9999-01-01"},
        )
        future_rows = list(csv.reader(io.StringIO(future_csv.text)))
        assert len(future_rows) == 1, "date filter must exclude all existing exceptions"

        pdf_response = client.get("/api/v1/reports/export.pdf", headers={"X-Actor": ACTOR})
        assert pdf_response.status_code == 200
        assert pdf_response.headers["content-type"] == "application/pdf"
        assert pdf_response.content[:5] == b"%PDF-"
        assert len(pdf_response.content) > 1500

        summary = client.get("/api/v1/reports/summary", headers={"X-Actor": ACTOR}).json()
        for key in (
            "matches",
            "exceptions",
            "match_rate_by_source",
            "auto_resolved_total",
            "human_resolved_total",
            "open_exception_aging",
        ):
            assert key in summary, f"summary missing {key}"
        sources = {r["source"]: r for r in summary["match_rate_by_source"]}
        assert set(sources) == {"razorpay", "bank", "erp"}

        async def generation_audits():
            async with SessionLocal() as db:
                rows = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.action == "report.generated",
                            AuditLog.actor == ACTOR,
                        )
                    )
                ).scalars().all()
                formats = {row.details["format"] for row in rows}
                return formats

        formats = asyncio.run(generation_audits())
        assert {"csv", "pdf", "summary"}.issubset(formats), (
            "every report generation must be audited"
        )


def test_webhook_push_delivers_and_failures_are_audited():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import httpx as real_httpx

    from app.services.webhook import WebhookDeliveryError, push_resolved

    captured: list[dict] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            captured.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"received": true}')

        def log_message(self, *args):  # silence
            return

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    try:
        # Ensure at least one RESOLVED exception exists so the ERP payload
        # carries both kinds: close an unmatched exception via the review API.
        async def arrange_closed_exception():
            from app.services.reconciliation import run_reconciliation as _run

            ids = await _ref_ids()
            await _purge_scope()
            async with SessionLocal() as db:
                await _run(db, actor="pytest-phase5", transaction_ids=list(ids.values()))
                await db.commit()
                row = (
                    await db.execute(
                        select(ExceptionRecord.id)
                        .join(Transaction, Transaction.id == ExceptionRecord.transaction_id)
                        .where(
                            Transaction.external_ref == "pay_Jj0k1L2m",
                            ExceptionRecord.status == "open",
                            ExceptionRecord.exception_type == "unmatched",
                        )
                        .limit(1)
                    )
                ).first()
                assert row, "expected an open unmatched exception for pay_Jj0k1L2m"
                return str(row[0])

        jj0_exception_id = asyncio.run(arrange_closed_exception())
        with _client() as client:
            dismissed = client.post(
                f"/api/v1/review/exceptions/{jj0_exception_id}/dismiss",
                headers={"X-Actor": ACTOR},
                json={"note": "phase5 webhook fixture"},
            )
            assert dismissed.status_code == 200

        with _client() as client:
            # 1. Endpoint-level happy path against a REAL local socket.
            ok = client.post(
                "/api/v1/integrations/erp/push",
                headers={"X-Actor": ACTOR},
                json={"url": f"http://127.0.0.1:{port}/hook"},
            )
            assert ok.status_code == 200, ok.text
            body = ok.json()
            assert body["ok"] is True and body["attempts"] == 1
            assert body["pushed_items"] >= 1

            assert len(captured) == 1
            payload = captured[0]
            assert payload["event"] == "reconciliation.results"
            kinds = {item["kind"] for item in payload["items"]}
            assert "match" in kinds and "exception" in kinds
            match_items = [i for i in payload["items"] if i["kind"] == "match"]
            sample = match_items[0]
            assert sample["resolution_type"] in ("auto", "human")
            assert isinstance(sample.get("transactions"), list)

            async def webhook_audits(action):
                async with SessionLocal() as db:
                    rows = (
                        await db.execute(
                            select(AuditLog)
                            .where(AuditLog.action == action, AuditLog.actor == ACTOR)
                            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                        )
                    ).scalars().all()
                    return rows

            delivered = asyncio.run(webhook_audits("webhook.delivered"))
            assert delivered, "webhook.delivered audit row missing"
            assert delivered[0].after_state["status_code"] == 200

            # 2. Retry-then-success via injected transport (no sockets).
            attempts_seen = []

            def flaky_handler(request: real_httpx.Request) -> real_httpx.Response:
                attempts_seen.append(1)
                if len(attempts_seen) < 3:
                    raise real_httpx.ConnectError("transient outage", request=request)
                return real_httpx.Response(200, json={"received": True})

            async def retry_scenario():
                async with SessionLocal() as db:
                    result = await push_resolved(
                        db,
                        actor=ACTOR,
                        url="http://mock/flaky",
                        max_attempts=5,
                        backoff_base_s=0.01,
                        transport=real_httpx.MockTransport(flaky_handler),
                    )
                    await db.commit()
                    return result

            retry_result = asyncio.run(retry_scenario())
            assert retry_result["attempts"] == 3, "must retry twice then succeed"
            assert len(attempts_seen) == 3

            # 3. Permanent failure: exhausts retries, raises, audited as failed.
            def dead_handler(request: real_httpx.Request) -> real_httpx.Response:
                raise real_httpx.ConnectError("connection refused", request=request)

            async def fail_scenario():
                async with SessionLocal() as db:
                    try:
                        await push_resolved(
                            db,
                            actor=ACTOR,
                            url="http://mock/dead",
                            max_attempts=3,
                            backoff_base_s=0.01,
                            transport=real_httpx.MockTransport(dead_handler),
                        )
                    except WebhookDeliveryError:
                        pass
                    else:
                        raise AssertionError("expected WebhookDeliveryError")
                    await db.commit()

            asyncio.run(fail_scenario())

            # 4. Delivery ledger shows both outcomes.
            deliveries = client.get("/api/v1/integrations/erp/deliveries").json()
            actions = [d["action"] for d in deliveries]
            assert "webhook.delivered" in actions, actions
            assert "webhook.failed" in actions, actions
            failed_entry = next(
                d
                for d in deliveries
                if d["action"] == "webhook.failed" and d["details"]["url"].endswith("/dead")
            )
            assert failed_entry["details"]["attempts"] == 3
            assert failed_entry["details"]["errors"], "failure entry must carry error detail"
    finally:
        server.shutdown()
