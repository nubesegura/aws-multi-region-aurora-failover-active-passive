"""Criteria 1.1, 1.2, 1.5, 1.6 and 7.5 at use case level."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from dr_demo.application.use_cases.create_transaction import CreateTransaction
from dr_demo.domain.errors import (
    DatabaseReadOnly,
    DatabaseUnavailable,
    InsufficientFunds,
    ValidationError,
)
from tests.fakes.in_memory import (
    FixedClock,
    FixedRegionProvider,
    InMemoryTransactionRepository,
    SequentialIdGenerator,
)

DEPOSIT = {"type": "deposit", "amount": Decimal("100.00")}
WITHDRAWAL = {"type": "withdrawal", "amount": Decimal("30.25")}


@pytest.fixture
def use_case(
    repository: InMemoryTransactionRepository,
    clock: FixedClock,
    ids: SequentialIdGenerator,
    region: FixedRegionProvider,
) -> CreateTransaction:
    return CreateTransaction(repository, clock, ids, region)


def test_deposit_then_withdrawal_give_the_expected_balances(
    use_case: CreateTransaction, repository: InMemoryTransactionRepository
) -> None:
    deposit = use_case.execute(DEPOSIT, None)
    withdrawal = use_case.execute(WITHDRAWAL, None)

    assert deposit.created is True
    assert deposit.balance == Decimal("100.00")
    assert withdrawal.balance == Decimal("69.75")
    assert withdrawal.transaction.balance_after == Decimal("69.75")
    assert repository.account().balance == Decimal("69.75")
    assert repository.account().movement_count == 2


def test_movement_carries_region_and_millisecond_time(use_case: CreateTransaction) -> None:
    result = use_case.execute(DEPOSIT, None)

    assert result.transaction.processed_region == "us-east-2"
    assert result.transaction.processed_at == datetime(2026, 10, 4, 12, 0, 0, 456_000, tzinfo=UTC)
    assert result.transaction.type == "deposit"
    assert result.transaction.amount == Decimal("100.00")


def test_overdraft_stores_nothing_and_keeps_the_balance(
    use_case: CreateTransaction, repository: InMemoryTransactionRepository
) -> None:
    use_case.execute(DEPOSIT, None)

    with pytest.raises(InsufficientFunds) as caught:
        use_case.execute({"type": "withdrawal", "amount": Decimal("100.01")}, "key-2")

    assert caught.value.balance == Decimal("100.00")
    assert len(repository.stored) == 1
    assert repository.account().balance == Decimal("100.00")


def test_withdrawal_of_the_whole_balance_is_allowed(use_case: CreateTransaction) -> None:
    use_case.execute(DEPOSIT, None)

    result = use_case.execute({"type": "withdrawal", "amount": Decimal("100.00")}, None)

    assert result.balance == Decimal("0.00")


def test_same_key_twice_returns_the_first_result_and_the_balance_does_not_change(
    use_case: CreateTransaction, repository: InMemoryTransactionRepository
) -> None:
    first = use_case.execute(DEPOSIT, "key-1")
    use_case.execute(WITHDRAWAL, None)
    replay = use_case.execute({"type": "deposit", "amount": 5}, "key-1")

    assert replay.created is False
    assert replay.transaction == first.transaction
    assert replay.balance == Decimal("100.00")
    assert len(repository.stored) == 2
    assert repository.account().balance == Decimal("69.75")


def test_replay_of_a_movement_made_by_another_region(
    repository: InMemoryTransactionRepository,
    clock: FixedClock,
    ids: SequentialIdGenerator,
) -> None:
    CreateTransaction(repository, clock, ids, FixedRegionProvider("us-east-2")).execute(
        DEPOSIT, "key-1"
    )

    replay = CreateTransaction(repository, clock, ids, FixedRegionProvider("us-west-2")).execute(
        DEPOSIT, "key-1"
    )

    assert replay.created is False
    assert replay.transaction.processed_region == "us-east-2"


def test_lost_race_on_the_unique_key_is_reported_as_existing(
    use_case: CreateTransaction, repository: InMemoryTransactionRepository
) -> None:
    original = use_case.execute(DEPOSIT, "key-1").transaction
    repository.find_by_idempotency_key = lambda _key: None  # type: ignore[method-assign,assignment]

    replay = use_case.execute(DEPOSIT, "key-1")

    assert replay.created is False
    assert replay.transaction == original
    assert repository.account().balance == Decimal("100.00")


def test_invalid_input_stores_nothing(
    use_case: CreateTransaction, repository: InMemoryTransactionRepository
) -> None:
    with pytest.raises(ValidationError):
        use_case.execute({"type": "deposit", "amount": -1}, None)

    assert repository.stored == []
    assert repository.writes == 0


def test_read_only_database_stores_nothing(
    use_case: CreateTransaction, repository: InMemoryTransactionRepository
) -> None:
    repository.role = "reader"

    with pytest.raises(DatabaseReadOnly):
        use_case.execute(DEPOSIT, None)

    assert repository.stored == []


def test_unavailable_database_is_propagated(
    use_case: CreateTransaction, repository: InMemoryTransactionRepository
) -> None:
    repository.unavailable = True

    with pytest.raises(DatabaseUnavailable):
        use_case.execute(DEPOSIT, "key-1")
