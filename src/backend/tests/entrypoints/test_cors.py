"""Criterion 4.7 and assumption A16: the response echoes an allow-listed Origin and nothing else."""

from typing import Any

import pytest

from tests.entrypoints.conftest import (
    BANK_ORIGIN,
    DASHBOARD_ORIGIN,
    FOREIGN_ORIGIN,
    FakeContext,
    Wiring,
    get_event,
    post_event,
)

ALLOW = "Access-Control-Allow-Origin"


@pytest.mark.parametrize("origin", [BANK_ORIGIN, DASHBOARD_ORIGIN])
def test_an_allowed_origin_is_echoed_with_vary(
    wiring: Wiring, context: FakeContext, origin: str
) -> None:
    result = wiring.controller.get_account(get_event("/account", origin=origin), context)

    assert result["headers"][ALLOW] == origin
    assert result["headers"]["Vary"] == "Origin"


@pytest.mark.parametrize(
    "origin",
    [
        FOREIGN_ORIGIN,
        "http://bank.example.com",
        "https://bank.example.com/",
        "https://BANK.example.com.evil.net",
        "*",
        "null",
        "",
    ],
)
def test_any_other_origin_gets_no_allow_header(
    wiring: Wiring, context: FakeContext, origin: str
) -> None:
    result = wiring.controller.get_account(get_event("/account", origin=origin), context)

    assert ALLOW not in result["headers"]
    assert result["headers"]["Vary"] == "Origin"
    assert result["headers"]["X-Served-By-Region"]


def test_a_request_without_origin_gets_no_allow_header(
    wiring: Wiring, context: FakeContext
) -> None:
    result = wiring.controller.get_account(get_event("/account"), context)

    assert ALLOW not in result["headers"]
    assert result["headers"]["Vary"] == "Origin"


def test_the_origin_header_name_is_case_insensitive(wiring: Wiring, context: FakeContext) -> None:
    event = get_event("/account")
    event["headers"] = {"oRiGiN": BANK_ORIGIN}

    result = wiring.controller.get_account(event, context)

    assert result["headers"][ALLOW] == BANK_ORIGIN


def _post(wiring: Wiring, context: FakeContext, origin: str) -> dict[str, Any]:
    event = post_event({"type": "deposit", "amount": 1}, {"Origin": origin})
    return wiring.controller.create_transaction(event, context)


@pytest.mark.parametrize("deposit_ok", [True, False])
def test_errors_follow_the_same_rule(
    wiring: Wiring, context: FakeContext, deposit_ok: bool
) -> None:
    wiring.repository.unavailable = not deposit_ok

    allowed = _post(wiring, context, BANK_ORIGIN)
    foreign = _post(wiring, context, FOREIGN_ORIGIN)

    assert allowed["headers"][ALLOW] == BANK_ORIGIN
    assert ALLOW not in foreign["headers"]
    assert allowed["statusCode"] == (201 if deposit_ok else 503)
