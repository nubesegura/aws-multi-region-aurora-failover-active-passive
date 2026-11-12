"""Criteria 2.2, 7.1, 7.2 and the movement fields of the list and the statistics."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from dr_demo.domain.transaction import NewTransaction
from tests.entrypoints.conftest import NOW, REGION, FakeContext, Wiring, get_event

MOVEMENT_FIELDS = {"id", "type", "amount", "balance_after", "processed_at", "processed_region"}


def _fill(wiring: Wiring, count: int) -> None:
    """Store alternating deposits of 2.00 and withdrawals of 1.00 in two regions."""
    for index in range(count):
        is_deposit = index % 2 == 0
        new = NewTransaction(
            "deposit" if is_deposit else "withdrawal", Decimal(2 if is_deposit else 1), None
        )
        draft = new.draft(
            UUID(int=index + 1),
            "us-east-2" if is_deposit else "us-west-2",
            NOW + timedelta(seconds=index),
        )
        wiring.repository.apply_movement(draft)


def _body(result: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result["body"])
    return parsed


def test_stats_with_data(wiring: Wiring, context: FakeContext) -> None:
    _fill(wiring, 12)

    result = wiring.controller.get_stats(get_event("/stats"), context)

    body = _body(result)
    assert result["statusCode"] == 200
    assert body["total_count"] == 12
    assert body["counts_by_region"] == {"us-east-2": 6, "us-west-2": 6}
    assert set(body["last_processed_at_by_region"]) == {"us-east-2", "us-west-2"}
    assert len(body["latest_transactions"]) == 10
    assert body["latest_transactions"][0] == {
        "id": str(UUID(int=12)),
        "type": "withdrawal",
        "amount": "1.00",
        "balance_after": "6.00",
        "processed_at": "2026-10-04T12:00:11.123Z",
        "processed_region": "us-west-2",
    }
    assert body["served_by_region"] == REGION
    assert body["database_role"] == "writer"
    assert body["generated_at"] == "2026-10-04T12:00:00.123Z"
    assert result["headers"]["X-Served-By-Region"] == REGION


def test_empty_stats(wiring: Wiring, context: FakeContext) -> None:
    body = _body(wiring.controller.get_stats(get_event("/stats"), context))

    assert body["total_count"] == 0
    assert body["counts_by_region"] == {}
    assert body["latest_transactions"] == []
    assert body["database_role"] == "writer"


def test_stats_with_unreachable_database(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.unavailable = True

    result = wiring.controller.get_stats(get_event("/stats"), context)

    assert result["statusCode"] == 503
    assert _body(result)["error"]["code"] == "database_unavailable"
    assert result["headers"]["X-Served-By-Region"] == REGION


def test_list_defaults_to_20_newest_first_with_the_movement_fields(
    wiring: Wiring, context: FakeContext
) -> None:
    _fill(wiring, 25)

    result = wiring.controller.list_transactions(get_event("/transactions"), context)

    items = _body(result)["items"]
    assert len(items) == 20
    assert set(items[0]) == MOVEMENT_FIELDS
    stamps = [item["processed_at"] for item in items]
    assert stamps == sorted(stamps, reverse=True)


def test_list_honours_limit(wiring: Wiring, context: FakeContext) -> None:
    _fill(wiring, 5)

    result = wiring.controller.list_transactions(
        get_event("/transactions", {"limit": "2"}), context
    )

    assert len(_body(result)["items"]) == 2


@pytest.mark.parametrize("limit", ["0", "101", "abc", ""])
def test_list_rejects_invalid_limit(wiring: Wiring, context: FakeContext, limit: str) -> None:
    result = wiring.controller.list_transactions(
        get_event("/transactions", {"limit": limit}), context
    )

    assert result["statusCode"] == 400
    assert _body(result)["error"]["fields"][0]["field"] == "limit"


def test_account_reports_balance_count_region_and_role(
    wiring: Wiring, context: FakeContext
) -> None:
    _fill(wiring, 3)

    result = wiring.controller.get_account(get_event("/account"), context)

    assert result["statusCode"] == 200
    assert _body(result) == {
        "balance": "3.00",
        "movement_count": 3,
        "served_by_region": REGION,
        "database_role": "writer",
    }
    assert result["headers"]["X-Served-By-Region"] == REGION


def test_account_on_a_reader_reports_the_real_role(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.role = "reader"

    body = _body(wiring.controller.get_account(get_event("/account"), context))

    assert body["database_role"] == "reader"
    assert body["balance"] == "0.00"


def test_account_with_unreachable_database(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.unavailable = True

    result = wiring.controller.get_account(get_event("/account"), context)

    assert result["statusCode"] == 503
    assert _body(result)["error"]["code"] == "database_unavailable"


def test_consistency_of_a_clean_ledger(wiring: Wiring, context: FakeContext) -> None:
    _fill(wiring, 4)

    result = wiring.controller.get_consistency(get_event("/consistency"), context)

    assert result["statusCode"] == 200
    assert _body(result) == {
        "count": 4,
        "total_deposited": "4.00",
        "total_withdrawn": "2.00",
        "replayed_balance": "2.00",
        "stored_balance": "2.00",
        "balances_match": True,
        "chain_ok": True,
        "duplicate_keys": 0,
        "counts_by_region": {"us-east-2": 2, "us-west-2": 2},
        "consistent": True,
        "first_offender": None,
        "truncated": False,
        "served_by_region": REGION,
    }


def test_consistency_names_the_first_offender_when_balances_differ(
    wiring: Wiring, context: FakeContext
) -> None:
    _fill(wiring, 2)
    wiring.repository.balance = Decimal("9.00")

    body = _body(wiring.controller.get_consistency(get_event("/consistency"), context))

    assert body["consistent"] is False
    assert body["balances_match"] is False
    assert body["stored_balance"] == "9.00"
    assert body["first_offender"] == str(wiring.repository.stored[-1].id)


def test_shallow_health_ignores_the_database(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.unavailable = True

    result = wiring.controller.get_health(get_event("/health"), context)

    assert result["statusCode"] == 200
    assert _body(result) == {"status": "ok", "region": REGION}


def test_deep_health_reports_the_role(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.role = "reader"

    result = wiring.controller.get_health(get_event("/health", {"deep": "TRUE"}), context)

    assert result["statusCode"] == 200
    assert _body(result)["database_role"] == "reader"


def test_deep_health_is_503_when_the_database_is_down(wiring: Wiring, context: FakeContext) -> None:
    wiring.repository.unavailable = True

    result = wiring.controller.get_health(get_event("/health", {"deep": "true"}), context)

    assert result["statusCode"] == 503
    assert _body(result)["error"]["code"] == "database_unavailable"
