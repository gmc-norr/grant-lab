import json
import logging

import pytest

from call_monitor.logging import JsonFormatter, configure_logging


@pytest.fixture(autouse=True)
def reset_root_handlers():
    """Each test gets a fresh root logger so configure_logging() actually
    re-installs a handler bound to pytest's per-test captured stderr.
    Without this, the idempotent-configure check in configure_logging()
    keeps a stale handler pointing at a previous test's stderr buffer."""
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved)


def _last_json_line(stderr: str) -> dict:
    return json.loads(stderr.strip().splitlines()[-1])


def test_logging_outputs_json(capsys):
    configure_logging()
    log = logging.getLogger("test")
    log.info("hello", extra={"user_id": 42})
    payload = _last_json_line(capsys.readouterr().err)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["user_id"] == 42


def test_extra_cannot_clobber_reserved_keys(capsys):
    """A caller passing `extra={"level": "PWNED"}` must not overwrite the
    real log level — that would let bad code bypass level-based filters.

    Note: stdlib `makeRecord` already rejects `extra={"message": ...}` and
    `extra={"asctime": ...}` with KeyError, so those names can't be tested
    here. `level`, `logger`, `ts`, `exc` are not protected by stdlib and
    would otherwise reach our payload — _RESERVED keeps them out.
    """
    configure_logging()
    log = logging.getLogger("test")
    log.warning(
        "real-message",
        extra={"level": "PWNED", "logger": "fake", "ts": "0", "exc": "spoofed"},
    )
    payload = _last_json_line(capsys.readouterr().err)
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "test"
    assert payload["message"] == "real-message"
    assert payload["ts"] != "0"
    assert "exc" not in payload


def test_configure_logging_is_idempotent():
    """Calling configure_logging() twice must not duplicate handlers nor
    clobber existing ones — important because create_app() invokes it on
    every call and tests may attach their own handlers (e.g. caplog)."""
    configure_logging()
    root = logging.getLogger()
    json_handlers_before = sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter))
    handlers_before = list(root.handlers)

    configure_logging()
    json_handlers_after = sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter))
    assert json_handlers_after == json_handlers_before == 1
    assert root.handlers == handlers_before  # exact same handler instances
