import json
import logging

from call_monitor.logging import configure_logging


def test_logging_outputs_json(capsys):
    configure_logging()
    log = logging.getLogger("test")
    log.info("hello", extra={"user_id": 42})
    captured = capsys.readouterr()
    # one JSON object per line
    line = captured.err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["user_id"] == 42
