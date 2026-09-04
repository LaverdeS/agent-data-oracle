import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any, cast

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from agent_data_oracle.config import database_url_from_environment
from agent_data_oracle.cpsc_source import (
    ImportResult,
    cpsc_source_status,
    import_cpsc_fixture,
    parse_observed_at,
)
from agent_data_oracle.database import Database
from agent_data_oracle.observability import configure_logging
from agent_data_oracle.schema import migrate_to_head
from agent_data_oracle.web import create_app

job_logger = logging.getLogger("agent_data_oracle.job")


def _run_async[Result](coroutine: Coroutine[Any, Any, Result]) -> Result:
    if sys.platform == "win32":
        return asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coroutine)


def _database_url(arguments: argparse.Namespace) -> str:
    return cast(str, arguments.database_url)


async def _serve_application(arguments: argparse.Namespace) -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(database_url=_database_url(arguments)),
            host=cast(str, arguments.host),
            port=cast(int, arguments.port),
            log_config=None,
            access_log=False,
        )
    )
    await server.serve()


def _serve(arguments: argparse.Namespace) -> int:
    configure_logging()
    _run_async(_serve_application(arguments))
    return 0


def _migrate(arguments: argparse.Namespace) -> int:
    configure_logging()
    migrate_to_head(_database_url(arguments))
    return 0


async def _check_database(database_url: str) -> bool:
    database = Database(database_url)
    try:
        return await database.is_ready()
    except SQLAlchemyError:
        return False
    finally:
        await database.close()


def _database_check_job(arguments: argparse.Namespace) -> int:
    configure_logging()
    if _run_async(_check_database(_database_url(arguments))):
        job_logger.info("database_check_completed")
        return 0
    job_logger.error("database_check_failed")
    return 1


def _cpsc_import_fixture_job(arguments: argparse.Namespace) -> int:
    configure_logging()
    result = _run_async(
        import_cpsc_fixture(
            database_url=_database_url(arguments),
            fixture_path=Path(cast(str, arguments.fixture)),
            observed_at=parse_observed_at(cast(str, arguments.observed_at)),
            expected_record_count=cast(int, arguments.expected_record_count),
            source_url=cast(str, arguments.source_url),
        )
    )
    print(json.dumps(result.as_dict(), separators=(",", ":"), sort_keys=True))
    return 0 if isinstance(result, ImportResult) else 1


def _cpsc_status_job(arguments: argparse.Namespace) -> int:
    configure_logging()
    result = _run_async(cpsc_source_status(_database_url(arguments)))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


def _add_database_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        default=database_url_from_environment(),
        help="SQLAlchemy PostgreSQL URL (defaults to DATABASE_URL)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-data-oracle")
    commands = parser.add_subparsers(dest="command", required=True)

    web = commands.add_parser("web", help="run the FastAPI web process")
    _add_database_url(web)
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", default=8080, type=int)
    web.set_defaults(handler=_serve)

    migrate = commands.add_parser("migrate", help="apply database migrations")
    _add_database_url(migrate)
    migrate.set_defaults(handler=_migrate)

    job = commands.add_parser("job", help="run a short-lived named job")
    jobs = job.add_subparsers(dest="job", required=True)
    database_check = jobs.add_parser(
        "database-check", help="verify required PostgreSQL access"
    )
    _add_database_url(database_check)
    database_check.set_defaults(handler=_database_check_job)

    cpsc_import = jobs.add_parser(
        "cpsc-import-fixture",
        help="import one recorded CPSC response as a complete source revision",
    )
    _add_database_url(cpsc_import)
    cpsc_import.add_argument("--fixture", required=True)
    cpsc_import.add_argument("--observed-at", required=True)
    cpsc_import.add_argument("--expected-record-count", required=True, type=int)
    cpsc_import.add_argument(
        "--source-url",
        default="https://www.saferproducts.gov/RestWebServices/Recall",
    )
    cpsc_import.set_defaults(handler=_cpsc_import_fixture_job)

    cpsc_status = jobs.add_parser(
        "cpsc-status", help="report current CPSC revision and last ingestion state"
    )
    _add_database_url(cpsc_status)
    cpsc_status.set_defaults(handler=_cpsc_status_job)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    arguments = build_parser().parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], arguments.handler)
    raise SystemExit(handler(arguments))
