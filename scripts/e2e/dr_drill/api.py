"""HTTP client of the banking API with timing: every call returns a `Sample`, never raises."""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dr_drill.http_safe import open_url

REQUEST_TIMEOUT_SECONDS = 4.0
STATUS_TRANSPORT_ERROR = 0
HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300
MAX_BODY_BYTES = 1_000_000

_SHORT_NAME = re.compile(r"[a-z0-9_-]{1,32}")
_AMOUNT = re.compile(r"\d{1,12}(?:\.\d{1,2})?")

FieldValue = str | int | bool | None | dict[str, int]
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class Sample:
    """One timed request: no header, no token; `body_fields` is a whitelist of the answer."""

    start_utc: datetime
    end_utc: datetime
    status: int
    latency_ms: float
    served_by_region: str | None = None
    processed_region: str | None = None
    database_role: str | None = None
    body_fields: dict[str, FieldValue] = field(default_factory=dict)
    malformed: bool = False

    @property
    def ok(self) -> bool:
        """True for a 2xx answer with a usable body (a 2xx proxy page is not an API answer)."""
        return HTTP_OK <= self.status < HTTP_MULTIPLE_CHOICES and not self.malformed


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    """Send the request over HTTPS (no redirects followed); HTTP errors and 3xx are values."""
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310
    try:
        with open_url(request, timeout) as response:
            return int(response.status), bytes(response.read(MAX_BODY_BYTES))
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_BODY_BYTES)


def cents(value: object) -> int | None:
    """Integer cents of a decimal string such as "112.50"; None when it is not an amount."""
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value):
        return None
    try:
        return int((Decimal(value) * 100).to_integral_value())
    except InvalidOperation:
        return None


def _short(value: object) -> str | None:
    return value if isinstance(value, str) and _SHORT_NAME.fullmatch(value) else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _region_counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {
        name: number
        for name, number in value.items()
        if _short(name) is not None and _count(number) is not None
    }


def _put(fields: dict[str, FieldValue], name: str, value: FieldValue) -> None:
    if value is not None:
        fields[name] = value


def _extract_fields(body: Mapping[str, Any]) -> dict[str, FieldValue]:
    """Whitelist of the answer fields the drill needs; amounts become integer cents."""
    fields: dict[str, FieldValue] = {}
    _put(fields, "balance_cents", cents(body.get("balance")))
    _put(fields, "replayed_balance_cents", cents(body.get("replayed_balance")))
    _put(fields, "stored_balance_cents", cents(body.get("stored_balance")))
    for name in ("movement_count", "total_count", "count", "duplicate_keys"):
        _put(fields, name, _count(body.get(name)))
    for name in ("consistent", "truncated", "chain_ok", "balances_match"):
        _put(fields, name, _flag(body.get(name)))
    _put(fields, "counts_by_region", _region_counts(body.get("counts_by_region")))
    error = body.get("error")
    if isinstance(error, dict):
        _put(fields, "error", _short(error.get("code")))
    return fields


def _parse(body: bytes) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _processed_region(body: Mapping[str, Any]) -> str | None:
    transaction = body.get("transaction")
    return _short(transaction.get("processed_region")) if isinstance(transaction, dict) else None


class ApiClient:
    """Adapter over the API: timeouts of 4 seconds, 429 and 5xx are values, headers never leave."""

    def __init__(
        self, base_url: str, clock: Clock, transport: Transport = urllib_transport
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._transport = transport

    def post_transaction(
        self, token: str, kind: str, amount: Decimal, idempotency_key: str
    ) -> Sample:
        """POST /transactions with a deposit or withdrawal; `amount` has two decimals."""
        payload = f'{{"type": {json.dumps(kind)}, "amount": {amount:.2f}}}'.encode()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        return self._send("POST", "/transactions", headers, payload, required_field="transaction")

    def get_account(self, token: str) -> Sample:
        """GET /account."""
        return self._send("GET", "/account", self._bearer(token))

    def get_stats(self, token: str) -> Sample:
        """GET /stats."""
        return self._send("GET", "/stats", self._bearer(token))

    def get_consistency(self, token: str) -> Sample:
        """GET /consistency."""
        return self._send("GET", "/consistency", self._bearer(token))

    def get_health(self, deep: bool = False) -> Sample:
        """GET /health, with the database role when `deep` (no token needed)."""
        query = "?" + urllib.parse.urlencode({"deep": "true"}) if deep else ""
        return self._send("GET", "/health" + query, {})

    @staticmethod
    def _bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        required_field: str | None = None,
    ) -> Sample:
        started = self._clock()
        try:
            status, raw = self._transport(
                method, self._base_url + path, headers, body, REQUEST_TIMEOUT_SECONDS
            )
        except (OSError, http.client.HTTPException, ValueError) as error:
            return self._finish(started, STATUS_TRANSPORT_ERROR, {"error": type(error).__name__})
        parsed = _parse(raw)
        if parsed is None:
            return self._finish(started, status, {"error": "MalformedBody"}, malformed=True)
        succeeded = HTTP_OK <= status < HTTP_MULTIPLE_CHOICES
        if succeeded and required_field and not isinstance(parsed.get(required_field), dict):
            fields = _extract_fields(parsed)
            fields.setdefault("error", "MalformedBody")
            return self._finish(started, status, fields, parsed, malformed=True)
        return self._finish(started, status, _extract_fields(parsed), parsed)

    def _finish(
        self,
        started: datetime,
        status: int,
        fields: dict[str, FieldValue],
        body: Mapping[str, Any] | None = None,
        malformed: bool = False,
    ) -> Sample:
        ended = self._clock()
        served = processed = role = None
        if body is not None:
            served = _short(body.get("served_by_region")) or _short(body.get("region"))
            processed = _processed_region(body)
            role = _short(body.get("database_role"))
        return Sample(
            start_utc=started,
            end_utc=ended,
            status=status,
            latency_ms=(ended - started).total_seconds() * 1000.0,
            served_by_region=served,
            processed_region=processed,
            database_role=role,
            body_fields=fields,
            malformed=malformed,
        )
