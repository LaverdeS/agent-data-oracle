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

    assert submitted.status_code == 303
    assert "No candidate recall-to-listing action records" in queue.text
    assert "000123456789" in queue.text
    assert queue.text.count("000123456789") == 2
    assert "Model ZX-9" in queue.text
    assert "do not prove that the CPSC source is complete" in queue.text
    assert "CPSC/U.S. consumer-product recall data only" in queue.text
    assert queue.text == reopened.text
    assert forbidden_queue.status_code == 404
    with pytest.raises(DBAPIError):
        async with evidence_database.begin() as connection:
            await connection.execute(
                text("UPDATE evidence_evaluations SET matcher_version = 'changed'")
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_operator_receives_source_linked_possible_candidate_evidence(
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
    assert "Possible recall-to-listing action records" in queue.text
    assert "MODEL No.: HANS0002" in queue.text
    assert "26651" in queue.text
    assert "Submitted identifier:</strong> model: HANS0002" in queue.text
    assert "Brand equality alone is insufficient identity" in queue.text
    assert "CPSC does not endorse this service" in queue.text
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
