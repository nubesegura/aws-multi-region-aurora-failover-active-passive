"""Criteria 1.1-1.7 through the API Gateway adapter."""

import base64
import json
from typing import Any

import pytest

from dr_demo.domain.errors import DatabaseBusy
from tests.entrypoints.conftest import REGION, FakeContext, Wiring, post_event

DEPOSIT = {"type": "deposit", "amount": 10.5}


def _call(wiring: Wiring, context: FakeContext, event: dict[str, Any]) -> dict[str, Any]:
    return wiring.controller.create_transaction(event, context)


def _body(result: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result["body"])
    return parsed


def test_created_returns_201_with_the_movement_the_balance_and_the_standard_headers(
    wiring: Wiring, context: FakeContext
) -> None:
    result = _call(wiring, context, post_event(DEPOSIT))

    body = _body(result)
    assert result["statusCode"] == 201
    assert set(body) == {"transaction", "balance", "served_by_region", "database_role", "server_ms"}
    assert body["transaction"] == {
        "id": str(wiring.repository.stored[0].id),
        "type": "deposit",
        "amount": "10.50",
        "balance_after": "10.50",
        "processed_at": "2026-10-04T12:00:00.123Z",
        "processed_region": REGION,
    }
    assert body["balance"] == "10.50"
    assert body["served_by_region"] == REGION
    assert body["database_role"] == "writer"
    assert body["server_ms"] >= 0
    assert len(wiring.repository.stored) == 1
    assert result["headers"]["X-Served-By-Region"] == REGION
    assert result["headers"]["Content-Type"] == "application/json"
    assert "X-Served-By-Region" in result["headers"]["Access-Control-Expose-Headers"]
    assert "Retry-After" in result["headers"]["Access-Control-Expose-Headers"]


def test_withdrawal_returns_the_reduced_balance(wiring: Wiring, context: FakeContext) -> None:
    _call(wiring, context, post_event({"type": "deposit", "amount": 100}))

    result = _call(wiring, context, post_event({"type": "withdrawal", "amount": 0.1}))

    body = _body(result)
    assert result["statusCode"] == 201
    assert body["transaction"]["type"] == "withdrawal"
    assert body["transaction"]["amount"] == "0.10"
    assert body["balance"] == "99.90"


def test_amounts_never_pass_through_float(wiring: Wiring, context: FakeContext) -> None:
    text = '{"type": "deposit", "amount": 1234567890.12}'

    body = _body(_call(wiring, context, post_event(text)))

    assert body["transaction"]["amount"] == "1234567890.12"
    assert body["balance"] == "1234567890.12"


def test_replay_with_same_key_returns_200_and_the_original_result(
    wiring: Wiring, context: FakeContext
) -> None:
    first = _call(wiring, context, post_event(DEPOSIT, {"Idempotency-Key": "abc"}))
    _call(wiring, context, post_event({"type": "deposit", "amount": 5}))

    second = _call(wiring, context, post_event(DEPOSIT, {"idempotency-key": "abc"}))

    assert (first["statusCode"], second["statusCode"]) == (201, 200)
    assert _body(first)["transaction"] == _body(second)["transaction"]
    assert _body(second)["balance"] == "10.50"
    assert len(wiring.repository.stored) == 2


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "not json",
        "[1]",
        '{"type": "deposit", "amount": NaN}',
        '{"type": "deposit", "amount": 1e999999999999999999}',
        '{"type": "deposit", "amount": 1.001}',
        '{"type": "deposit", "amount": 0}',
        '{"type": "deposit", "amount": -3}',
        '{"type": "deposit", "amount": 99999999999.99}',
        '{"type": "transfer", "amount": 1}',
        '{"type": "deposit", "amount": 1, "currency": "USD"}',
        '{"amount": 1}',
        '{"type": "deposit", "amount": "1"}',
    ],
)
def test_invalid_body_returns_400_and_stores_nothing(
    wiring: Wiring, context: FakeContext, body: str | None
) -> None:
    result = _call(wiring, context, post_event(body))

    error = _body(result)["error"]
    assert result["statusCode"] == 400
    assert error["code"] == "validation_error"
    assert error["fields"]
    assert {"field", "message"} == set(error["fields"][0])
    assert wiring.repository.stored == []
    assert result["headers"]["X-Served-By-Region"] == REGION


def test_every_field_error_is_listed(wiring: Wiring, context: FakeContext) -> None:
    result = _call(
        wiring,
        context,
        post_event({"type": "gift", "amount": -1, "extra": 1}, {"Idempotency-Key": "k" * 65}),
    )

    fields = {item["field"] for item in _body(result)["error"]["fields"]}
    assert fields == {"type", "amount", "extra", "Idempotency-Key"}


def test_hostile_encodings_are_a_400_not_a_crash(wiring: Wiring, context: FakeContext) -> None:
    not_utf8 = base64.b64encode(b"\xff\xfe\x00").decode()

    results = [
        _call(wiring, context, post_event(not_utf8, isBase64Encoded=True)),
        _call(wiring, context, post_event("%%%", isBase64Encoded=True)),
        _call(wiring, context, post_event("[" * 100_000)),
        _call(wiring, context, post_event('{"type": "deposit", "amount": ' + "9" * 5000 + "}")),
    ]

    assert [item["statusCode"] for item in results] == [400, 400, 400, 400]


def test_base64_body_is_decoded(wiring: Wiring, context: FakeContext) -> None:
    encoded = base64.b64encode(json.dumps(DEPOSIT).encode()).decode()

    result = _call(wiring, context, post_event(encoded, isBase64Encoded=True))

    assert result["statusCode"] == 201


def test_insufficient_funds_returns_422_with_the_current_balance(
    wiring: Wiring, context: FakeContext
) -> None:
    _call(wiring, context, post_event({"type": "deposit", "amount": 20}))

    result = _call(wiring, context, post_event({"type": "withdrawal", "amount": 20.01}))

    error = _body(result)["error"]
    assert result["statusCode"] == 422
    assert error["code"] == "insufficient_funds"
    assert error["balance"] == "20.00"
    assert error["fields"] == []
    assert len(wiring.repository.stored) == 1
    assert result["headers"]["X-Served-By-Region"] == REGION
    assert "Retry-After" not in result["headers"]


def test_read_only_returns_503_with_retry_after(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.role = "reader"

    result = _call(wiring, context, post_event(DEPOSIT))

    assert result["statusCode"] == 503
    assert _body(result)["error"]["code"] == "database_read_only"
    assert result["headers"]["Retry-After"] == "5"
    assert wiring.repository.stored == []
    assert result["headers"]["X-Served-By-Region"] == REGION


def test_busy_returns_503_with_retry_after(
    wiring: Wiring, context: FakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(_draft: object) -> None:
        raise DatabaseBusy

    monkeypatch.setattr(wiring.repository, "apply_movement", busy)

    result = _call(wiring, context, post_event(DEPOSIT))

    assert result["statusCode"] == 503
    assert _body(result)["error"]["code"] == "database_busy"
    assert result["headers"]["Retry-After"] == "5"


def test_unavailable_returns_503_without_details(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.unavailable = True

    result = _call(wiring, context, post_event(DEPOSIT))

    error = _body(result)["error"]
    assert result["statusCode"] == 503
    assert error["code"] == "database_unavailable"
    assert "Retry-After" not in result["headers"]
    assert error["fields"] == []


def test_unexpected_error_returns_500_without_details(
    wiring: Wiring, context: FakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_draft: object) -> None:
        raise RuntimeError("password=hunter2 host=db.internal")

    monkeypatch.setattr(wiring.repository, "apply_movement", explode)

    result = _call(wiring, context, post_event(DEPOSIT))

    assert result["statusCode"] == 500
    assert _body(result)["error"]["code"] == "internal_error"
    assert "hunter2" not in result["body"]
    assert "db.internal" not in result["body"]
    assert result["headers"]["X-Served-By-Region"] == REGION
