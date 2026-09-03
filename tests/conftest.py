import asyncio
from collections.abc import Callable, Mapping

import pytest
from pytest import Config, Item


@pytest.fixture
def unavailable_database_url() -> str:
    return "postgresql+psycopg://unavailable:unavailable@127.0.0.1:1/unavailable"


def pytest_asyncio_loop_factories(
    config: Config, item: Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    del config, item
    return {"selector": asyncio.SelectorEventLoop}
