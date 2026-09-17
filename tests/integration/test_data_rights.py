import json
import re
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_data_oracle.auth import LocalCaptureEmailProvider
from agent_data_oracle.web import create_app
from tests.integration.test_evidence_queue import (
    import_completed_fixture,
    sign_in_and_declare,
)

pytest_plugins = ("tests.integration.test_evidence_queue",)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_operator_can_export_then_request_deletion_with_immediate_revocation(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    import_completed_fixture(postgres_url)
    email_provider = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email_provider,
        clock=lambda: datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
        public_origin="https://test",
        secure_cookies=True,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as operator,
    ):
        await sign_in_and_declare(operator, email_provider, "operator@example.com")
        keys_page = await operator.get("/agent-keys")
        created_key = await operator.post(
            "/agent-keys",
            data={
                "scopes": ["queues:submit", "queues:read"],
                "csrf_token": keys_page.cookies["ado_csrf"],
            },
        )
        secret_match = re.search(
            r'<code id="agent-key-secret">([^<]+)</code>', created_key.text
        )
        assert secret_match is not None
        created_queue = await operator.post(
            "/api/v1/queues",
            headers={
                "Authorization": f"Bearer {secret_match.group(1)}",
                "Idempotency-Key": "d" * 24,
            },
            json={"identifiers": [{"type": "upc", "literal": "000123456789"}]},
        )
        queue_id = created_queue.json()["evaluation_id"]
        queue = await operator.get(f"/queues/{queue_id}")
        acknowledgement = await operator.post(
            f"/queues/{queue_id}/acknowledgements",
            data={
                "outcome": "reviewed_same_product",
                "csrf_token": queue.cookies["ado_csrf"],
            },
        )

        exported = await operator.get("/account/data/export")
        exported_body = exported.json()
        deletion = await operator.post(
            "/account/data/deletion-requests",
            data={"csrf_token": operator.cookies["ado_csrf"]},
        )
        after_deletion = await operator.get("/app")
        revoked_key = await operator.get(
            f"/api/v1/queues/{queue_id}",
            headers={"Authorization": f"Bearer {secret_match.group(1)}"},
        )

    async with evidence_database.connect() as connection:
        operator_id = await connection.scalar(text("SELECT operator_id FROM operators"))
        source_count = await connection.scalar(
            text("SELECT count(*) FROM cpsc_recalls")
        )
        work = (
            await connection.execute(
                text(
                    "SELECT status, categories FROM operator_deletion_work "
                    "WHERE operator_id = :operator_id"
                ),
                {"operator_id": operator_id},
            )
        ).one()

    assert created_queue.status_code == 201
    assert acknowledgement.status_code == 303
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/json")
    assert exported_body["account"] == {
        "email": "operator@example.com",
        "operator_type": "business_operator",
        "sells_into_us": True,
    }
    assert exported_body["submissions"][0]["evaluation_id"] == queue_id
    assert exported_body["delegated_keys"][0]["secret_prefix"]
    assert "secret_hash" not in json.dumps(exported_body)
    assert deletion.status_code == 202
    assert deletion.json()["status"] == "pending"
    assert after_deletion.status_code == 303
    assert after_deletion.headers["location"] == "/sign-in"
    assert revoked_key.status_code == 401
    assert work[0] == "pending"
    assert work[1] == [
        "account_and_declaration",
        "acknowledgements",
        "delegated_key_metadata",
        "released_evaluations",
        "review_reports",
        "submissions",
    ]
    assert source_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cleanup_pauses_overdue_held_deletion(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    del evidence_database
    started_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    email_provider = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email_provider,
        clock=lambda: started_at,
        public_origin="https://test",
        secure_cookies=True,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as operator,
    ):
        await sign_in_and_declare(operator, email_provider, "operator@example.com")
        await operator.get("/account/data/export")
        requested = await operator.post(
            "/account/data/deletion-requests",
            data={"csrf_token": operator.cookies["ado_csrf"]},
        )

    async with app.state.database.connection() as connection:
        operator_id = await connection.scalar(text("SELECT operator_id FROM operators"))
    assert operator_id is not None
    assert await app.state.data_rights.place_incident_hold(
        operator_id=operator_id,
        reason_category="security_incident",
        recorded_at=started_at,
    )

    overdue = await app.state.data_rights.cleanup(
        now=datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    )
    async with app.state.database.connection() as connection:
        still_present = await connection.scalar(
            text("SELECT count(*) FROM operators WHERE operator_id = :operator_id"),
            {"operator_id": operator_id},
        )
        paused = await connection.scalar(
            text("SELECT is_paused FROM global_pause_state WHERE singleton = true")
        )
        receipt_count = await connection.scalar(
            text("SELECT count(*) FROM deletion_completion_receipts")
        )

    assert requested.status_code == 202
    assert overdue.completed_deletions == 0
    assert still_present == 1
    assert paused is True
    assert receipt_count >= 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cleanup_expires_unheld_founder_test_data_at_thirty_day_boundary(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    del evidence_database
    started_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    email_provider = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email_provider,
        clock=lambda: started_at,
        public_origin="https://test",
        secure_cookies=True,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as operator,
    ):
        await sign_in_and_declare(operator, email_provider, "operator@example.com")

    result = await app.state.data_rights.cleanup(
        now=datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    )
    async with app.state.database.connection() as connection:
        operators = await connection.scalar(text("SELECT count(*) FROM operators"))
        receipt = (
            await connection.execute(
                text(
                    "SELECT categories, completed_at, outcome FROM "
                    "deletion_completion_receipts ORDER BY completed_at DESC LIMIT 1"
                )
            )
        ).one()

    assert result.expired_operators == 1
    assert operators == 0
    assert receipt[0] == [
        "account_and_declaration",
        "acknowledgements",
        "delegated_key_metadata",
        "released_evaluations",
        "review_reports",
        "submissions",
    ]
    assert receipt[1] == datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    assert receipt[2] == "completed"
