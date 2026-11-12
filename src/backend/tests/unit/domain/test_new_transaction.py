"""Criteria 1.3 and 1.4: validation of the movement payload."""

from decimal import Decimal
from typing import Any

import pytest

from dr_demo.domain.errors import ValidationError
from dr_demo.domain.transaction import NewTransaction


def _fields(error: ValidationError) -> set[str]:
    return {item.field for item in error.fields}


def _payload(**overrides: Any) -> dict[str, Any]:
    return {"type": "deposit", "amount": Decimal("10.50")} | overrides


@pytest.mark.parametrize("movement_type", ["deposit", "withdrawal"])
def test_valid_payload(movement_type: str) -> None:
    new = NewTransaction.from_payload(_payload(type=movement_type), "key-1")

    assert new.type == movement_type
    assert new.amount == Decimal("10.50")
    assert new.idempotency_key == "key-1"


def test_valid_payload_without_idempotency_key() -> None:
    assert NewTransaction.from_payload({"type": "deposit", "amount": 5}, None).amount == Decimal(5)


def test_float_amount_is_converted_without_binary_noise() -> None:
    assert NewTransaction.from_payload(_payload(amount=0.1), None).amount == Decimal("0.1")


@pytest.mark.parametrize("amount", [Decimal("0.01"), 1, Decimal("9999999999.99"), Decimal("1.5")])
def test_boundary_amounts_are_valid(amount: Any) -> None:
    NewTransaction.from_payload(_payload(amount=amount), None)


@pytest.mark.parametrize(
    "amount",
    [
        0,
        Decimal("0.00"),
        -1,
        Decimal("-0.01"),
        Decimal("1.001"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        float("nan"),
        float("inf"),
        Decimal("1E+999999"),
        Decimal("1E+999999999999999999"),
        Decimal("-1E+999999999999999999"),
        Decimal("1E-999999999999999999"),
        Decimal("-1E-999999999999999999"),
        Decimal("10000000000"),
        Decimal("10000000000.00"),
        "10.00",
        True,
        None,
        [1],
    ],
)
def test_invalid_amount(amount: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload(_payload(amount=amount), None)

    assert _fields(caught.value) == {"amount"}


@pytest.mark.parametrize(
    "movement_type", ["transfer", "Deposit", "deposit ", "", None, 1, ["deposit"], {"a": 1}]
)
def test_invalid_type(movement_type: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload(_payload(type=movement_type), None)

    assert _fields(caught.value) == {"type"}


def test_missing_type_and_amount() -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload({}, None)

    assert _fields(caught.value) == {"type", "amount"}


def test_the_old_currency_and_description_fields_are_unknown() -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload(_payload(currency="USD", description="coffee"), None)

    assert _fields(caught.value) == {"currency", "description"}


def test_server_owned_fields_are_rejected_by_name() -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload(_payload(id="x", balance_after=1), None)

    assert _fields(caught.value) == {"id", "balance_after"}


@pytest.mark.parametrize("payload", [[], "text", 5, None])
def test_payload_must_be_an_object(payload: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload(payload, None)

    assert _fields(caught.value) == {"body"}


@pytest.mark.parametrize("key", ["", "k" * 65])
def test_invalid_idempotency_key(key: str) -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload(_payload(), key)

    assert _fields(caught.value) == {"Idempotency-Key"}


@pytest.mark.parametrize("key", ["k", "k" * 64])
def test_idempotency_key_length_boundaries(key: str) -> None:
    assert NewTransaction.from_payload(_payload(), key).idempotency_key == key


def test_all_errors_are_reported_together() -> None:
    with pytest.raises(ValidationError) as caught:
        NewTransaction.from_payload({"type": "x", "amount": -1, "extra": 1}, "")

    assert _fields(caught.value) == {"type", "amount", "extra", "Idempotency-Key"}
    assert all(item.message for item in caught.value.fields)
