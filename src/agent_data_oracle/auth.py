import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import struct
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text

from agent_data_oracle.database import Database

LOGIN_TOKEN_LIFETIME = timedelta(minutes=15)
SESSION_LIFETIME = timedelta(hours=12)
REAUTHENTICATION_LIFETIME = timedelta(minutes=15)
auth_logger = logging.getLogger("agent_data_oracle.auth")


@dataclass(frozen=True)
class SignInDelivery:
    recipient: str
    sign_in_url: str


@dataclass(frozen=True)
class SessionGrant:
    token: str
    destination: str


@dataclass(frozen=True)
class AuthenticatedOperator:
    operator_id: UUID
    email: str
    operator_type: str | None
    sells_into_us: bool | None
    is_founder: bool
    reauthenticated_at: datetime
    founder_second_factor_at: datetime | None


class SecondFactorResult(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"
    RATE_LIMITED = "rate_limited"


class EmailProvider(Protocol):
    async def send_sign_in_link(self, *, recipient: str, sign_in_url: str) -> None: ...


class LocalCaptureEmailProvider:
    """Capture local sign-in links without printing credentials to logs."""

    def __init__(self) -> None:
        self.deliveries: list[SignInDelivery] = []

    async def send_sign_in_link(self, *, recipient: str, sign_in_url: str) -> None:
        self.deliveries.append(SignInDelivery(recipient, sign_in_url))


class GmailApiEmailProvider:
    """Deliver sign-in links through Gmail's narrow send-message endpoint."""

    def __init__(self, access_token: Callable[[], str]) -> None:
        self._access_token = access_token

    async def send_sign_in_link(self, *, recipient: str, sign_in_url: str) -> None:
        message = EmailMessage()
        message["To"] = recipient
        message["Subject"] = "Your Agent Data Oracle sign-in link"
        message.set_content(
            "Use this single-use link within 15 minutes:\n\n"
            f"{sign_in_url}\n\n"
            "If you did not request it, you can ignore this email."
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        body = ('{"raw":"' + raw + '"}').encode()

        def send() -> None:
            request = urllib.request.Request(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._access_token()}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status >= 300:
                    raise RuntimeError("gmail_delivery_failed")

        await asyncio.to_thread(send)


def email_provider_from_environment() -> EmailProvider:
    if os.environ.get("APP_ENV", "local").casefold() in {"local", "test"}:
        return LocalCaptureEmailProvider()
    access_token = os.environ.get("GMAIL_API_ACCESS_TOKEN")
    if access_token is None:
        raise RuntimeError("GMAIL_API_ACCESS_TOKEN is required outside local/test")
    return GmailApiEmailProvider(lambda: access_token)


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_email(value: str) -> str | None:
    normalized = value.strip().casefold()
    if (
        len(normalized) > 254
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        return None
    local, domain = normalized.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return normalized


class HumanAccess:
    def __init__(
        self,
        *,
        database: Database,
        secret: bytes,
        email_provider: EmailProvider,
        clock: Callable[[], datetime] = utc_now,
        founder_emails: frozenset[str] = frozenset(),
    ) -> None:
        if len(secret) < 24:
            raise ValueError("auth secret must contain at least 24 bytes")
        self._database = database
        self._secret = secret
        self._email_provider = email_provider
        self._clock = clock
        self._founder_emails = founder_emails

    def _digest(self, namespace: str, value: str) -> str:
        return hmac.new(
            self._secret,
            f"{namespace}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def issue_csrf_token(self) -> str:
        nonce = secrets.token_urlsafe(24)
        signature = self._digest("csrf", nonce)
        return f"{nonce}.{signature}"

    def csrf_token_is_valid(self, cookie: str | None, submitted: str | None) -> bool:
        if (
            cookie is None
            or submitted is None
            or not hmac.compare_digest(cookie, submitted)
        ):
            return False
        try:
            nonce, signature = submitted.rsplit(".", 1)
        except ValueError:
            return False
        return hmac.compare_digest(self._digest("csrf", nonce), signature)

    async def request_sign_in(
        self, *, email: str, network_identity: str, base_url: str
    ) -> None:
        normalized = normalize_email(email)
        now = self._clock()
        email_subject = normalized or "invalid"
        email_hash = self._digest("rate-email", email_subject)
        network_hash = self._digest("rate-network", network_identity)
        allowed = False
        token: str | None = None

        async with self._database.transaction() as connection:
            for subject_hash in (network_hash, email_hash):
                await connection.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(:subject_hash, 0))"
                    ),
                    {"subject_hash": subject_hash},
                )
            counts = (
                (
                    await connection.execute(
                        text(
                            "SELECT subject_kind, count(*) AS attempt_count "
                            "FROM auth_attempts "
                            "WHERE attempted_at > :window_start AND ("
                            "(subject_kind = 'email' "
                            "AND subject_hash = :email_hash) OR "
                            "(subject_kind = 'network' "
                            "AND subject_hash = :network_hash)) "
                            "GROUP BY subject_kind"
                        ),
                        {
                            "email_hash": email_hash,
                            "network_hash": network_hash,
                            "window_start": now - timedelta(hours=1),
                        },
                    )
                )
                .mappings()
                .all()
            )
            count_by_kind = {
                str(row["subject_kind"]): int(row["attempt_count"]) for row in counts
            }
            await connection.execute(
                text(
                    "INSERT INTO auth_attempts "
                    "(subject_kind, subject_hash, attempted_at) VALUES "
                    "('email', :email_hash, :attempted_at), "
                    "('network', :network_hash, :attempted_at)"
                ),
                {
                    "attempted_at": now,
                    "email_hash": email_hash,
                    "network_hash": network_hash,
                },
            )
            allowed = (
                normalized is not None
                and count_by_kind.get("email", 0) < 5
                and count_by_kind.get("network", 0) < 20
            )
            if allowed:
                token = secrets.token_urlsafe(32)
                await connection.execute(
                    text(
                        "INSERT INTO login_tokens "
                        "(token_id, email_normalized, token_hash, requested_at, "
                        "expires_at) VALUES (:token_id, :email, :token_hash, "
                        ":requested_at, :expires_at)"
                    ),
                    {
                        "email": normalized,
                        "expires_at": now + LOGIN_TOKEN_LIFETIME,
                        "requested_at": now,
                        "token_hash": self._digest("login", token),
                        "token_id": uuid4(),
                    },
                )

        if allowed and token is not None and normalized is not None:
            try:
                await self._email_provider.send_sign_in_link(
                    recipient=normalized,
                    sign_in_url=f"{base_url}/auth/verify?token={token}",
                )
            except Exception:
                auth_logger.exception("sign_in_delivery_failed")

    async def consume_sign_in_token(self, token: str) -> SessionGrant | None:
        now = self._clock()
        async with self._database.transaction() as connection:
            email = await connection.scalar(
                text(
                    "UPDATE login_tokens SET consumed_at = :consumed_at "
                    "WHERE token_hash = :token_hash AND consumed_at IS NULL "
                    "AND expires_at > :consumed_at RETURNING email_normalized"
                ),
                {
                    "consumed_at": now,
                    "token_hash": self._digest("login", token),
                },
            )
            if not isinstance(email, str):
                return None
            operator_id = await connection.scalar(
                text(
                    "INSERT INTO operators "
                    "(operator_id, email_normalized, is_founder, created_at) "
                    "VALUES (:operator_id, :email, :is_founder, :created_at) "
                    "ON CONFLICT (email_normalized) DO UPDATE SET "
                    "is_founder = operators.is_founder OR EXCLUDED.is_founder "
                    "RETURNING operator_id"
                ),
                {
                    "created_at": now,
                    "email": email,
                    "is_founder": email in self._founder_emails,
                    "operator_id": uuid4(),
                },
            )
            if not isinstance(operator_id, UUID):
                raise RuntimeError("operator_creation_failed")
            session_token = secrets.token_urlsafe(32)
            await connection.execute(
                text(
                    "INSERT INTO browser_sessions "
                    "(session_id, operator_id, token_hash, created_at, expires_at, "
                    "reauthenticated_at) VALUES (:session_id, :operator_id, "
                    ":token_hash, :created_at, :expires_at, :reauthenticated_at)"
                ),
                {
                    "created_at": now,
                    "expires_at": now + SESSION_LIFETIME,
                    "operator_id": operator_id,
                    "reauthenticated_at": now,
                    "session_id": uuid4(),
                    "token_hash": self._digest("session", session_token),
                },
            )
            declared = await connection.scalar(
                text(
                    "SELECT declaration_recorded_at IS NOT NULL FROM operators "
                    "WHERE operator_id = :operator_id"
                ),
                {"operator_id": operator_id},
            )
        return SessionGrant(
            token=session_token,
            destination="/app" if declared is True else "/declare",
        )

    async def authenticated_operator(
        self, session_token: str | None
    ) -> AuthenticatedOperator | None:
        if session_token is None:
            return None
        now = self._clock()
        async with self._database.connection() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT operators.operator_id, operators.email_normalized, "
                            "operators.operator_type, operators.sells_into_us, "
                            "operators.is_founder, sessions.reauthenticated_at, "
                            "sessions.founder_second_factor_at "
                            "FROM browser_sessions AS sessions "
                            "JOIN operators ON "
                            "operators.operator_id = sessions.operator_id "
                            "WHERE sessions.token_hash = :token_hash "
                            "AND sessions.revoked_at IS NULL "
                            "AND sessions.expires_at > :now"
                        ),
                        {
                            "now": now,
                            "token_hash": self._digest("session", session_token),
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return AuthenticatedOperator(
            operator_id=row["operator_id"],
            email=row["email_normalized"],
            operator_type=row["operator_type"],
            sells_into_us=row["sells_into_us"],
            is_founder=row["is_founder"],
            reauthenticated_at=row["reauthenticated_at"],
            founder_second_factor_at=row["founder_second_factor_at"],
        )

    async def record_declaration(
        self,
        *,
        session_token: str,
        operator_type: str,
        sells_into_us: bool,
    ) -> bool:
        if operator_type not in {"business_operator", "agent_operator"}:
            return False
        now = self._clock()
        async with self._database.transaction() as connection:
            updated = await connection.scalar(
                text(
                    "UPDATE operators SET operator_type = :operator_type, "
                    "sells_into_us = :sells_into_us, declaration_recorded_at = :now "
                    "WHERE operator_id = (SELECT operator_id FROM browser_sessions "
                    "WHERE token_hash = :token_hash AND revoked_at IS NULL "
                    "AND expires_at > :now "
                    "AND reauthenticated_at > :reauthenticated_after) "
                    "AND declaration_recorded_at IS NULL RETURNING operator_id"
                ),
                {
                    "now": now,
                    "operator_type": operator_type,
                    "reauthenticated_after": now - REAUTHENTICATION_LIFETIME,
                    "sells_into_us": sells_into_us,
                    "token_hash": self._digest("session", session_token),
                },
            )
        return isinstance(updated, UUID)

    def reauthentication_is_current(self, operator: AuthenticatedOperator) -> bool:
        return self._clock() - operator.reauthenticated_at <= (
            REAUTHENTICATION_LIFETIME
        )

    async def revoke_session(self, session_token: str) -> None:
        now = self._clock()
        async with self._database.transaction() as connection:
            await connection.execute(
                text(
                    "UPDATE browser_sessions SET revoked_at = :now "
                    "WHERE token_hash = :token_hash AND revoked_at IS NULL"
                ),
                {"now": now, "token_hash": self._digest("session", session_token)},
            )

    def _founder_totp_key(self, operator_id: UUID) -> bytes:
        return hmac.new(
            self._secret,
            b"founder-totp:" + operator_id.bytes,
            hashlib.sha256,
        ).digest()[:20]

    def _founder_totp_secret(self, operator_id: UUID) -> str:
        return (
            base64.b32encode(self._founder_totp_key(operator_id)).decode().rstrip("=")
        )

    def _totp_is_valid(self, operator_id: UUID, submitted: str) -> bool:
        if len(submitted) != 6 or not submitted.isdigit():
            return False
        counter = int(self._clock().timestamp()) // 30
        for candidate_counter in range(counter - 1, counter + 2):
            digest = hmac.new(
                self._founder_totp_key(operator_id),
                struct.pack(">Q", candidate_counter),
                hashlib.sha1,
            ).digest()
            offset = digest[-1] & 0x0F
            value = struct.unpack(">I", digest[offset : offset + 4])[0]
            expected = f"{(value & 0x7FFFFFFF) % 1_000_000:06d}"
            if hmac.compare_digest(expected, submitted):
                return True
        return False

    @staticmethod
    def _recovery_hash(code: str, salt: bytes) -> bytes:
        return hashlib.scrypt(code.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)

    async def founder_factor_exists(self, operator_id: UUID) -> bool:
        async with self._database.connection() as connection:
            exists = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM founder_totp_factors "
                    "WHERE operator_id = :operator_id)"
                ),
                {"operator_id": operator_id},
            )
        return exists is True

    async def totp_enrollment_secret(self, session_token: str) -> str | None:
        operator = await self.authenticated_operator(session_token)
        now = self._clock()
        if (
            operator is None
            or not operator.is_founder
            or now - operator.reauthenticated_at > REAUTHENTICATION_LIFETIME
            or await self.founder_factor_exists(operator.operator_id)
        ):
            return None
        return self._founder_totp_secret(operator.operator_id)

    async def confirm_totp_enrollment(
        self, *, session_token: str, code: str
    ) -> tuple[str, ...] | None:
        operator = await self.authenticated_operator(session_token)
        now = self._clock()
        if (
            operator is None
            or not operator.is_founder
            or now - operator.reauthenticated_at > REAUTHENTICATION_LIFETIME
            or await self.founder_factor_exists(operator.operator_id)
            or not self._totp_is_valid(operator.operator_id, code)
        ):
            return None
        recovery_codes = tuple(secrets.token_urlsafe(9) for _ in range(8))
        async with self._database.transaction() as connection:
            await connection.execute(
                text(
                    "INSERT INTO founder_totp_factors (operator_id, confirmed_at) "
                    "VALUES (:operator_id, :confirmed_at)"
                ),
                {"confirmed_at": now, "operator_id": operator.operator_id},
            )
            for recovery_code in recovery_codes:
                salt = secrets.token_bytes(16)
                await connection.execute(
                    text(
                        "INSERT INTO auth_recovery_codes "
                        "(recovery_code_id, operator_id, salt, code_hash, created_at) "
                        "VALUES (:code_id, :operator_id, :salt, "
                        ":code_hash, :created_at)"
                    ),
                    {
                        "code_hash": self._recovery_hash(recovery_code, salt),
                        "code_id": uuid4(),
                        "created_at": now,
                        "operator_id": operator.operator_id,
                        "salt": salt,
                    },
                )
            await connection.execute(
                text(
                    "UPDATE browser_sessions SET founder_second_factor_at = :now "
                    "WHERE token_hash = :token_hash"
                ),
                {"now": now, "token_hash": self._digest("session", session_token)},
            )
        return recovery_codes

    async def verify_founder_second_factor(
        self, *, session_token: str, credential: str
    ) -> SecondFactorResult:
        operator = await self.authenticated_operator(session_token)
        if operator is None or not operator.is_founder:
            return SecondFactorResult.INVALID
        now = self._clock()
        attempt_subject = self._digest("factor-session", session_token)
        async with self._database.transaction() as connection:
            await connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended(:subject_hash, 0))"
                ),
                {"subject_hash": attempt_subject},
            )
            attempts = await connection.scalar(
                text(
                    "SELECT count(*) FROM auth_attempts "
                    "WHERE subject_kind = 'factor_session' "
                    "AND subject_hash = :subject_hash "
                    "AND attempted_at > :window_start"
                ),
                {
                    "subject_hash": attempt_subject,
                    "window_start": now - timedelta(minutes=15),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO auth_attempts "
                    "(subject_kind, subject_hash, attempted_at) "
                    "VALUES ('factor_session', :subject_hash, :attempted_at)"
                ),
                {"attempted_at": now, "subject_hash": attempt_subject},
            )
            if not isinstance(attempts, int) or attempts >= 5:
                return SecondFactorResult.RATE_LIMITED

            verified = self._totp_is_valid(operator.operator_id, credential)
            used_recovery_id: UUID | None = None
            if not verified:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT recovery_code_id, salt, code_hash "
                            "FROM auth_recovery_codes WHERE operator_id = :operator_id "
                            "AND used_at IS NULL"
                        ),
                        {"operator_id": operator.operator_id},
                    )
                ).all()
                for row in rows:
                    actual = self._recovery_hash(credential, bytes(row.salt))
                    if hmac.compare_digest(actual, bytes(row.code_hash)):
                        used_recovery_id = row.recovery_code_id
                        verified = True
            if not verified:
                return SecondFactorResult.INVALID
            if used_recovery_id is not None:
                updated = await connection.scalar(
                    text(
                        "UPDATE auth_recovery_codes SET used_at = :now "
                        "WHERE recovery_code_id = :code_id AND used_at IS NULL "
                        "RETURNING recovery_code_id"
                    ),
                    {"code_id": used_recovery_id, "now": now},
                )
                if updated is None:
                    return SecondFactorResult.INVALID
            await connection.execute(
                text(
                    "UPDATE browser_sessions SET founder_second_factor_at = :now "
                    "WHERE token_hash = :token_hash AND revoked_at IS NULL "
                    "AND expires_at > :now"
                ),
                {"now": now, "token_hash": self._digest("session", session_token)},
            )
        return SecondFactorResult.VERIFIED
