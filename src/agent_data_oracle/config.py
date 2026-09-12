import os
import secrets
from urllib.parse import urlsplit

from agent_data_oracle.preview_access import PreviewAccess, normalized_email_set

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
    return normalized_email_set(os.environ.get("FOUNDER_EMAILS", "").split(","))


def preview_access_from_environment(
    *, founder_emails: frozenset[str], access_secret: str | None = None
) -> PreviewAccess:
    environment = os.environ.get("APP_ENV", "local").casefold()
    secret = access_secret or os.environ.get("PREVIEW_ACCESS_SECRET")
    configured_recipients = os.environ.get("PREVIEW_RECIPIENT_EMAILS")
    if (
        environment in {"local", "test"}
        and secret is None
        and configured_recipients is None
    ):
        return PreviewAccess.disabled()
    recipients = normalized_email_set((configured_recipients or "").split(","))
    if not secret or not recipients:
        raise RuntimeError(
            "PREVIEW_ACCESS_SECRET and PREVIEW_RECIPIENT_EMAILS are required"
        )
    return PreviewAccess.founder_only(
        access_secret=secret,
        recipient_emails=recipients,
        founder_emails=founder_emails,
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


def validated_local_preview_harness(enabled: bool) -> bool:
    environment = os.environ.get("APP_ENV", "local").casefold()
    if enabled and environment not in {"local", "test"}:
        raise RuntimeError("LOCAL_PREVIEW_HARNESS is forbidden outside local/test")
    return enabled


def local_preview_harness_from_environment() -> bool:
    configured = os.environ.get("LOCAL_PREVIEW_HARNESS", "").casefold()
    if configured not in {"", "0", "1", "false", "true"}:
        raise RuntimeError("LOCAL_PREVIEW_HARNESS must be true or false")
    return validated_local_preview_harness(configured in {"1", "true"})


def provider_disclosure_from_environment() -> str:
    environment = os.environ.get("APP_ENV", "local").casefold()
    if environment in {"local", "test"}:
        return (
            "Application data stays in the founder's local development "
            "environment. Sign-in links are captured in memory and are not sent "
            "through an external email provider. GitHub is used for development "
            "and CI without production data. Recorded CPSC fixtures make no live "
            "source request."
        )
    configured = os.environ.get("PROVIDER_DISCLOSURE", "").strip()
    if not configured:
        raise RuntimeError("PROVIDER_DISCLOSURE is required outside local/test")
    return configured
