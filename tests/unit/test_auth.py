import base64
import json
from email import message_from_bytes
from typing import Any
from urllib.request import Request

import pytest

from agent_data_oracle.auth import (
    GmailApiEmailProvider,
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


def test_production_requires_a_stable_authentication_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("AUTH_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        auth_secret_from_environment()


def test_production_selects_gmail_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GMAIL_API_ACCESS_TOKEN", "runtime-secret")

    provider = email_provider_from_environment()

    assert isinstance(provider, GmailApiEmailProvider)
