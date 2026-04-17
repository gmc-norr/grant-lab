import json
import logging
import sys
from datetime import UTC, datetime

# Top-level keys we set explicitly in `format()`. User-supplied `extra={}`
# fields with these names would otherwise overwrite them — including `level`,
# which would let a careless caller bypass log-level filters or audit assertions.
_RESERVED = {"ts", "level", "logger", "message", "exc"}


class JsonFormatter(logging.Formatter):
    # Stdlib LogRecord attrs we should not surface as user fields.
    _STANDARD = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._STANDARD or key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install a JsonFormatter stderr handler on the root logger.

    Idempotent: if a `JsonFormatter` handler is already attached, only the
    log level is updated and existing handlers are left in place. This
    matters because `create_app()` calls `configure_logging()` on every
    invocation; without this guard, each fresh app wipes the root logger's
    handlers, which would silently drop records from `caplog` or any other
    test-side handler attached between calls.
    """
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
