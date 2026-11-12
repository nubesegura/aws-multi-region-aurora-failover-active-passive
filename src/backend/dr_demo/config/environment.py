"""Environment helpers for Cognito authorization configuration."""

import os
from collections.abc import Mapping

COGNITO_ALLOWED_CLIENT_IDS_ENV = "COGNITO_ALLOWED_CLIENT_IDS"


def parse_allowed_client_ids(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated list of allowed Cognito App Client IDs."""
    if not raw:
        return ()
    return tuple(
        item
        for part in raw.split(",")
        if (item := part.strip()) and item.isascii() and not any(ch.isspace() for ch in item)
    )


def get_allowed_client_ids(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Read and parse COGNITO_ALLOWED_CLIENT_IDS from a mapping or os.environ."""
    source = os.environ if env is None else env
    return parse_allowed_client_ids(source.get(COGNITO_ALLOWED_CLIENT_IDS_ENV))
