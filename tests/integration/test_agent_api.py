import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent_data_oracle.auth import LocalCaptureEmailProvider
from agent_data_oracle.cpsc_source import (
    CPSC_RECALL_API_URL,
    CpscRefreshMode,
    import_cpsc_refresh_response,
)
from agent_data_oracle.schema import migrate_to_head
from agent_data_oracle.web import create_app
from tests.integration.test_evidence_queue import import_completed_fixture
from tests.preview import founder_preview_access


@pytest_asyncio.fixture
async def evidence_database(postgres_url: str) -> AsyncEngine:
    migrate_to_head(postgres_url)
    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE sign_in_delivery_admissions, auth_attempts, operators, "
                "cpsc_current_records, "
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
    client: AsyncClient,
    email: LocalCaptureEmailProvider,
    operator_email: str = "operator@example.com",
) -> None:
    sign_in = await client.get("/sign-in")
    await client.post(
        "/auth/sign-in",
        data={
            "email": operator_email,
            "csrf_token": sign_in.cookies["ado_csrf"],
        },
    )
    token = parse_qs(urlparse(email.deliveries[-1].sign_in_url).query)["token"][0]
    verify = await client.get(f"/auth/verify?token={token}")
    await client.post(
        "/auth/verify", data={"token": token, "csrf_token": verify.cookies["ado_csrf"]}
    )
    declaration = await client.get("/declare")
    assert declaration.status_code == 200, declaration.headers.get("location")
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
async def test_preview_rejects_founder_agent_key_after_owner_leaves_allowlist(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    del evidence_database
    email = LocalCaptureEmailProvider()
    shared_arguments = {
        "database_url": postgres_url,
        "auth_secret": b"test-secret-that-is-long-enough",
        "email_provider": email,
        "clock": lambda: datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
        "public_origin": "https://test",
        "secure_cookies": True,
        "founder_emails": frozenset(
            {"founder@example.com", "replacement-founder@example.com"}
        ),
    }
    unrestricted_app = create_app(**shared_arguments)

    async with (
        unrestricted_app.router.lifespan_context(unrestricted_app),
        AsyncClient(
            transport=ASGITransport(app=unrestricted_app),
            base_url="https://test",
            follow_redirects=False,
        ) as client,
    ):
        await sign_in_and_declare(client, email, "founder@example.com")
        keys_page = await client.get("/agent-keys")
        created_key = await client.post(
            "/agent-keys",
            data={
                "scopes": ["queues:read"],
                "csrf_token": keys_page.cookies["ado_csrf"],
            },
        )
        secret_match = re.search(
            r'<code id="agent-key-secret">([^<]+)</code>', created_key.text
        )
        assert secret_match is not None
        secret = secret_match.group(1)

    preview_app = create_app(
        **shared_arguments,
        preview_access=founder_preview_access(
            "replacement-founder@example.com",
            founder_emails=frozenset(
                {"founder@example.com", "replacement-founder@example.com"}
            ),
        ),
    )
    async with (
        preview_app.router.lifespan_context(preview_app),
        AsyncClient(
            transport=ASGITransport(app=preview_app), base_url="https://test"
        ) as client,
    ):
        rejected = await client.get(
            "/api/v1/queues/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": f"Bearer {secret}"},
        )

    assert rejected.status_code == 401
    assert rejected.json()["code"] == "authentication_required"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_agent_key_submits_and_reads_the_same_released_evidence_contract(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
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
        retrieved_evidence = await operator.get(
            f"/api/v1/queues/{evaluation_id}/evidence",
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
        reporting_key_page = await operator.post(
            "/agent-keys",
            data={
                "scopes": ["reviews:report-agent"],
                "csrf_token": restricted_key_page.cookies["ado_csrf"],
            },
        )
        reporting_match = re.search(
            r'<code id="agent-key-secret">([^<]+)</code>', reporting_key_page.text
        )
        assert reporting_match is not None
        agent_reported = await operator.post(
            f"/api/v1/queues/{evaluation_id}/reviews",
            headers={"Authorization": f"Bearer {reporting_match.group(1)}"},
            json={"outcome": "not_sure"},
        )
        reviews_after_agent_report = await operator.get(
            f"/api/v1/queues/{evaluation_id}",
            headers={"Authorization": f"Bearer {secret}"},
        )
        held = await operator.post(
            "/api/v1/queues",
            headers={
                "Authorization": f"Bearer {secret}",
                "Idempotency-Key": "b" * 24,
            },
            json={"identifiers": [{"type": "model", "literal": "HANS0002"}]},
        )
        held_review = await operator.post(
            f"/api/v1/queues/{held.json()['evaluation_id']}/reviews",
            headers={"Authorization": f"Bearer {reporting_match.group(1)}"},
            json={"outcome": "not_sure"},
        )
        held_evidence = await operator.get(
            f"/api/v1/queues/{held.json()['evaluation_id']}/evidence",
            headers={"Authorization": f"Bearer {secret}"},
        )
        nonexistent_review = await operator.post(
            "/api/v1/queues/00000000-0000-0000-0000-000000000000/reviews",
            headers={"Authorization": f"Bearer {reporting_match.group(1)}"},
            json={"outcome": "not_sure"},
        )
        human_acknowledgement_api_attempt = await operator.post(
            f"/api/v1/queues/{evaluation_id}/acknowledgements",
            headers={"Authorization": f"Bearer {reporting_match.group(1)}"},
            json={"outcome": "not_sure"},
        )
        key_id_match = re.search(r"/agent-keys/([^/]+)/revoke", created_key.text)
        assert key_id_match is not None
        revoked = await operator.post(
            f"/agent-keys/{key_id_match.group(1)}/revoke",
            data={"csrf_token": reporting_key_page.cookies["ado_csrf"]},
        )
        revoked_key_rejected = await operator.get(
            f"/api/v1/queues/{evaluation_id}",
            headers={"Authorization": f"Bearer {secret}"},
        )

    async with evidence_database.connect() as connection:
        retrieval = (
            await connection.execute(
                text(
                    "SELECT agent_key_id, evidence_row_id FROM "
                    "source_evidence_retrievals"
                )
            )
        ).one()

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
    assert retrieved_evidence.status_code == 200
    assert retrieval[0] is not None
    assert retrieval[1] is None
    assert scope_denied.status_code == 403
    assert scope_denied.headers["content-type"].startswith("application/problem+json")
    assert scope_denied.json()["code"] == "insufficient_scope"
    assert agent_reported.status_code == 201
    assert agent_reported.json() == {
        "outcome": "not_sure",
        "report_type": "agent_review",
        "status": "recorded",
    }
    assert reviews_after_agent_report.json()["reviews"] == {
        "agent_review_reports": [
            {
                "outcome": "not_sure",
                "reported_at": "2026-09-04T10:00:00+00:00",
                "report_type": "agent_review",
            }
        ],
        "current_human_acknowledgement": None,
        "human_acknowledgement_history": [],
    }
    assert held.status_code == 202
    assert held_review.status_code == 404
    assert held_evidence.status_code == 404
    assert nonexistent_review.status_code == 404
    assert human_acknowledgement_api_attempt.status_code == 404
    assert revoked.status_code == 303
    assert revoked_key_rejected.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_agent_can_explicitly_refresh_a_queue_with_an_immutable_delta(
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
        await sign_in_and_declare(operator, email_provider)
        keys_page = await operator.get("/agent-keys")
        key_page = await operator.post(
            "/agent-keys",
            data={
                "scopes": ["queues:submit", "queues:read", "queues:refresh"],
                "csrf_token": keys_page.cookies["ado_csrf"],
            },
        )
        secret = re.search(r'<code id="agent-key-secret">([^<]+)</code>', key_page.text)
        assert secret is not None
        headers = {
            "Authorization": f"Bearer {secret.group(1)}",
            "Idempotency-Key": "r" * 24,
        }
        original = await operator.post(
            "/api/v1/queues",
            headers=headers,
            json={"identifiers": [{"type": "upc", "literal": "000123456789"}]},
        )
        original_id = original.json()["evaluation_id"]
        original_queue = await operator.get(f"/queues/{original_id}")
        acknowledged = await operator.post(
            f"/queues/{original_id}/acknowledgements",
            data={
                "outcome": "reviewed_same_product",
                "csrf_token": original_queue.cookies["ado_csrf"],
            },
        )

        changed_record = json.loads(
            Path("tests/fixtures/cpsc/recall-10887.json").read_text(encoding="utf-8")
        )
        changed_record[0]["ProductUPCs"] = ["000123456789"]
        await import_cpsc_refresh_response(
            database_url=postgres_url,
            raw_response=json.dumps(changed_record).encode(),
            observed_at=datetime(2026, 9, 4, 9, 0, tzinfo=UTC),
            expected_record_count=1,
            source_url=f"{CPSC_RECALL_API_URL}?format=json",
            refresh_mode=CpscRefreshMode.FULL,
            retrieval_attempts=1,
        )
        async with evidence_database.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE audit_gate_state SET committed_queue_count = 20 "
                    "WHERE singleton = true"
                )
            )
        refreshed = await operator.post(
            f"/api/v1/queues/{original_id}/refresh", headers=headers
        )
        replayed = await operator.post(
            f"/api/v1/queues/{original_id}/refresh", headers=headers
        )
        changed_match_record = json.loads(json.dumps(changed_record))
        changed_match_record[0]["ProductUPCs"] = [{"UPC": "000123456789"}]
        await import_cpsc_refresh_response(
            database_url=postgres_url,
            raw_response=json.dumps(changed_match_record).encode(),
            observed_at=datetime(2026, 9, 4, 9, 10, tzinfo=UTC),
            expected_record_count=1,
            source_url=f"{CPSC_RECALL_API_URL}?format=json",
            refresh_mode=CpscRefreshMode.FULL,
            retrieval_attempts=1,
        )
        changed = await operator.post(
            f"/api/v1/queues/{original_id}/refresh", headers=headers
        )
        removed_record = json.loads(json.dumps(changed_match_record))
        removed_record[0]["ProductUPCs"] = []
        await import_cpsc_refresh_response(
            database_url=postgres_url,
            raw_response=json.dumps(removed_record).encode(),
            observed_at=datetime(2026, 9, 4, 9, 20, tzinfo=UTC),
            expected_record_count=1,
            source_url=f"{CPSC_RECALL_API_URL}?format=json",
            refresh_mode=CpscRefreshMode.FULL,
            retrieval_attempts=1,
        )
        removed = await operator.post(
            f"/api/v1/queues/{original_id}/refresh", headers=headers
        )
        old_queue = await operator.get(f"/api/v1/queues/{original_id}", headers=headers)

    assert original.status_code == 201
    assert acknowledged.status_code == 303
    assert refreshed.status_code == 201
    assert refreshed.json()["evaluation_id"] != original_id
    assert refreshed.json()["evidence"]["outcome"] == "candidates"
    refresh = refreshed.json()["evidence"]["refresh"]
    assert refresh["predecessor"]["evaluation_id"] == original_id
    assert refresh["successor"]["evaluation_id"] == refreshed.json()["evaluation_id"]
    assert (
        refresh["predecessor"]["source_revision_id"]
        != refresh["successor"]["source_revision_id"]
    )
    assert (
        refresh["predecessor"]["normalization_version"]
        == refresh["successor"]["normalization_version"]
    )
    assert refresh["delta"]["added"] == [
        {
            "input_position": 0,
            "recall_number": "26651",
            "current": {
                "candidate_class": "exact_identifier_candidate",
                "constraints": {
                    "scope": "not_machine_parsed",
                    "source_literal": changed_record[0]["Description"],
                },
                "match_bases": [
                    {
                        "candidate_class": "exact_identifier_candidate",
                        "identity_limit": None,
                        "matched_field": "ProductUPCs[0]",
                        "matched_literal": "000123456789",
                    }
                ],
            },
        }
    ]
    assert refresh["delta"]["changed"] == []
    assert refresh["delta"]["removed"] == []
    assert replayed.status_code == 200
    assert replayed.json()["evaluation_id"] == refreshed.json()["evaluation_id"]
    assert changed.status_code == 201
    assert changed.json()["evidence"]["refresh"]["delta"]["changed"][0][
        "match_bases_changed"
    ]
    assert not changed.json()["evidence"]["refresh"]["delta"]["changed"][0][
        "constraints_changed"
    ]
    assert removed.status_code == 201
    assert removed.json()["evidence"]["outcome"] == "no_candidates"
    assert (
        removed.json()["evidence"]["refresh"]["delta"]["removed"][0]["recall_number"]
        == "26651"
    )
    assert old_queue.json()["evidence"]["outcome"] == "no_candidates"
    assert (
        old_queue.json()["evidence"]["refresh"]
        == refreshed.json()["evidence"]["refresh"]
    )
    assert refreshed.json()["reviews"]["current_human_acknowledgement"] is None
    assert old_queue.json()["reviews"]["current_human_acknowledgement"]["outcome"] == (
        "reviewed_same_product"
    )
