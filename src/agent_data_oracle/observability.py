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


class JsonFormatter(logging.Formatter):
    """Format application logs as one redacted JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event.update(
            {
                field: getattr(record, field)
                for field in _EVENT_FIELDS
                if hasattr(record, field)
            }
        )
        if record.exc_info is not None:
            event["exception"] = self.formatException(record.exc_info)
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
