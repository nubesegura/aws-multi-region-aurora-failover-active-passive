"""Structured JSON logging for Lambda."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "dr_demo"
REDACTED = "[redacted]"
_FIELDS_ATTRIBUTE = "fields"
_THIRD_PARTY_LOGGERS = ("botocore", "boto3", "urllib3", "pg8000")
_SENSITIVE_WORDS = "password|passwd|pwd|token|secret|bearer|authorization"
_SENSITIVE_NAME = re.compile(_SENSITIVE_WORDS, re.IGNORECASE)
_SECRET_TEXT = re.compile(
    rf"((?:{_SENSITIVE_WORDS})\w*[\"']?\s*[:=]\s*(?:basic\s+|bearer\s+)?)[^\s,;\"'}}]+"
    r"|(bearer\s+)\S+"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Mask credentials, bearer tokens and AWS access key ids inside free text."""

    def mask(match: re.Match[str]) -> str:
        prefix = match.group(1) or match.group(2)
        return f"{prefix}{REDACTED}" if prefix else REDACTED

    return _SECRET_TEXT.sub(mask, text)


def _safe_field(name: str, value: Any) -> Any:
    if _SENSITIVE_NAME.search(name):
        return REDACTED
    return redact(value) if isinstance(value, str) else value


class JsonFormatter(logging.Formatter):
    """One JSON object per line; structured values come from ``extra={"fields": {...}}``."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "message": redact(record.getMessage()),
        }
        fields: dict[str, Any] = getattr(record, _FIELDS_ATTRIBUTE, {})
        entry.update({name: _safe_field(name, value) for name, value in fields.items()})
        if record.exc_info:
            entry["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            entry["stack"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> logging.Logger:
    """Attach one JSON handler to the application logger (idempotent) and set its level.

    The level applies to the application logger only; client libraries stay at WARNING.
    """
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not any(isinstance(handler.formatter, JsonFormatter) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
