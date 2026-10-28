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
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
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


# Loggers held at WARNING whatever the root level, because at INFO they are
# pure volume and say nothing an operator acts on:
#
#   * apscheduler logs "Running job" / "executed successfully" for every tick
#     of every job — measured at 32 MB/day (review 2026-09-02 §2.6h);
#   * httpx logs one "HTTP Request: POST .../api/embed" line per call, and
#     httpcore narrates the connection underneath it. Review 2026-10-26 A4
#     captured the operator's whole surviving log window — 635 lines, about
#     3 hours — and 419 of them (66%) were those httpx lines. The embedding
#     client alone drowned the log the scheduler and ranking worker write to.
#
# A real HTTP failure is not lost: errors reach the app's own loggers (the
# ranking worker, the embedding client) with the context that matters, and
# httpx's own warnings and above still pass. Pinned by
# tests/test_logging_level.py, whose positive control asserts that app loggers
# still emit at INFO — a configure_logging that quieted everything would
# satisfy "httpx is quiet" and destroy the logs.
QUIET_LOGGERS: tuple[str, ...] = ("apscheduler", "httpx", "httpcore")


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install a JsonFormatter stdout handler on the root logger.

    `level` may be a logging constant or a level NAME (Settings.log_level). The
    loggers in QUIET_LOGGERS are pinned to WARNING (or the root level, if that
    is higher).

    Writes to stdout to match the v1 design spec's observability contract
    (`Structured JSON logs to stdout`). Most container runtimes capture
    both streams, but standardizing on stdout avoids platform-specific
    routing quirks (e.g. Kubernetes treating stderr as an "errors" stream).

    Idempotent: if a `JsonFormatter` handler is already attached, only the
    log level is updated and existing handlers are left in place. This
    matters because `create_app()` calls `configure_logging()` on every
    invocation; without this guard, each fresh app wipes the root logger's
    handlers, which would silently drop records from `caplog` or any other
    test-side handler attached between calls.
    """
    if isinstance(level, str):
        level = logging.getLevelName(level.strip().upper())
        if not isinstance(level, int):
            raise ValueError(f"unknown log level name: {level!r}")
    root = logging.getLogger()
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, level))
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
