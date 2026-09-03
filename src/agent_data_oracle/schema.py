import logging

from alembic import command
from alembic.config import Config

schema_logger = logging.getLogger("agent_data_oracle.schema")


def migrate_to_head(database_url: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    schema_logger.info("migration_completed", extra={"target_revision": "head"})
