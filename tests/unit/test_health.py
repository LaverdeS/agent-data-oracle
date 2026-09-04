import errno
import io
import json
import logging
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from agent_data_oracle.observability import JsonFormatter
from agent_data_oracle.web import create_app


@pytest.mark.asyncio
async def test_live_reports_process_liveness_without_database_access(
    unavailable_database_url: str,
) -> None:
    app = create_app(database_url=unavailable_database_url)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


@pytest.mark.asyncio
async def test_requests_log_json_with_generated_correlation_ids(
    unavailable_database_url: str,
) -> None:
    app = create_app(database_url=unavailable_database_url)
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
                "/path-secret?identifier=query-secret",
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
        "request_path": "<unmatched>",
        "response_status": 404,
    }
    assert len(correlation_id) == 36
    assert correlation_id != "caller-controlled"
    assert "path-secret" not in output.getvalue()
    assert "query-secret" not in output.getvalue()
    assert "body-secret" not in output.getvalue()


def test_json_logs_redact_exception_messages() -> None:
    try:
        raise RuntimeError("submitted-secret")
    except RuntimeError:
        record = logging.LogRecord(
            name="agent_data_oracle.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request_failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    output = JsonFormatter().format(record)

    assert json.loads(output)["exception_type"] == "RuntimeError"
    assert "submitted-secret" not in output


def test_json_logs_redact_arbitrary_messages() -> None:
    record = logging.LogRecord(
        name="external.library",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failure included submitted-secret",
        args=(),
        exc_info=None,
    )

    output = JsonFormatter().format(record)

    assert json.loads(output)["message"] == "<redacted>"
    assert "submitted-secret" not in output


def test_json_logs_name_address_in_use_without_exposing_details() -> None:
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=OSError(errno.EADDRINUSE, "submitted-secret"),
        args=(),
        exc_info=None,
    )

    output = JsonFormatter().format(record)

    assert json.loads(output) == {
        "event": "address_in_use",
        "exception_type": "OSError",
        "level": "ERROR",
        "logger": "uvicorn.error",
        "message": "<redacted>",
    }
    assert "submitted-secret" not in output


@pytest.mark.asyncio
async def test_ready_reports_unavailable_when_postgresql_cannot_be_reached(
    unavailable_database_url: str,
) -> None:
    app = create_app(database_url=unavailable_database_url)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "x-correlation-id" in response.headers
