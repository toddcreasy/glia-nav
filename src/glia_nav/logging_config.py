import json
import logging
import os
import sys
from datetime import UTC, datetime

RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    # uvicorn attaches an ANSI-coloured copy of its own message; it is not a log field.
    "color_message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Anything passed as logger.info(..., extra={...}) lands as a top-level field.
        payload.update({k: v for k, v in record.__dict__.items() if k not in RESERVED})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))

    # uvicorn installs its own handlers; drop them so every line is JSON on one stream.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # App Runner probes /health every 10 seconds, which is ~8,600 access lines a day
    # that bury everything worth reading. Keep the errors, drop the successes.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
