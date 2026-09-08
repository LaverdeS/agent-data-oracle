import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from agent_data_oracle.auth import utc_now
from agent_data_oracle.database import Database

ALLOWED_AGENT_SCOPES = frozenset(
    {
        "queues:submit",
        "queues:read",
        "queues:refresh",
        "evidence:read",
        "reviews:report-agent",
    }
)
MAX_ACTIVE_AGENT_KEYS = 3
REQUESTS_PER_MINUTE = 30


class AgentAuthenticationError(ValueError):
    """A bearer credential is absent, malformed, revoked, or invalid."""


class AgentScopeError(PermissionError):
    """A valid delegated key does not have the requested authority."""


class AgentRateLimitError(RuntimeError):
    """A delegated key has reached its temporary request limit."""


class AgentKeyLimitError(ValueError):
    """The operator already has the permitted active delegated keys."""


@dataclass(frozen=True)
class CreatedAgentKey:
    agent_key_id: UUID
    secret: str
    secret_prefix: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class AgentKeySummary:
    agent_key_id: UUID
    secret_prefix: str
    scopes: tuple[str, ...]
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class AgentPrincipal:
    agent_key_id: UUID
    operator_id: UUID
    scopes: frozenset[str]


class AgentAccess:
    """Own bounded delegated API credentials without storing their secrets."""

    def __init__(
        self, database: Database, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._database = database
        self._clock = clock

    @staticmethod
    def _secret_hash(secret: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            secret.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )

    async def create_key(
        self, *, operator_id: UUID, scopes: frozenset[str]
    ) -> CreatedAgentKey:
        if not scopes or not scopes <= ALLOWED_AGENT_SCOPES:
            raise ValueError("Choose one or more approved agent scopes.")
        now = self._clock()
        ordered_scopes = tuple(sorted(scopes))
        async with self._database.transaction() as connection:
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:operator, 0))"),
                {"operator": f"agent-key:{operator_id}"},
            )
            active_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM agent_api_keys "
                    "WHERE operator_id = :operator_id "
                    "AND revoked_at IS NULL"
                ),
                {"operator_id": operator_id},
            )
            if (
                not isinstance(active_count, int)
                or active_count >= MAX_ACTIVE_AGENT_KEYS
            ):
                raise AgentKeyLimitError("At most three active agent keys are allowed.")
            secret_prefix = secrets.token_hex(6)
            secret = f"ado_{secret_prefix}_{secrets.token_urlsafe(32)}"
            salt = secrets.token_bytes(16)
            key_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO agent_api_keys "
                    "(agent_key_id, operator_id, secret_prefix, secret_salt, "
                    "secret_hash, scopes, created_at) VALUES "
                    "(:agent_key_id, :operator_id, :secret_prefix, :secret_salt, "
                    ":secret_hash, :scopes, :created_at)"
                ),
                {
                    "agent_key_id": key_id,
                    "operator_id": operator_id,
                    "secret_prefix": secret_prefix,
                    "secret_salt": salt,
                    "secret_hash": self._secret_hash(secret, salt),
                    "scopes": list(ordered_scopes),
                    "created_at": now,
                },
            )
        return CreatedAgentKey(key_id, secret, secret_prefix, ordered_scopes)

    async def list_keys(self, *, operator_id: UUID) -> tuple[AgentKeySummary, ...]:
        async with self._database.connection() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT agent_key_id, secret_prefix, scopes, created_at, "
                            "last_used_at FROM agent_api_keys "
                            "WHERE operator_id = :operator_id "
                            "AND revoked_at IS NULL ORDER BY created_at DESC"
                        ),
                        {"operator_id": operator_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            AgentKeySummary(
                agent_key_id=row["agent_key_id"],
                secret_prefix=row["secret_prefix"],
                scopes=tuple(row["scopes"]),
                created_at=row["created_at"],
                last_used_at=row["last_used_at"],
            )
            for row in rows
        )

    async def revoke_key(self, *, operator_id: UUID, agent_key_id: UUID) -> bool:
        async with self._database.transaction() as connection:
            revoked = await connection.scalar(
                text(
                    "UPDATE agent_api_keys SET revoked_at = :now "
                    "WHERE agent_key_id = :agent_key_id AND operator_id = :operator_id "
                    "AND revoked_at IS NULL RETURNING agent_key_id"
                ),
                {
                    "agent_key_id": agent_key_id,
                    "operator_id": operator_id,
                    "now": self._clock(),
                },
            )
        return isinstance(revoked, UUID)

    async def authenticate(
        self, *, bearer_secret: str, required_scope: str
    ) -> AgentPrincipal:
        parts = bearer_secret.split("_", maxsplit=2)
        if len(parts) != 3 or parts[0] != "ado" or len(parts[1]) != 12:
            raise AgentAuthenticationError("invalid bearer credential")
        now = self._clock()
        async with self._database.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT agent_key_id, operator_id, secret_salt, "
                            "secret_hash, scopes "
                            "FROM agent_api_keys WHERE secret_prefix = :secret_prefix "
                            "AND revoked_at IS NULL"
                        ),
                        {"secret_prefix": parts[1]},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or not hmac.compare_digest(
                self._secret_hash(bearer_secret, bytes(row["secret_salt"])),
                bytes(row["secret_hash"]),
            ):
                raise AgentAuthenticationError("invalid bearer credential")
            scopes = frozenset(row["scopes"])
            if required_scope not in scopes:
                raise AgentScopeError("delegated key does not have this scope")
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"agent-rate:{row['agent_key_id']}"},
            )
            attempts = await connection.scalar(
                text(
                    "SELECT count(*) FROM agent_api_request_attempts "
                    "WHERE agent_key_id = :agent_key_id AND attempted_at > :window"
                ),
                {
                    "agent_key_id": row["agent_key_id"],
                    "window": now - timedelta(minutes=1),
                },
            )
            if not isinstance(attempts, int) or attempts >= REQUESTS_PER_MINUTE:
                raise AgentRateLimitError("agent request limit reached")
            await connection.execute(
                text(
                    "INSERT INTO agent_api_request_attempts "
                    "(agent_key_id, attempted_at) "
                    "VALUES (:agent_key_id, :attempted_at)"
                ),
                {"agent_key_id": row["agent_key_id"], "attempted_at": now},
            )
            await connection.execute(
                text(
                    "UPDATE agent_api_keys SET last_used_at = :now "
                    "WHERE agent_key_id = :agent_key_id"
                ),
                {"agent_key_id": row["agent_key_id"], "now": now},
            )
        return AgentPrincipal(row["agent_key_id"], row["operator_id"], scopes)
