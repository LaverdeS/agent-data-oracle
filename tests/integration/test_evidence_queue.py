import asyncio
import base64
import hashlib
import hmac
import re
import struct
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent_data_oracle.auth import LocalCaptureEmailProvider
from agent_data_oracle.web import create_app


@pytest_asyncio.fixture
async def evidence_database(postgres_url: str) -> AsyncEngine:
    migration = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_data_oracle",
            "migrate",
            "--database-url",
            postgres_url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert migration.returncode == 0, migration.stderr
    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE auth_attempts, operators, cpsc_current_records, "
                "cpsc_revision_records, "
                "cpsc_source_observations, cpsc_recall_versions, cpsc_recalls, "
                "cpsc_source_revisions, cpsc_ingestion_runs CASCADE"
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
        await engine.dispose()


def import_completed_fixture(postgres_url: str) -> None:
    imported = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_data_oracle",
            "job",
            "cpsc-import-fixture",
            "--database-url",
            postgres_url,
            "--fixture",
            str(Path("tests/fixtures/cpsc/recall-10887.json")),
            "--observed-at",
            "2026-09-04T00:00:00Z",
            "--expected-record-count",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr


async def sign_in_and_declare(
    client: AsyncClient, email_provider: LocalCaptureEmailProvider, email: str
) -> None:
    sign_in = await client.get("/sign-in")
    await client.post(
        "/auth/sign-in",
        data={"email": email, "csrf_token": sign_in.cookies["ado_csrf"]},
    )
    token = parse_qs(urlparse(email_provider.deliveries[-1].sign_in_url).query)[
        "token"
    ][0]
    verification = await client.get(f"/auth/verify?token={token}")
    await client.post(
        "/auth/verify",
        data={"token": token, "csrf_token": verification.cookies["ado_csrf"]},
    )
    declaration = await client.get("/declare")
    await client.post(
        "/declare",
        data={
            "operator_type": "business_operator",
            "sells_into_us": "yes",
            "csrf_token": declaration.cookies["ado_csrf"],
        },
    )


def totp_code(secret: str, instant: datetime) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(instant.timestamp()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_operator_can_create_reopen_and_isolate_a_no_candidate_queue(
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
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as another_operator,
    ):
        await sign_in_and_declare(operator, email_provider, "operator@example.com")
        submission = await operator.get("/queues/new")
        submitted = await operator.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "upc"),
                    ("identifier_value", "000123456789"),
                    ("identifier_type", "model"),
                    ("identifier_value", "Model ZX-9"),
                    ("identifier_type", "upc"),
                    ("identifier_value", "000123456789"),
                    ("authorization", "authorized"),
                    ("idempotency_key", submission.headers["x-idempotency-key"]),
                    ("csrf_token", submission.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        queue = await operator.get(submitted.headers["location"])
        reopened = await operator.get(submitted.headers["location"])

        await sign_in_and_declare(another_operator, email_provider, "other@example.com")
        forbidden_queue = await another_operator.get(submitted.headers["location"])
        forbidden_acknowledgement = await another_operator.post(
            f"{submitted.headers['location']}/acknowledgements",
            data={
                "outcome": "not_sure",
                "csrf_token": another_operator.cookies["ado_csrf"],
            },
        )

    assert submitted.status_code == 303
    assert "No candidate recall-to-listing action records" in queue.text
    assert "000123456789" in queue.text
    assert queue.text.count("000123456789") == 2
    assert "Model ZX-9" in queue.text
    assert "do not prove that the CPSC source is complete" in queue.text
    assert "CPSC/U.S. consumer-product recall data only" in queue.text
    assert queue.text == reopened.text
    assert forbidden_queue.status_code == 404
    assert forbidden_acknowledgement.status_code == 404
    with pytest.raises(DBAPIError):
        async with evidence_database.begin() as connection:
            await connection.execute(
                text("UPDATE evidence_evaluations SET matcher_version = 'changed'")
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_operator_sees_only_pending_status_for_held_candidate_evidence(
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
        ) as client,
    ):
        await sign_in_and_declare(client, email_provider, "operator@example.com")
        form = await client.get("/queues/new")
        submitted = await client.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "HANS0002"),
                    ("identifier_type", "brand"),
                    ("identifier_value", "HARPPA"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        queue = await client.get(submitted.headers["location"])

    assert submitted.status_code == 303
    assert "Founder review pending" in queue.text
    assert "HANS0002" not in queue.text
    assert "26651" not in queue.text
    assert "Brand equality alone is insufficient identity" not in queue.text
    async with evidence_database.connect() as connection:
        evidence_rows = await connection.scalar(
            text("SELECT count(*) FROM evidence_rows")
        )
    assert evidence_rows == 2
    with pytest.raises(DBAPIError):
        async with evidence_database.begin() as connection:
            await connection.execute(
                text("UPDATE evidence_rows SET recall_number = 'changed'")
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_candidate_queue_is_held_until_a_totp_verified_founder_approves_it(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    import_completed_fixture(postgres_url)
    now = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    email_provider = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email_provider,
        clock=lambda: now,
        public_origin="https://test",
        secure_cookies=True,
        founder_emails=frozenset({"founder@example.com"}),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as operator,
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as founder,
    ):
        await sign_in_and_declare(operator, email_provider, "operator@example.com")
        form = await operator.get("/queues/new")
        submitted = await operator.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "HANS0002"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        pending = await operator.get(submitted.headers["location"])
        assert pending.status_code == 200
        assert "Founder review pending" in pending.text
        assert "HANS0002" not in pending.text
        assert "26651" not in pending.text
        evaluation_id = submitted.headers["location"].rsplit("/", maxsplit=1)[-1]
        operator_audit_attempt = await operator.get(f"/founder/audits/{evaluation_id}")

        await sign_in_and_declare(founder, email_provider, "founder@example.com")
        unverified_founder_audit_attempt = await founder.get(
            f"/founder/audits/{evaluation_id}"
        )
        enrollment = await founder.get("/founder/totp/enroll")
        secret = enrollment.text.split('data-totp-secret="')[1].split('"')[0]
        enrolled = await founder.post(
            "/founder/totp/enroll",
            data={
                "code": totp_code(secret, now),
                "csrf_token": enrollment.cookies["ado_csrf"],
            },
        )
        assert enrolled.status_code == 200
        audit = await founder.get(f"/founder/audits/{evaluation_id}")
        csrf_rejected_approval = await founder.post(
            f"/founder/audits/{evaluation_id}/approve"
        )
        approved = await founder.post(
            f"/founder/audits/{evaluation_id}/approve",
            data={"csrf_token": audit.cookies["ado_csrf"]},
        )
        released = await operator.get(submitted.headers["location"])
        founder_controls = await founder.get("/founder")
        manually_paused = await founder.post(
            "/founder/pause",
            data={
                "reason_category": "scope_ambiguity",
                "csrf_token": founder_controls.cookies["ado_csrf"],
            },
        )
        manual_pause_form = await operator.get("/queues/new")
        manually_blocked = await operator.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "upc"),
                    ("identifier_value", "000123456789"),
                    ("authorization", "authorized"),
                    (
                        "idempotency_key",
                        manual_pause_form.headers["x-idempotency-key"],
                    ),
                    ("csrf_token", manual_pause_form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        paused_controls = await founder.get("/founder")
        resolved_manual_pause = await founder.post(
            "/founder/pause/resolve",
            data={
                "resolution_note": "Audit procedure confirmed.",
                "csrf_token": paused_controls.cookies["ado_csrf"],
            },
        )
        second_form = await operator.get("/queues/new")
        second_submission = await operator.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "HANS0002"),
                    ("authorization", "authorized"),
                    ("idempotency_key", second_form.headers["x-idempotency-key"]),
                    ("csrf_token", second_form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        second_evaluation_id = second_submission.headers["location"].rsplit(
            "/", maxsplit=1
        )[-1]
        rejection_page = await founder.get(f"/founder/audits/{second_evaluation_id}")
        non_founder_pause_attempt = await operator.post(
            "/founder/pause",
            data={"csrf_token": operator.cookies["ado_csrf"]},
        )
        rejected, duplicate_rejection = await asyncio.gather(
            founder.post(
                f"/founder/audits/{second_evaluation_id}/reject",
                data={
                    "reason_category": "false_exact_candidate",
                    "csrf_token": rejection_page.cookies["ado_csrf"],
                },
            ),
            founder.post(
                f"/founder/audits/{second_evaluation_id}/reject",
                data={
                    "reason_category": "false_exact_candidate",
                    "csrf_token": rejection_page.cookies["ado_csrf"],
                },
            ),
        )
        rejection_form = await operator.get("/queues/new")
        rejected_acknowledgement = await operator.post(
            f"/queues/{second_evaluation_id}/acknowledgements",
            data={
                "outcome": "not_sure",
                "csrf_token": rejection_form.cookies["ado_csrf"],
            },
        )
        blocked_form = await operator.get("/queues/new")
        blocked_submission = await operator.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "upc"),
                    ("identifier_value", "000123456789"),
                    ("authorization", "authorized"),
                    ("idempotency_key", blocked_form.headers["x-idempotency-key"]),
                    ("csrf_token", blocked_form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        historical = await operator.get(submitted.headers["location"])

    assert submitted.status_code == 303
    assert operator_audit_attempt.status_code == 404
    assert unverified_founder_audit_attempt.status_code == 404
    assert audit.status_code == 200
    assert "HANS0002" in audit.text
    assert csrf_rejected_approval.status_code == 403
    assert approved.status_code == 303
    assert "Possible recall-to-listing action records" in released.text
    assert manually_paused.status_code == 303
    assert manually_blocked.status_code == 503
    assert resolved_manual_pause.status_code == 303
    assert non_founder_pause_attempt.status_code == 404
    assert rejected.status_code == 303
    assert duplicate_rejection.status_code == 404
    assert rejected_acknowledgement.status_code == 404
    assert blocked_submission.status_code == 503
    assert "New evidence queues are paused." in blocked_submission.text
    assert "Possible recall-to-listing action records" in historical.text
    async with evidence_database.connect() as connection:
        audit_rows = await connection.execute(
            text(
                "SELECT decision, reason_category FROM evaluation_audits "
                "ORDER BY audited_at"
            )
        )
        pause = await connection.execute(
            text("SELECT is_paused, reason_category FROM global_pause_state")
        )
    assert audit_rows.all() == [
        ("approved", None),
        ("rejected", "false_exact_candidate"),
    ]
    assert pause.one() == (True, "false_exact_candidate")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_retained_fixture_evaluation_preserves_evidence_lineage(
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
        ) as client,
    ):
        await sign_in_and_declare(client, email_provider, "operator@example.com")
        form = await client.get("/queues/new")
        created = await client.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "HANS0002"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        contract_response = await client.get(created.headers["location"])

    assert created.status_code == 303
    assert "Founder review pending" in contract_response.text
    assert "HANS0002" not in contract_response.text
    evaluation_id = created.headers["location"].rsplit("/", maxsplit=1)[-1]
    async with evidence_database.connect() as connection:
        lineage = (
            (
                await connection.execute(
                    text(
                        "SELECT evaluations.source_revision_id, revisions.state, "
                        "revisions.completed_at, rows.recall_number, "
                        "rows.official_url, "
                        "rows.source_revision_completed_at, current.revision_id "
                        "AS current_revision_id "
                        "FROM evidence_evaluations AS evaluations "
                        "JOIN cpsc_source_revisions AS revisions "
                        "ON revisions.revision_id = evaluations.source_revision_id "
                        "JOIN evidence_rows AS rows "
                        "ON rows.evaluation_id = evaluations.evaluation_id "
                        "JOIN cpsc_current_source_revision AS current "
                        "ON current.singleton = true "
                        "WHERE evaluations.evaluation_id = CAST(:evaluation_id AS uuid)"
                    ),
                    {"evaluation_id": evaluation_id},
                )
            )
            .mappings()
            .one()
        )

    assert lineage["state"] == "completed"
    assert lineage["source_revision_id"] == lineage["current_revision_id"]
    assert lineage["recall_number"] == "26651"
    assert lineage["official_url"].endswith("Entrapment-and-Fall-Hazards")
    assert lineage["source_revision_completed_at"] == lineage["completed_at"]
    with pytest.raises(DBAPIError):
        async with evidence_database.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE evidence_evaluations SET matcher_version = 'changed' "
                    "WHERE evaluation_id = CAST(:evaluation_id AS uuid)"
                ),
                {"evaluation_id": evaluation_id},
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_queue_submission_rejects_unbounded_input_and_replays_only_same_input(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    del evidence_database
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
        ) as client,
    ):
        await sign_in_and_declare(client, email_provider, "operator@example.com")
        form = await client.get("/queues/new")
        rejected = await client.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "https://example.test/list"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        page_text_rejected = await client.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "brand"),
                    ("identifier_value", "A product description " * 8),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", client.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        short_page_text_rejected = await client.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "This product is recommended"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", client.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        versioned_page_text_rejected = await client.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "This product version 1 is recommended"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", client.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        valid_common = [
            ("identifier_type", "upc"),
            ("identifier_value", "000123456789"),
            ("authorization", "authorized"),
            ("idempotency_key", form.headers["x-idempotency-key"]),
            ("csrf_token", client.cookies["ado_csrf"]),
        ]
        created = await client.post(
            "/queues",
            content=urlencode(valid_common),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        replayed = await client.post(
            "/queues",
            content=urlencode(valid_common),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        conflict = await client.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "OTHER-M2"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", client.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    assert rejected.status_code == 400
    assert page_text_rejected.status_code == 400
    assert short_page_text_rejected.status_code == 400
    assert versioned_page_text_rejected.status_code == 400
    assert "Submit only explicit UPC, model, or brand rows" in rejected.text
    assert created.status_code == 303
    assert replayed.headers["location"] == created.headers["location"]
    assert conflict.status_code == 409


@pytest.mark.asyncio
@pytest.mark.integration
async def test_evaluation_failure_leaves_no_released_partial_queue(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    import_completed_fixture(postgres_url)
    async with evidence_database.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE evidence_evaluation_inputs ADD CONSTRAINT "
                "simulate_evaluation_failure CHECK (false) NOT VALID"
            )
        )
    email_provider = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email_provider,
        clock=lambda: datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
        public_origin="https://test",
        secure_cookies=True,
    )
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="https://test",
                follow_redirects=False,
            ) as client,
        ):
            await sign_in_and_declare(client, email_provider, "operator@example.com")
            form = await client.get("/queues/new")
            response = await client.post(
                "/queues",
                content=urlencode(
                    [
                        ("identifier_type", "upc"),
                        ("identifier_value", "000123456789"),
                        ("authorization", "authorized"),
                        ("idempotency_key", form.headers["x-idempotency-key"]),
                        ("csrf_token", form.cookies["ado_csrf"]),
                    ]
                ),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
    finally:
        async with evidence_database.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE evidence_evaluation_inputs DROP CONSTRAINT "
                    "simulate_evaluation_failure"
                )
            )

    async with evidence_database.connect() as connection:
        evaluation_count = await connection.scalar(
            text("SELECT count(*) FROM evidence_evaluations")
        )
    assert response.status_code == 503
    assert evaluation_count == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_operator_can_append_a_human_acknowledgement_to_a_released_queue(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    del evidence_database
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
        form = await operator.get("/queues/new")
        created = await operator.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "upc"),
                    ("identifier_value", "000123456789"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        await operator.get(created.headers["location"])
        acknowledgements = []
        for outcome in (
            "reviewed_same_product",
            "reviewed_different_product",
            "need_more_identifiers",
            "not_sure",
        ):
            acknowledgements.append(
                await operator.post(
                    f"{created.headers['location']}/acknowledgements",
                    data={
                        "outcome": outcome,
                        "csrf_token": operator.cookies["ado_csrf"],
                    },
                )
            )
        acknowledged_queue = await operator.get(created.headers["location"])
        keys_page = await operator.get("/agent-keys")
        created_key = await operator.post(
            "/agent-keys",
            data={
                "scopes": ["queues:read"],
                "csrf_token": keys_page.cookies["ado_csrf"],
            },
        )
        secret = re.search(
            r'<code id="agent-key-secret">([^<]+)</code>', created_key.text
        )
        assert secret is not None
        api_queue = await operator.get(
            f"/api/v1/queues/{created.headers['location'].rsplit('/', maxsplit=1)[-1]}",
            headers={"Authorization": f"Bearer {secret.group(1)}"},
        )

    assert [acknowledgement.status_code for acknowledgement in acknowledgements] == [
        303,
        303,
        303,
        303,
    ]
    assert "Current human acknowledgement: not sure" in acknowledged_queue.text
    assert acknowledged_queue.text.count("supersedes the prior acknowledgement") == 3
    assert (
        "operator's report, not a safety, legal, recall-status, or removal finding"
        in acknowledged_queue.text
    )
    assert api_queue.json()["reviews"]["current_human_acknowledgement"] == {
        "acknowledged_at": "2026-09-04T10:00:00+00:00",
        "outcome": "not_sure",
        "report_type": "human_acknowledgement",
    }
    assert len(api_queue.json()["reviews"]["human_acknowledgement_history"]) == 4
    assert api_queue.json()["reviews"]["agent_review_reports"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_official_evidence_handoff_records_no_submitted_identifier(
    postgres_url: str, evidence_database: AsyncEngine
) -> None:
    import_completed_fixture(postgres_url)
    async with evidence_database.begin() as connection:
        await connection.execute(
            text(
                "UPDATE audit_gate_state SET committed_queue_count = 20 "
                "WHERE singleton = true"
            )
        )
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
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as another_operator,
    ):
        await sign_in_and_declare(operator, email_provider, "operator@example.com")
        form = await operator.get("/queues/new")
        created = await operator.post(
            "/queues",
            content=urlencode(
                [
                    ("identifier_type", "model"),
                    ("identifier_value", "HANS0002"),
                    ("authorization", "authorized"),
                    ("idempotency_key", form.headers["x-idempotency-key"]),
                    ("csrf_token", form.cookies["ado_csrf"]),
                ]
            ),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        queue = await operator.get(created.headers["location"])
        handoff_path = re.search(r'href="(/queues/[^/]+/evidence/[^/]+)"', queue.text)
        assert handoff_path is not None
        handoff = await operator.get(handoff_path.group(1))
        await sign_in_and_declare(another_operator, email_provider, "other@example.com")
        forbidden_handoff = await another_operator.get(handoff_path.group(1))

    async with evidence_database.connect() as connection:
        retrieval = (
            await connection.execute(
                text(
                    "SELECT agent_key_id, evidence_row_id FROM "
                    "source_evidence_retrievals"
                )
            )
        ).one()
        columns = await connection.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'source_evidence_retrievals'"
            )
        )

    assert handoff.status_code == 303
    assert handoff.headers["location"].endswith("Entrapment-and-Fall-Hazards")
    assert forbidden_handoff.status_code == 404
    assert retrieval[0] is None
    assert str(retrieval[1]) == re.search(
        r"evidence/([^/]+)", handoff_path.group(1)
    ).group(1)
    assert not any("identifier" in column for column in columns)
