import errno
import json
import logging
import sys
from collections.abc import Mapping
from typing import Any

_EVENT_FIELDS = (
    "correlation_id",
    "request_method",
    "request_path",
    "response_status",
    "target_revision",
)
_SAFE_MESSAGES = frozenset(
    {
        "database_check_completed",
        "database_check_failed",
        "migration_completed",
        "request_completed",
        "sign_in_delivery_failed",
    }
)


class JsonFormatter(logging.Formatter):
    """Format application logs as one redacted JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        event: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": message if message in _SAFE_MESSAGES else "<redacted>",
        }
        event.update(
            {
                field: getattr(record, field)
                for field in _EVENT_FIELDS
                if hasattr(record, field)
            }
        )
        if isinstance(record.msg, OSError):
            event["exception_type"] = type(record.msg).__name__
            if record.msg.errno == errno.EADDRINUSE:
                event["event"] = "address_in_use"
        if record.exc_info is not None:
            exception_type = record.exc_info[0]
            event["exception_type"] = (
                exception_type.__name__ if exception_type is not None else "Exception"
            )
        return json.dumps(event, separators=(",", ":"), sort_keys=True)


def configure_logging(*, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def request_log_fields(
    *, correlation_id: str, method: str, path: str, status: int
) -> Mapping[str, object]:
    return {
        "correlation_id": correlation_id,
        "request_method": method,
        "request_path": path,
        "response_status": status,
    }
