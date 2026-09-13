"""Shared browser guards for the local founder-preview checks."""

from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Route


def require(condition: bool, outcome: str) -> None:
    if not condition:
        raise RuntimeError(outcome)


def allow_only_origin(context: BrowserContext, base_url: str) -> list[str]:
    expected = urlsplit(base_url)
    blocked: list[str] = []

    def guard(route: Route) -> None:
        destination = urlsplit(route.request.url)
        if (destination.scheme, destination.netloc) == (
            expected.scheme,
            expected.netloc,
        ):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    context.route("**/*", guard)
    return blocked
