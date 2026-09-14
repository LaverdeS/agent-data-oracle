from datetime import UTC, datetime

import pytest

from agent_data_oracle.cpsc_source import (
    CpscRefreshMode,
    CpscRetrievalError,
    build_cpsc_recall_url,
    retrieve_cpsc_response,
)


def test_daily_recall_url_uses_a_seven_day_publish_overlap() -> None:
    url = build_cpsc_recall_url(
        CpscRefreshMode.DAILY,
        last_completed_at=datetime(2026, 9, 14, 12, 30, tzinfo=UTC),
    )

    assert url == (
        "https://www.saferproducts.gov/RestWebServices/Recall?"
        "LastPublishDateStart=2026-09-07T12%3A30%3A00Z&format=json"
    )


@pytest.mark.asyncio
async def test_transient_retrieval_retries_at_most_three_times() -> None:
    attempts: list[str] = []
    waits: list[float] = []

    def fetch(url: str, timeout_seconds: float) -> bytes:
        del timeout_seconds
        attempts.append(url)
        if len(attempts) < 3:
            raise TimeoutError("CPSC did not respond")
        return b"[]"

    response = await retrieve_cpsc_response(
        "https://www.saferproducts.gov/RestWebServices/Recall?format=json",
        fetch=fetch,
        sleep=waits.append,
        jitter=lambda: 0.0,
    )

    assert response == b"[]"
    assert len(attempts) == 3
    assert waits == [1.0, 2.0]


@pytest.mark.asyncio
async def test_permanent_retrieval_failure_is_not_retried() -> None:
    attempts: list[str] = []

    def fetch(url: str, timeout_seconds: float) -> bytes:
        del timeout_seconds
        attempts.append(url)
        raise CpscRetrievalError("http_404", transient=False)

    with pytest.raises(CpscRetrievalError, match="http_404"):
        await retrieve_cpsc_response(
            "https://www.saferproducts.gov/RestWebServices/Recall?format=json",
            fetch=fetch,
            sleep=lambda seconds: None,
            jitter=lambda: 0.0,
        )

    assert len(attempts) == 1
