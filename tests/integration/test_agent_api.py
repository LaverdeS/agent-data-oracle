import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent_data_oracle.auth import LocalCaptureEmailProvider
from agent_data_oracle.schema import migrate_to_head
from agent_data_oracle.web import create_app
from tests.integration.test_evidence_queue import import_completed_fixture


@pytest_asyncio.fixture
async def evidence_database(postgres_url: str) -> AsyncEngine:
    migrate_to_head(postgres_url)
    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE auth_attempts, operators, cpsc_current_records, "
                "cpsc_revision_records, cpsc_source_observations, "
                "cpsc_recall_versions, cpsc_recalls, cpsc_source_revisions, "
                "cpsc_ingestion_runs CASCADE"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO audit_gate_state (singleton, committed_queue_count) "
                "VALUES (true, 0) ON CONFLICT (singleton) DO UPDATE "
                "SET committed_queue_count = 0"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO global_pause_state (singleton, is_paused) "
                "VALUES (true, false) ON CONFLICT (singleton) DO UPDATE "
                "SET is_paused = false, trigger_kind = NULL, "
                "reason_category = NULL, activated_at = NULL, activated_by = NULL, "
                "resolution_note = NULL, resolved_at = NULL, resolved_by = NULL"
            )
        )
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE cpsc_current_source_revision, cpsc_current_records, "
                    "cpsc_revision_records, cpsc_source_observations, "
                    "cpsc_recall_versions, cpsc_recalls, cpsc_source_revisions, "
                    "cpsc_ingestion_runs, operators CASCADE"
                )
            )
        await engine.dispose()


async def sign_in_and_declare(
    client: AsyncClient, email: LocalCaptureEmailProvider
) -> None:
    sign_in = await client.get("/sign-in")
    await client.post(
        "/auth/sign-in",
        data={
            "email": "operator@example.com",
            "csrf_token": sign_in.cookies["ado_csrf"],
        },
    )
    token = parse_qs(urlparse(email.deliveries[-1].sign_in_url).query)["token"][0]
    verify = await client.get(f"/auth/verify?token={token}")
    await client.post(
        "/auth/verify", data={"token": token, "csrf_token": verify.cookies["ado_csrf"]}
    )
    declaration = await client.get("/declare")
    await client.post(
        "/declare",
        data={
            "operator_type": "agent_operator",
            "sells_into_us": "yes",
            "csrf_token": declaration.cookies["ado_csrf"],
        },
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_agent_key_submits_and_reads_the_same_released_evidence_contract(
    postgres_url: str, evidence_database: object
) -> None:
    del evidence_database
    import_completed_fixture(postgres_url)
    email = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email,
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
        await sign_in_and_declare(operator, email)
        keys_page = await operator.get("/agent-keys")
        created_key = await operator.post(
            "/agent-keys",
            data={
                "scopes": ["queues:submit", "queues:read", "evidence:read"],
                "csrf_token": keys_page.cookies["ado_csrf"],
            },
        )
        secret = re.search(
            r'<code id="agent-key-secret">([^<]+)</code>', created_key.text
        ).group(1)
        headers = {"Authorization": f"Bearer {secret}", "Idempotency-Key": "a" * 24}
        created = await operator.post(
            "/api/v1/queues",
            headers=headers,
            json={"identifiers": [{"type": "upc", "literal": "000123456789"}]},
        )
        evaluation_id = created.json()["evaluation_id"]
        replayed = await operator.post(
            "/api/v1/queues",
            headers=headers,
            json={"identifiers": [{"type": "upc", "literal": "000123456789"}]},
        )
        conflicting_reuse = await operator.post(
            "/api/v1/queues",
            headers=headers,
            json={"identifiers": [{"type": "model", "literal": "OTHER-M2"}]},
        )
        retrieved = await operator.get(
            f"/api/v1/queues/{evaluation_id}",
            headers={"Authorization": f"Bearer {secret}"},
        )
        restricted_key_page = await operator.post(
            "/agent-keys",
            data={
                "scopes": ["queues:submit"],
                "csrf_token": created_key.cookies["ado_csrf"],
            },
        )
        restricted_match = re.search(
            r'<code id="agent-key-secret">([^<]+)</code>', restricted_key_page.text
        )
        assert restricted_match is not None
        scope_denied = await operator.get(
            f"/api/v1/queues/{evaluation_id}",
            headers={"Authorization": f"Bearer {restricted_match.group(1)}"},
        )
        key_id_match = re.search(r"/agent-keys/([^/]+)/revoke", created_key.text)
        assert key_id_match is not None
        revoked = await operator.post(
            f"/agent-keys/{key_id_match.group(1)}/revoke",
            data={"csrf_token": restricted_key_page.cookies["ado_csrf"]},
        )
        revoked_key_rejected = await operator.get(
            f"/api/v1/queues/{evaluation_id}",
            headers={"Authorization": f"Bearer {secret}"},
        )

    assert created_key.status_code == 200
    assert created.status_code == 201
    assert created.json()["contract_version"] == "v1"
    assert replayed.status_code == 201
    assert replayed.json()["evaluation_id"] == evaluation_id
    assert conflicting_reuse.status_code == 409
    assert conflicting_reuse.json()["code"] == "idempotency_conflict"
    assert retrieved.status_code == 200
    assert retrieved.json()["evidence"]["outcome"] == "no_candidates"
    assert (
        "No-candidate results do not prove"
        in retrieved.json()["evidence"]["limitations"][1]
    )
    assert scope_denied.status_code == 403
    assert scope_denied.headers["content-type"].startswith("application/problem+json")
    assert scope_denied.json()["code"] == "insufficient_scope"
    assert revoked.status_code == 303
    assert revoked_key_rejected.status_code == 401
