import io
import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from agent_data_oracle.observability import JsonFormatter
from agent_data_oracle.web import create_app


@pytest.mark.asyncio
async def test_live_reports_process_liveness_without_database_access() -> None:
    app = create_app(
        database_url="postgresql+psycopg://unavailable:unavailable@127.0.0.1:1/unavailable"
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


@pytest.mark.asyncio
async def test_requests_log_json_with_generated_correlation_ids() -> None:
    app = create_app(
        database_url="postgresql+psycopg://unavailable:unavailable@127.0.0.1:1/unavailable"
    )
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter())
    request_logger = logging.getLogger("agent_data_oracle.http")
    request_logger.addHandler(handler)
    request_logger.setLevel(logging.INFO)
    request_logger.propagate = False

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/missing?identifier=query-secret",
                content="body-secret",
                headers={"X-Correlation-ID": "caller-controlled"},
            )
    finally:
        request_logger.removeHandler(handler)
        request_logger.propagate = True

    event = json.loads(output.getvalue())
    correlation_id = response.headers["x-correlation-id"]
    assert event == {
        "correlation_id": correlation_id,
        "level": "INFO",
        "logger": "agent_data_oracle.http",
        "message": "request_completed",
        "request_method": "POST",
        "request_path": "/missing",
        "response_status": 404,
    }
    assert len(correlation_id) == 36
    assert correlation_id != "caller-controlled"
    assert "query-secret" not in output.getvalue()
    assert "body-secret" not in output.getvalue()


@pytest.mark.asyncio
async def test_ready_reports_unavailable_when_postgresql_cannot_be_reached() -> None:
    app = create_app(
        database_url="postgresql+psycopg://unavailable:unavailable@127.0.0.1:1/unavailable"
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "x-correlation-id" in response.headers
