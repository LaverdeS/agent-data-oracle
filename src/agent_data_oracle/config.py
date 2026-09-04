import os
import secrets
from urllib.parse import urlsplit

LOCAL_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:54329/agent_data_oracle_test"
)


def database_url_from_environment() -> str:
    return os.environ.get("DATABASE_URL", LOCAL_DATABASE_URL)


def auth_secret_from_environment() -> bytes:
    configured = os.environ.get("AUTH_SECRET")
    if configured is not None:
        return configured.encode()
    if os.environ.get("APP_ENV", "local").casefold() not in {"local", "test"}:
        raise RuntimeError("AUTH_SECRET is required outside local/test environments")
    return secrets.token_bytes(32)


def secure_cookies_from_environment() -> bool:
    return os.environ.get("APP_ENV", "local").casefold() not in {"local", "test"}


def founder_emails_from_environment() -> frozenset[str]:
    return frozenset(
        value.strip().casefold()
        for value in os.environ.get("FOUNDER_EMAILS", "").split(",")
        if value.strip()
    )


def validated_public_origin(value: str, *, require_https: bool) -> str:
    origin = value.rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_ORIGIN must be an HTTP(S) origin without a path")
    if require_https and parsed.scheme != "https":
        raise ValueError("PUBLIC_ORIGIN must use HTTPS outside local/test")
    return origin


def public_origin_from_environment() -> str:
    environment = os.environ.get("APP_ENV", "local").casefold()
    configured = os.environ.get("PUBLIC_ORIGIN")
    if configured is None:
        if environment not in {"local", "test"}:
            raise RuntimeError("PUBLIC_ORIGIN is required outside local/test")
        return "http://127.0.0.1:8080"
    return validated_public_origin(
        configured, require_https=environment not in {"local", "test"}
    )
