"""Criterion 1.1 (ms precision, region) and 2.3/2.4 (limit parsing)."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from dr_demo.domain.errors import ValidationError
from dr_demo.domain.transaction import (
    DEFAULT_LIMIT,
    NewTransaction,
    Transaction,
    parse_limit,
)

_ID = UUID("00000000-0000-4000-8000-000000000001")


def test_draft_is_stamped_with_region_and_millisecond_utc_time() -> None:
    new = NewTransaction("withdrawal", Decimal("1.50"), "key")
    local = timezone(timedelta(hours=-5))
    moment = datetime(2026, 10, 4, 7, 0, 0, 123_987, tzinfo=local)

    draft = new.draft(_ID, "us-east-2", moment)

    assert draft.id == _ID
    assert draft.type == "withdrawal"
    assert draft.amount == Decimal("1.50")
    assert draft.processed_region == "us-east-2"
    assert draft.processed_at == datetime(2026, 10, 4, 12, 0, 0, 123_000, tzinfo=UTC)
    assert draft.idempotency_key == "key"


def test_completed_draft_is_a_transaction_with_the_balance_it_produced() -> None:
    moment = datetime(2026, 10, 4, 12, tzinfo=UTC)
    draft = NewTransaction("deposit", Decimal(5), None).draft(_ID, "us-east-2", moment)

    stored = draft.completed(Decimal("12.00"))

    assert stored == Transaction(
        _ID, "deposit", Decimal(5), Decimal("12.00"), "us-east-2", moment, None
    )


def test_naive_time_is_rejected() -> None:
    new = NewTransaction("deposit", Decimal(1), None)

    with pytest.raises(ValueError, match="timezone"):
        new.draft(_ID, "us-east-2", datetime(2026, 10, 4))


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, DEFAULT_LIMIT), ("1", 1), ("100", 100), ("20", 20)]
)
def test_valid_limit(raw: str | None, expected: int) -> None:
    assert parse_limit(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["0", "101", "-1", "abc", "", "1.5", " 5x", "\N{FULLWIDTH DIGIT ONE}\N{FULLWIDTH DIGIT TWO}"],
)
def test_invalid_limit(raw: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_limit(raw)

    assert [item.field for item in caught.value.fields] == ["limit"]
