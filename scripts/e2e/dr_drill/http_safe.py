"""HTTPS opener shared by the urllib adapters: redirects are never followed.

`urllib` follows redirects by default and keeps the `Authorization` header on the new request,
which could send a token to another host (or over plain HTTP). Here a 3xx answer is a value.
"""

from __future__ import annotations

import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so the 3xx answer surfaces as an `HTTPError` value."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(request: urllib.request.Request | str, timeout: float) -> Any:
    """Open an HTTPS request without following redirects."""
    return _OPENER.open(request, timeout=timeout)
