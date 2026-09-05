import base64
import json
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from typing import Any
from urllib.error import URLError
from urllib.request import Request

import pytest

from agent_data_oracle.auth import (
    GmailApiEmailProvider,
    GmailOAuthAccessToken,
    email_provider_from_environment,
)
from agent_data_oracle.config import auth_secret_from_environment


class GmailResponse:
    status = 200

    def __enter__(self) -> "GmailResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_gmail_provider_sends_the_sign_in_link_without_logging_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Request] = []

    def urlopen(request: Request, *, timeout: int) -> GmailResponse:
        assert timeout == 10
        captured.append(request)
        return GmailResponse()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    provider = GmailApiEmailProvider(lambda: "oauth-access-token")

    await provider.send_sign_in_link(
        recipient="operator@example.com",
        sign_in_url="https://service.test/auth/verify?token=one-time-secret",
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    )
    assert request.headers["Authorization"] == "Bearer oauth-access-token"
    body: dict[str, Any] = json.loads(request.data or b"{}")
    message = message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert message["To"] == "operator@example.com"
    assert "one-time-secret" in message.get_payload()


@pytest.mark.parametrize(
    "failure",
    [URLError("temporary provider outage"), TimeoutError("temporary provider timeout")],
)
@pytest.mark.asyncio
async def test_gmail_provider_retries_a_transient_delivery_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    attempts = 0

    def urlopen(request: Request, *, timeout: int) -> GmailResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure
        return GmailResponse()

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("agent_data_oracle.auth.asyncio.sleep", no_wait)
    provider = GmailApiEmailProvider(lambda: "oauth-access-token")

    await provider.send_sign_in_link(
        recipient="operator@example.com",
        sign_in_url="https://service.test/auth/verify?token=one-time-secret",
    )

    assert attempts == 2


def test_production_requires_a_stable_authentication_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        auth_secret_from_environment()


def test_production_selects_gmail_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GMAIL_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GMAIL_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GMAIL_OAUTH_REFRESH_TOKEN", "refresh-token")

    provider = email_provider_from_environment()

    assert isinstance(provider, GmailApiEmailProvider)


def test_gmail_oauth_access_token_refreshes_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    requests: list[Request] = []

    class OAuthResponse(GmailResponse):
        def read(self) -> bytes:
            return b'{"access_token":"fresh-token","expires_in":3600}'

    def urlopen(request: Request, *, timeout: int) -> OAuthResponse:
        assert timeout == 10
        requests.append(request)
        return OAuthResponse()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    clock = MutableTokenClock(now)
    access_token = GmailOAuthAccessToken(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        clock=clock,
    )

    assert access_token() == "fresh-token"
    assert access_token() == "fresh-token"
    clock.instant += timedelta(hours=1)
    assert access_token() == "fresh-token"
    assert len(requests) == 2
    assert b"refresh_token=refresh-token" in (requests[0].data or b"")


class MutableTokenClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant
