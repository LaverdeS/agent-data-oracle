import hashlib
import hmac
from dataclasses import dataclass


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
        normalized_recipients = frozenset(
            email.strip().casefold() for email in recipient_emails if email.strip()
        )
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
