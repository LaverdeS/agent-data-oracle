import pytest
from httpx import ASGITransport, AsyncClient

from agent_data_oracle.auth import LocalCaptureEmailProvider
from agent_data_oracle.config import (
    provider_disclosure_from_environment,
    validated_local_preview_harness,
)
from agent_data_oracle.web import create_app
from tests.preview import founder_preview_access


@pytest.mark.asyncio
async def test_root_renders_the_product_boundary(
    unavailable_database_url: str,
) -> None:
    app = create_app(database_url=unavailable_database_url)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "CPSC evidence, not a safety verdict" in response.text
    assert "No payment is accepted" in response.text
    assert "30 days after the phase closes" in response.text
    assert "local development environment" in response.text
    assert "captured in memory" in response.text
    assert "Google Cloud in Frankfurt" not in response.text
    assert "consumer Gmail" not in response.text
    assert "Google Workspace" not in response.text
    assert "not legal advice" in response.text
    assert "not endorsed by CPSC" in response.text
    assert 'href="/sign-in"' in response.text


@pytest.mark.asyncio
async def test_preview_root_does_not_present_usage_learning_as_active(
    unavailable_database_url: str,
) -> None:
    app = create_app(
        database_url=unavailable_database_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=LocalCaptureEmailProvider(),
        public_origin="https://test",
        secure_cookies=True,
        founder_emails=frozenset({"founder@example.com"}),
        preview_access=founder_preview_access("founder@example.com"),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Founder-only development preview" in response.text
    assert "usage-learning phase is not active" in response.text
    assert "local development environment" in response.text


@pytest.mark.asyncio
async def test_local_capture_link_can_be_claimed_once_through_secret_gated_route(
    monkeypatch: pytest.MonkeyPatch, unavailable_database_url: str
) -> None:
    monkeypatch.setenv("PREVIEW_RECIPIENT_EMAILS", "founder@example.com")
    email = LocalCaptureEmailProvider()
    app = create_app(
        database_url=unavailable_database_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=email,
        founder_emails=frozenset({"founder@example.com"}),
        local_preview_harness=True,
    )
    await email.send_sign_in_link(
        recipient="founder@example.com",
        sign_in_url="http://127.0.0.1:8080/auth/verify?token=one-time-secret",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url="http://test",
    ) as client:
        access = await client.post("/_local/preview-access")
        preview_secret = access.json()["preview_access_secret"]
        denied = await client.post(
            "/_local/sign-in-links/claim",
            headers={"Authorization": "Bearer wrong-secret"},
            json={"recipient": "founder@example.com"},
        )
        claimed = await client.post(
            "/_local/sign-in-links/claim",
            headers={"Authorization": f"Bearer {preview_secret}"},
            json={"recipient": "founder@example.com"},
        )
        replayed = await client.post(
            "/_local/sign-in-links/claim",
            headers={"Authorization": f"Bearer {preview_secret}"},
            json={"recipient": "founder@example.com"},
        )

    assert access.status_code == 200
    assert isinstance(preview_secret, str)
    assert denied.status_code == 404
    assert claimed.status_code == 200
    assert claimed.json() == {
        "sign_in_url": ("http://127.0.0.1:8080/auth/verify?token=one-time-secret")
    }
    assert replayed.status_code == 404


@pytest.mark.asyncio
async def test_local_preview_access_is_hidden_from_non_loopback_clients(
    monkeypatch: pytest.MonkeyPatch, unavailable_database_url: str
) -> None:
    monkeypatch.setenv("PREVIEW_RECIPIENT_EMAILS", "founder@example.com")
    app = create_app(
        database_url=unavailable_database_url,
        auth_secret=b"test-secret-that-is-long-enough",
        founder_emails=frozenset({"founder@example.com"}),
        local_preview_harness=True,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.1", 1234)),
        base_url="http://test",
    ) as client:
        response = await client.post("/_local/preview-access")

    assert response.status_code == 404


def test_local_capture_route_is_absent_without_explicit_configuration(
    unavailable_database_url: str,
) -> None:
    app = create_app(
        database_url=unavailable_database_url,
        auth_secret=b"test-secret-that-is-long-enough",
        email_provider=LocalCaptureEmailProvider(),
    )

    local_paths = {
        "/_local/preview-access",
        "/_local/sign-in-links/claim",
    }
    assert all(getattr(route, "path", None) not in local_paths for route in app.routes)


def test_deployed_provider_copy_must_be_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("PROVIDER_DISCLOSURE", raising=False)

    with pytest.raises(RuntimeError, match="PROVIDER_DISCLOSURE"):
        provider_disclosure_from_environment()

    monkeypatch.setenv(
        "PROVIDER_DISCLOSURE",
        "Data uses Example EU hosting; mail uses Example Mail processing.",
    )
    assert provider_disclosure_from_environment() == (
        "Data uses Example EU hosting; mail uses Example Mail processing."
    )


def test_deployed_process_cannot_enable_local_preview_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(RuntimeError, match="forbidden outside local/test"):
        validated_local_preview_harness(True)


def test_preview_configuration_fails_closed_outside_local_test(
    monkeypatch: pytest.MonkeyPatch, unavailable_database_url: str
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("PREVIEW_ACCESS_SECRET", raising=False)
    monkeypatch.delenv("PREVIEW_RECIPIENT_EMAILS", raising=False)

    with pytest.raises(
        RuntimeError,
        match="PREVIEW_ACCESS_SECRET and PREVIEW_RECIPIENT_EMAILS are required",
    ):
        create_app(
            database_url=unavailable_database_url,
            auth_secret=b"test-secret-that-is-long-enough",
            email_provider=LocalCaptureEmailProvider(),
            public_origin="https://test",
            secure_cookies=True,
            founder_emails=frozenset({"founder@example.com"}),
        )


def test_preview_recipient_allowlist_can_only_contain_founders(
    unavailable_database_url: str,
) -> None:
    del unavailable_database_url
    with pytest.raises(
        ValueError, match="preview recipients must all be configured founders"
    ):
        founder_preview_access(
            "visitor@example.com",
            founder_emails=frozenset({"founder@example.com"}),
        )
