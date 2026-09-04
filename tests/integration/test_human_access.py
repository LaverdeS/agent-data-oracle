import base64
import hashlib
import hmac
import re
import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent_data_oracle.auth import LocalCaptureEmailProvider
from agent_data_oracle.schema import migrate_to_head
from agent_data_oracle.web import create_app


class MutableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant

    def advance(self, delta: timedelta) -> None:
        self.instant += delta


class FailingEmailProvider:
    async def send_sign_in_link(self, *, recipient: str, sign_in_url: str) -> None:
        del recipient, sign_in_url
        raise RuntimeError("provider unavailable")


@pytest_asyncio.fixture
async def access_database(postgres_url: str) -> AsyncIterator[None]:
    migrate_to_head(postgres_url)
    engine: AsyncEngine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE auth_recovery_codes, founder_totp_factors, "
                "browser_sessions, login_tokens, auth_attempts, operators CASCADE"
            )
        )
    try:
        yield
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sign_in_request_is_generic_and_delivers_a_single_use_link(
    postgres_url: str, access_database: None
) -> None:
    del access_database
    email = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email,
        clock=lambda: datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
        secure_cookies=True,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client,
    ):
        form = await client.get("/sign-in")
        csrf = form.cookies["ado_csrf"]
        response = await client.post(
            "/auth/sign-in",
            data={"email": " Operator@Example.COM ", "csrf_token": csrf},
        )

    assert form.status_code == 200
    assert 'type="email"' in form.text
    assert response.status_code == 202
    assert "If the address can receive sign-in email" in response.text
    assert len(email.deliveries) == 1
    assert email.deliveries[0].recipient == "operator@example.com"
    assert email.deliveries[0].sign_in_url.startswith("https://test/auth/verify?token=")
    assert "operator@example.com" not in response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_single_use_link_creates_session_and_records_operator_declaration(
    postgres_url: str, access_database: None
) -> None:
    del access_database
    email = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email,
        clock=lambda: datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
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
        sign_in = await client.get("/sign-in")
        await client.post(
            "/auth/sign-in",
            data={
                "email": "operator@example.com",
                "csrf_token": sign_in.cookies["ado_csrf"],
            },
        )
        verification_url = email.deliveries[0].sign_in_url
        token = parse_qs(urlparse(verification_url).query)["token"][0]
        confirmation = await client.get(verification_url)
        verified = await client.post(
            "/auth/verify",
            data={"token": token, "csrf_token": confirmation.cookies["ado_csrf"]},
        )
        declaration = await client.get(verified.headers["location"])
        declared = await client.post(
            "/declare",
            data={
                "operator_type": "business_operator",
                "sells_into_us": "yes",
                "csrf_token": client.cookies["ado_csrf"],
            },
        )
        shell = await client.get(declared.headers["location"])

        replay_confirmation = await client.get(f"/auth/verify?token={token}")
        replay = await client.post(
            "/auth/verify",
            data={
                "token": token,
                "csrf_token": replay_confirmation.cookies["ado_csrf"],
            },
        )

    assert verified.status_code == 303
    assert verified.headers["location"] == "/declare"
    session_cookie = verified.headers["set-cookie"]
    assert "ado_session=" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert declaration.status_code == 200
    assert "How are you using this evidence queue?" in declaration.text
    assert declared.status_code == 303
    assert declared.headers["location"] == "/app"
    assert "Declared business operator" in shell.text
    assert "Products sold into the United States" in shell.text
    assert "does not verify your legal business status" in shell.text
    assert replay.status_code == 400
    assert "expired or has already been used" in replay.text


def totp_code(secret: str, instant: datetime) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int(instant.timestamp()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


async def request_login(client: AsyncClient, email: LocalCaptureEmailProvider) -> str:
    sign_in = await client.get("/sign-in")
    await client.post(
        "/auth/sign-in",
        data={
            "email": "founder@example.com",
            "csrf_token": sign_in.cookies["ado_csrf"],
        },
    )
    return parse_qs(urlparse(email.deliveries[-1].sign_in_url).query)["token"][0]


async def verify_login(client: AsyncClient, token: str) -> None:
    confirmation = await client.get(f"/auth/verify?token={token}")
    response = await client.post(
        "/auth/verify",
        data={"token": token, "csrf_token": confirmation.cookies["ado_csrf"]},
    )
    assert response.status_code == 303


@pytest.mark.asyncio
@pytest.mark.integration
async def test_founder_requires_totp_and_receives_one_time_recovery_codes(
    postgres_url: str, access_database: None
) -> None:
    del access_database
    now = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    email = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email,
        clock=lambda: now,
        secure_cookies=True,
        founder_emails=frozenset({"founder@example.com"}),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            follow_redirects=False,
        ) as client,
    ):
        await verify_login(client, await request_login(client, email))
        founder_gate = await client.get("/founder")
        enrollment = await client.get(founder_gate.headers["location"])
        secret = re.search(r'data-totp-secret="([A-Z2-7]+)"', enrollment.text).group(1)  # type: ignore[union-attr]
        enrolled = await client.post(
            "/founder/totp/enroll",
            data={
                "code": totp_code(secret, now),
                "csrf_token": enrollment.cookies["ado_csrf"],
            },
        )
        recovery_codes = re.findall(
            r'data-recovery-code="([A-Za-z0-9_-]+)"', enrolled.text
        )
        founder_shell = await client.get("/founder")

        client.cookies.clear()
        await verify_login(client, await request_login(client, email))
        challenge_redirect = await client.get("/founder")
        challenge = await client.get(challenge_redirect.headers["location"])
        recovered = await client.post(
            "/founder/totp",
            data={
                "credential": recovery_codes[0],
                "csrf_token": challenge.cookies["ado_csrf"],
            },
        )

        client.cookies.clear()
        await verify_login(client, await request_login(client, email))
        second_challenge = await client.get("/founder/totp")
        reused = await client.post(
            "/founder/totp",
            data={
                "credential": recovery_codes[0],
                "csrf_token": second_challenge.cookies["ado_csrf"],
            },
        )

    assert founder_gate.status_code == 303
    assert founder_gate.headers["location"] == "/founder/totp/enroll"
    assert enrollment.status_code == 200
    assert enrolled.status_code == 200
    assert len(recovery_codes) == 8
    assert "shown only once" in enrolled.text
    assert founder_shell.status_code == 200
    assert "Founder controls" in founder_shell.text
    assert challenge_redirect.headers["location"] == "/founder/totp"
    assert recovered.status_code == 303
    assert recovered.headers["location"] == "/founder"
    assert reused.status_code == 403
    assert "could not be verified" in reused.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_login_and_session_expiry_rejection_and_revocation(
    postgres_url: str, access_database: None
) -> None:
    del access_database
    clock = MutableClock(datetime(2026, 9, 4, 10, 0, tzinfo=UTC))
    email = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email,
        clock=clock,
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
        sign_in = await client.get("/sign-in")
        missing_csrf = await client.post(
            "/auth/sign-in", data={"email": "operator@example.com"}
        )
        await client.post(
            "/auth/sign-in",
            data={
                "email": "operator@example.com",
                "csrf_token": sign_in.cookies["ado_csrf"],
            },
        )
        expired_token = parse_qs(urlparse(email.deliveries[-1].sign_in_url).query)[
            "token"
        ][0]
        clock.advance(timedelta(minutes=16))
        expired_confirmation = await client.get(f"/auth/verify?token={expired_token}")
        expired = await client.post(
            "/auth/verify",
            data={
                "token": expired_token,
                "csrf_token": expired_confirmation.cookies["ado_csrf"],
            },
        )

        token = await request_login(client, email)
        await verify_login(client, token)
        session_token = client.cookies["ado_session"]
        declaration = await client.get("/declare")
        sign_out_without_csrf = await client.post("/auth/sign-out")
        signed_out = await client.post(
            "/auth/sign-out",
            data={"csrf_token": declaration.cookies["ado_csrf"]},
        )
        after_revocation = await client.get("/app")

    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as connection:
            login_rows = (
                await connection.execute(
                    text(
                        "SELECT token_hash, expires_at - requested_at AS lifetime "
                        "FROM login_tokens"
                    )
                )
            ).all()
            stored_session = (
                await connection.execute(
                    text(
                        "SELECT token_hash, expires_at - created_at AS lifetime, "
                        "revoked_at FROM browser_sessions"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert missing_csrf.status_code == 403
    assert expired.status_code == 400
    assert all(row.lifetime == timedelta(minutes=15) for row in login_rows)
    assert all(expired_token not in row.token_hash for row in login_rows)
    assert token not in {row.token_hash for row in login_rows}
    assert stored_session.token_hash != session_token
    assert stored_session.lifetime == timedelta(hours=12)
    assert stored_session.revoked_at is not None
    assert sign_out_without_csrf.status_code == 403
    assert signed_out.status_code == 303
    assert after_revocation.status_code == 303
    assert after_revocation.headers["location"] == "/sign-in"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sign_in_abuse_controls_do_not_retain_network_address(
    postgres_url: str, access_database: None
) -> None:
    del access_database
    email = LocalCaptureEmailProvider()
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email,
        clock=lambda: datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
        secure_cookies=True,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client,
    ):
        sign_in = await client.get("/sign-in")
        responses = [
            await client.post(
                "/auth/sign-in",
                data={
                    "email": "operator@example.com",
                    "csrf_token": sign_in.cookies["ado_csrf"],
                },
            )
            for _ in range(6)
        ]
        invalid = await client.post(
            "/auth/sign-in",
            data={
                "email": "not-an-email",
                "csrf_token": sign_in.cookies["ado_csrf"],
            },
        )

    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as connection:
            attempt_hashes = (
                await connection.scalars(text("SELECT subject_hash FROM auth_attempts"))
            ).all()
    finally:
        await engine.dispose()

    assert len(email.deliveries) == 5
    assert all(response.status_code == 202 for response in responses)
    assert invalid.status_code == 202
    assert invalid.text == responses[0].text
    assert all(len(value) == 64 for value in attempt_hashes)
    assert all("127.0.0.1" not in value for value in attempt_hashes)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_email_provider_failure_keeps_the_sign_in_response_generic(
    postgres_url: str, access_database: None
) -> None:
    del access_database
    app = create_app(
        database_url=postgres_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=FailingEmailProvider(),
        clock=lambda: datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
        secure_cookies=True,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as client,
    ):
        sign_in = await client.get("/sign-in")
        response = await client.post(
            "/auth/sign-in",
            data={
                "email": "operator@example.com",
                "csrf_token": sign_in.cookies["ado_csrf"],
            },
        )

    assert response.status_code == 202
    assert "If the address can receive sign-in email" in response.text
