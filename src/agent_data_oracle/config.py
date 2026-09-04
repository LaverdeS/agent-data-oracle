import os
import secrets

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
