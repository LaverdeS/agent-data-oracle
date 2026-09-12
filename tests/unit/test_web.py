import pytest
from httpx import ASGITransport, AsyncClient

from agent_data_oracle.auth import LocalCaptureEmailProvider
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
    assert "Google Cloud in Frankfurt" in response.text
    assert "processed by consumer Gmail" in response.text
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
