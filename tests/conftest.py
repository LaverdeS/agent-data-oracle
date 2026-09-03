import asyncio
from collections.abc import Callable, Mapping

from pytest import Config, Item


def pytest_asyncio_loop_factories(
    config: Config, item: Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    del config, item
    return {"selector": asyncio.SelectorEventLoop}
