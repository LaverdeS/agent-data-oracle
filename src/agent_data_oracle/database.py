from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


class Database:
    """Own the application's bounded asynchronous PostgreSQL connection pool."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=0,
            pool_timeout=5,
            connect_args={"connect_timeout": 2},
        )

    async def is_ready(self) -> bool:
        async with self._engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            return cast(int, result.scalar_one()) == 1

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.begin() as connection:
            yield connection

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.connect() as connection:
            yield connection

    async def close(self) -> None:
        await self._engine.dispose()
