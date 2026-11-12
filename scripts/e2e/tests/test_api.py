import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from dr_drill import api, http_safe
from dr_drill.api import ApiClient, cents
from tests.fakes import FakeOpener

BASE = "https://api.example.invalid"
TOKEN = "fake-id-token-value"
START = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Each call advances 25 ms, so every request lasts exactly 25 ms."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        value = self.now
        self.now += timedelta(milliseconds=25)
        return value


class FakeTransport:
    def __init__(self, status: int = 200, body: Any = None, error: Exception | None = None) -> None:
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        self.error = error
        self.requests: list[tuple[str, str, Mapping[str, str], bytes | None, float]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append((method, url, headers, body, timeout))
        if self.error:
            raise self.error
        return self.status, self.body


def client(transport: FakeTransport) -> ApiClient:
    return ApiClient(BASE + "/", FakeClock(), transport)


POST_ANSWER = {
    "transaction": {"id": "x", "balance_after": "112.50", "processed_region": "us-east-2"},
    "balance": "112.50",
    "served_by_region": "us-east-2",
    "database_role": "writer",
}


def test_created_movement_is_parsed_and_timed() -> None:
    transport = FakeTransport(201, POST_ANSWER)

    sample = client(transport).post_transaction(TOKEN, "deposit", Decimal("0.01"), "key-1")

    assert sample.status == 201
    assert sample.ok
    assert sample.latency_ms == 25.0
    assert sample.start_utc == START
    assert sample.end_utc == START + timedelta(milliseconds=25)
    assert (sample.served_by_region, sample.processed_region, sample.database_role) == (
        "us-east-2",
        "us-east-2",
        "writer",
    )
    assert sample.body_fields == {"balance_cents": 11250}


def test_request_shape_and_timeout() -> None:
    transport = FakeTransport(201, POST_ANSWER)

    client(transport).post_transaction(TOKEN, "withdrawal", Decimal("0.01"), "key-1")

    method, url, headers, body, timeout = transport.requests[0]
    assert (method, url, timeout) == ("POST", BASE + "/transactions", 4.0)
    assert headers["Idempotency-Key"] == "key-1"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert body is not None
    assert json.loads(body) == {"type": "withdrawal", "amount": 0.01}


def test_replay_is_a_two_hundred() -> None:
    sample = client(FakeTransport(200, POST_ANSWER)).post_transaction(
        TOKEN, "deposit", Decimal("0.01"), "key-1"
    )

    assert sample.status == 200
    assert sample.ok


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthorized"), (422, "insufficient_funds"), (503, "database_read_only")],
)
def test_error_answers_are_values_with_the_error_code(status: int, code: str) -> None:
    body = {"error": {"code": code, "message": "x", "fields": []}}

    sample = client(FakeTransport(status, body)).get_account(TOKEN)

    assert sample.status == status
    assert not sample.ok
    assert sample.body_fields == {"error": code}


def test_throttling_without_error_body_is_a_value() -> None:
    sample = client(FakeTransport(429, {"message": "Too Many Requests"})).get_stats(TOKEN)

    assert sample.status == 429
    assert sample.body_fields == {}


def test_timeout_becomes_status_zero_with_the_class_name_only() -> None:
    transport = FakeTransport(error=TimeoutError("https://api.example.invalid/?secret=1"))

    sample = client(transport).get_account(TOKEN)

    assert sample.status == 0
    assert not sample.ok
    assert sample.body_fields == {"error": "TimeoutError"}
    assert "secret" not in repr(sample)


def test_malformed_json_keeps_the_status() -> None:
    sample = client(FakeTransport(200, b"<html>")).get_account(TOKEN)

    assert sample.status == 200
    assert sample.body_fields == {"error": "MalformedBody"}


def test_a_two_hundred_with_a_non_json_body_is_not_ok() -> None:
    sample = client(FakeTransport(200, b"<html>proxy</html>")).get_account(TOKEN)

    assert sample.status == 200
    assert sample.malformed
    assert not sample.ok


def test_a_created_movement_without_a_transaction_is_not_acknowledged() -> None:
    sample = client(FakeTransport(201, {"balance": "1.00"})).post_transaction(
        TOKEN, "deposit", Decimal("0.01"), "key-1"
    )

    assert sample.status == 201
    assert not sample.ok
    assert sample.body_fields["error"] == "MalformedBody"


def test_a_rejected_movement_keeps_its_error_code_and_is_not_marked_malformed() -> None:
    body = {"error": {"code": "insufficient_funds"}}

    sample = client(FakeTransport(422, body)).post_transaction(
        TOKEN, "withdrawal", Decimal("0.01"), "key-1"
    )

    assert sample.body_fields == {"error": "insufficient_funds"}
    assert not sample.malformed


def test_a_deeply_nested_body_is_malformed_not_a_crash() -> None:
    body = b"[" * 200_000 + b"]" * 200_000

    sample = client(FakeTransport(200, body)).get_account(TOKEN)

    assert sample.body_fields == {"error": "MalformedBody"}
    assert not sample.ok


def test_a_json_array_is_malformed() -> None:
    assert client(FakeTransport(200, b"[]")).get_account(TOKEN).body_fields == {
        "error": "MalformedBody"
    }


def test_account_parses_balance_and_movement_count() -> None:
    body = {
        "balance": "5.07",
        "movement_count": 7,
        "served_by_region": "us-west-2",
        "database_role": "writer",
    }

    sample = client(FakeTransport(200, body)).get_account(TOKEN)

    assert sample.body_fields == {"balance_cents": 507, "movement_count": 7}
    assert sample.served_by_region == "us-west-2"


def test_health_deep_has_no_token_and_reads_the_region() -> None:
    transport = FakeTransport(
        200, {"status": "ok", "region": "us-east-2", "database_role": "reader"}
    )

    sample = client(transport).get_health(deep=True)

    _, url, headers, _, _ = transport.requests[0]
    assert url == BASE + "/health?deep=true"
    assert "Authorization" not in headers
    assert (sample.served_by_region, sample.database_role) == ("us-east-2", "reader")


def test_plain_health_url() -> None:
    transport = FakeTransport(200, {"status": "ok", "region": "us-east-2"})

    client(transport).get_health()

    assert transport.requests[0][1] == BASE + "/health"


def test_consistency_fields_and_stats() -> None:
    body = {
        "count": 7,
        "replayed_balance": "112.50",
        "stored_balance": "112.50",
        "balances_match": True,
        "chain_ok": True,
        "duplicate_keys": 0,
        "counts_by_region": {"us-east-2": 5, "us-west-2": 2, "bad name!": 1},
        "consistent": None,
        "truncated": True,
        "first_offender": "ignored",
    }

    fields = client(FakeTransport(200, body)).get_consistency(TOKEN).body_fields

    assert fields == {
        "replayed_balance_cents": 11250,
        "stored_balance_cents": 11250,
        "count": 7,
        "duplicate_keys": 0,
        "truncated": True,
        "chain_ok": True,
        "balances_match": True,
        "counts_by_region": {"us-east-2": 5, "us-west-2": 2},
    }
    stats = client(FakeTransport(200, {"total_count": 42})).get_stats(TOKEN)
    assert stats.body_fields == {"total_count": 42}


def test_unexpected_value_types_are_dropped() -> None:
    body = {"balance": 12.5, "movement_count": True, "counts_by_region": [], "served_by_region": 3}

    sample = client(FakeTransport(200, body)).get_account(TOKEN)

    assert sample.body_fields == {}
    assert sample.served_by_region is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("112.50", 11250), ("0.01", 1), ("7", 700), ("1.5", 150), ("-1.00", None), ("x", None)],
)
def test_cents(text: str, expected: int | None) -> None:
    assert cents(text) == expected


def test_cents_rejects_non_strings() -> None:
    assert cents(1.5) is None


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return b"{}"


def test_urllib_transport_sends_and_returns_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        seen.update(url=request.full_url, method=request.get_method(), timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(http_safe, "_OPENER", FakeOpener(fake_urlopen))

    result = api.urllib_transport("GET", "https://example.invalid/x", {}, None, 4.0)

    assert result == (200, b"{}")
    assert seen == {"url": "https://example.invalid/x", "method": "GET", "timeout": 4.0}


def test_urllib_transport_returns_http_errors_as_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(http_safe, "_OPENER", FakeOpener(fake_urlopen))

    assert api.urllib_transport("GET", "https://example.invalid/", {}, None, 1.0)[0] == 503


def test_urllib_transport_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="https"):
        api.urllib_transport("GET", "http://example.invalid/", {}, None, 1.0)


def test_redirects_are_never_followed() -> None:
    opener = http_safe._OPENER
    request = urllib.request.Request("https://api.example.invalid/", headers={"Authorization": "x"})

    assert not any(
        type(handler) is urllib.request.HTTPRedirectHandler for handler in opener.handlers
    )
    assert (
        http_safe._NoRedirect().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://elsewhere.invalid/",  # type: ignore[arg-type]
        )
        is None
    )


def test_open_url_goes_through_the_no_redirect_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, float]] = []
    monkeypatch.setattr(
        http_safe, "_OPENER", FakeOpener(lambda request, timeout: seen.append((request, timeout)))
    )

    http_safe.open_url("https://api.example.invalid/", 2.0)

    assert seen == [("https://api.example.invalid/", 2.0)]
