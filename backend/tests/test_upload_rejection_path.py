import asyncio
import uuid

import asyncpg
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


def _pg_dsn() -> str:
    return get_settings().database_url.replace("+asyncpg", "")


async def _cleanup(prefix: str, filename: str) -> None:
    conn = await asyncpg.connect(dsn=_pg_dsn())
    try:
        await conn.execute(
            "DELETE FROM transactions WHERE external_ref LIKE $1", f"{prefix}%"
        )
        await conn.execute("DELETE FROM ingestions WHERE filename = $1", filename)
    finally:
        await conn.close()


def _refs_from_api(client: TestClient, prefix: str) -> set[str]:
    payload = client.get("/api/v1/transactions", params={"limit": 500}).json()
    return {
        item["external_ref"]
        for item in payload["items"]
        if item["external_ref"].startswith(prefix)
    }


def _build_mixed_validity_csv(prefix: str) -> tuple[bytes, str]:
    filename = f"malformed-{prefix}.csv"
    header = (
        "settlement_id,settlement_date,payment_id,order_id,customer_ref,"
        "amount,fee,tax,credit_amount,status,method\n"
    )
    rows = [
        f"setl_MX9001,2026-08-20,{prefix}A,,,1000.00,3.00,0.54,996.46,processed,\n",
        f"setl_MX9001,NOT-A-DATE,{prefix}B,,,2000.00,6.00,1.08,1992.92,processed,\n",
        f"setl_MX9001,2026-08-21,{prefix}C,,,3000.00,9.00,1.62,2989.38,processed,\n",
        f"setl_MX9001,2026-08-22,{prefix}D,,,4000.00,12.00,2.16,3985.84,processed,\n",
    ]
    return (header + "".join(rows)).encode(), filename


class TestPerRowRejectionPath:
    def test_three_valid_rows_persist_while_bad_row_is_rejected_with_reason(self):
        prefix = "TST" + uuid.uuid4().hex[:10].upper()
        content, filename = _build_mixed_validity_csv(prefix)

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/uploads",
                data={"source": "razorpay"},
                files={"file": (filename, content, "text/csv")},
            )
            persisted_refs = _refs_from_api(client, prefix)

        try:
            assert response.status_code == 201
            body = response.json()

            assert body["status"] == "completed", (
                "mixed-validity file must not be reported as a full failure"
            )
            assert body["rows_total"] == 4
            assert body["rows_inserted"] == 3, (
                "the three valid rows must be ingested despite one bad row"
            )
            assert body["rows_skipped_duplicate"] == 0
            assert body["rows_rejected"] == 1

            assert len(body["rejections_preview"]) == 1
            rejection = body["rejections_preview"][0]
            assert "row 3" in rejection
            assert "unparseable date" in rejection

            assert persisted_refs == {f"{prefix}A", f"{prefix}C", f"{prefix}D"}, (
                "valid rows must reach the database; the bad row must not"
            )
        finally:
            asyncio.run(_cleanup(prefix, filename))

    def test_fully_invalid_file_still_returns_row_level_details_not_crash(self):
        prefix = "TST" + uuid.uuid4().hex[:10].upper()
        csv_bytes = (
            "settlement_id,settlement_date,payment_id,order_id,customer_ref,"
            "amount,fee,tax,credit_amount,status,method\n"
            f"setl_MX9002,GARBAGE,{prefix}X,,,1.00,0.00,0.00,1.00,processed,\n"
        ).encode()
        filename = f"allbad-{prefix}.csv"

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/uploads",
                data={"source": "razorpay"},
                files={"file": (filename, csv_bytes, "text/csv")},
            )
            persisted_refs = _refs_from_api(client, prefix)

        try:
            assert response.status_code == 201
            body = response.json()

            assert body["status"] == "completed"
            assert body["rows_total"] == 1
            assert body["rows_inserted"] == 0
            assert body["rows_rejected"] == 1
            assert "unparseable date" in body["rejections_preview"][0]
            assert not persisted_refs
        finally:
            asyncio.run(_cleanup(prefix, filename))
