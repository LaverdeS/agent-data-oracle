import hashlib
import hmac
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass


def normalized_email_set(emails: Iterable[str]) -> frozenset[str]:
    return frozenset(email.strip().casefold() for email in emails if email.strip())


@dataclass(frozen=True)
class PreviewAccess:
    """Enforce the founder-only preview admission policy."""

    recipient_emails: frozenset[str]
    _secret_digest: bytes | None

    @classmethod
    def disabled(cls) -> "PreviewAccess":
        return cls(frozenset(), None)

    @classmethod
    def founder_only(
        cls,
        *,
        access_secret: str,
        recipient_emails: frozenset[str],
        founder_emails: frozenset[str],
    ) -> "PreviewAccess":
        normalized_recipients = normalized_email_set(recipient_emails)
        if len(access_secret.encode("utf-8")) < 24:
            raise ValueError("preview access secret must contain at least 24 bytes")
        if not normalized_recipients:
            raise ValueError("preview recipient allowlist must not be empty")
        if not normalized_recipients <= founder_emails:
            raise ValueError("preview recipients must all be configured founders")
        return cls(
            normalized_recipients,
            hashlib.sha256(access_secret.encode("utf-8")).digest(),
        )

    @property
    def is_enabled(self) -> bool:
        return self._secret_digest is not None

    def admits_sign_in(self, *, email: str | None, access_secret: str) -> bool:
        if not self.is_enabled:
            return True
        supplied_digest = hashlib.sha256(access_secret.encode("utf-8")).digest()
        return (
            email in self.recipient_emails
            and self._secret_digest is not None
            and hmac.compare_digest(supplied_digest, self._secret_digest)
        )

    def admits_identity(self, email: str) -> bool:
        return not self.is_enabled or email.casefold() in self.recipient_emails


class ManualPreviewAdmission:
    """Keep a short-lived manual-preview admission only in process memory."""

    _LIFETIME_SECONDS = 300

    def __init__(self) -> None:
        self._admissions: dict[bytes, float] = {}

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._remove_expired()
        self._admissions[self._digest(token)] = (
            time.monotonic() + self._LIFETIME_SECONDS
        )
        return token

    def admits(self, token: str | None) -> bool:
        if token is None:
            return False
        self._remove_expired()
        return self._digest(token) in self._admissions

    def consume(self, token: str | None) -> bool:
        if not self.admits(token):
            return False
        assert token is not None
        self._admissions.pop(self._digest(token), None)
        return True

    def _remove_expired(self) -> None:
        now = time.monotonic()
        for digest, expires_at in tuple(self._admissions.items()):
            if expires_at <= now:
                self._admissions.pop(digest, None)

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()
