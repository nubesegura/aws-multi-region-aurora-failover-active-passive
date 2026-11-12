import json
import logging

import pytest

from dr_demo.config.logging import LOGGER_NAME, JsonFormatter, configure_logging


def test_formatter_emits_one_json_object_with_structured_fields() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "request_completed", None, None)
    record.fields = {"route": "GET /stats", "status": 200}

    entry = json.loads(JsonFormatter().format(record))

    assert entry["message"] == "request_completed"
    assert entry["level"] == "INFO"
    assert entry["route"] == "GET /stats"
    assert entry["timestamp"].endswith("+00:00")


def test_formatter_includes_exception_details_when_present() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())

    entry = json.loads(JsonFormatter().format(record))

    assert entry["exception_type"] == "ValueError"
    assert "boom" in entry["stack"]


def test_configure_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    logger = configure_logging("INFO")
    before = len(logger.handlers)

    again = configure_logging("WARNING")

    assert again is logger
    assert len(again.handlers) == before
    assert again.level == logging.WARNING
    assert again.name == LOGGER_NAME
    assert again.propagate is False
    configure_logging("INFO")
