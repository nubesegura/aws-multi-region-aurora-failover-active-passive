"""Runtime hardening: third-party loggers are capped and secrets never reach a log line."""

import json
import logging

import pytest

from dr_demo.config.logging import JsonFormatter, configure_logging

THIRD_PARTY = ("botocore", "boto3", "urllib3", "pg8000")


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2",
        "passwd: hunter2",
        "pwd=hunter2",
        "token=hunter2",
        "client_secret=hunter2",
        "Authorization: Basic hunter2",
        "Bearer hunter2",
        "key AKIAABCDEFGHIJKLMNOP leaked",
        "key ASIAABCDEFGHIJKLMNOP leaked",
    ],
)
def test_secrets_in_messages_and_stacks_are_redacted(text: str) -> None:
    try:
        raise ValueError(text)
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, text, None, sys.exc_info())

    entry = json.loads(JsonFormatter().format(record))

    for value in (entry["message"], entry["stack"]):
        assert "hunter2" not in value
        assert "ABCDEFGHIJKLMNOP" not in value


def test_sensitive_field_names_and_values_are_redacted() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "request completed", None, None)
    record.fields = {"route": "GET /x", "authorization": "abc", "note": "token=hunter2"}

    entry = json.loads(JsonFormatter().format(record))

    assert entry["route"] == "GET /x"
    assert entry["authorization"] == "[redacted]"
    assert "hunter2" not in entry["note"]


def test_third_party_loggers_are_capped_at_warning() -> None:
    for name in THIRD_PARTY:
        logging.getLogger(name).setLevel(logging.DEBUG)

    configure_logging("DEBUG")

    for name in THIRD_PARTY:
        assert logging.getLogger(name).level == logging.WARNING
    configure_logging("INFO")
