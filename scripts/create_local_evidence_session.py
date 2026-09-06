"""Create a declared local browser session through the passwordless auth seam."""

import argparse
import asyncio
import os
from urllib.parse import parse_qs, urlparse

from agent_data_oracle.auth import HumanAccess, LocalCaptureEmailProvider
from agent_data_oracle.config import database_url_from_environment
from agent_data_oracle.database import Database


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a local-only session for manually testing evidence queues."
    )
    parser.add_argument("--email", default="local-evidence@example.test")
    return parser.parse_args()


async def create_session(email: str) -> str:
    if os.environ.get("APP_ENV", "local").casefold() not in {"local", "test"}:
        raise RuntimeError("This helper only runs with APP_ENV=local or APP_ENV=test.")
    secret = os.environ.get("AUTH_SECRET")
    if secret is None:
        raise RuntimeError("Set the same local AUTH_SECRET used by the web process.")

    database = Database(database_url_from_environment())
    provider = LocalCaptureEmailProvider()
    access = HumanAccess(
        database=database,
        secret=secret.encode(),
        email_provider=provider,
    )
    try:
        await access.request_sign_in(
            email=email,
            network_identity="local-evidence-session",
            base_url="http://127.0.0.1:8080",
        )
        token = parse_qs(urlparse(provider.deliveries[-1].sign_in_url).query)[
            "token"
        ][0]
        grant = await access.consume_sign_in_token(token)
        if grant is None:
            raise RuntimeError("Could not consume the local sign-in token.")
        await access.record_declaration(
            session_token=grant.token,
            operator_type="business_operator",
            sells_into_us=True,
        )
        return grant.token
    finally:
        await database.close()


def main() -> int:
    arguments = parse_arguments()
    session_token = asyncio.run(
        create_session(arguments.email), loop_factory=asyncio.SelectorEventLoop
    )
    print(session_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
