"""Criteria 2.2 and 7.1: the read-only use cases of the account and its consistency."""

from decimal import Decimal

import pytest

from dr_demo.application.use_cases.create_transaction import CreateTransaction
from dr_demo.application.use_cases.get_account import GetAccount
from dr_demo.application.use_cases.get_consistency import GetConsistency
from dr_demo.domain.errors import DatabaseUnavailable
from tests.fakes.in_memory import (
    FixedClock,
    FixedRegionProvider,
    InMemoryTransactionRepository,
    SequentialIdGenerator,
)


@pytest.fixture
def populated(
    repository: InMemoryTransactionRepository,
    clock: FixedClock,
    ids: SequentialIdGenerator,
    region: FixedRegionProvider,
) -> InMemoryTransactionRepository:
    create = CreateTransaction(repository, clock, ids, region)
    create.execute({"type": "deposit", "amount": 100}, "key-1")
    create.execute({"type": "withdrawal", "amount": Decimal("40.50")}, "key-2")
    return repository


def test_get_account_reports_balance_count_region_and_role_without_writing(
    populated: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    writes_before = populated.writes
    populated.role = "reader"

    result = GetAccount(populated, region).execute()

    assert result.balance == Decimal("59.50")
    assert result.movement_count == 2
    assert result.served_by_region == "us-east-2"
    assert result.database_role == "reader"
    assert populated.writes == writes_before


def test_get_account_of_a_new_account_is_zero(
    repository: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    result = GetAccount(repository, region).execute()

    assert result.balance == Decimal(0)
    assert result.movement_count == 0


def test_get_consistency_of_a_clean_ledger_reads_only(
    populated: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    writes_before = populated.writes

    result = GetConsistency(populated, region).execute()

    assert result.report.consistent is True
    assert result.report.count == 2
    assert result.served_by_region == "us-east-2"
    assert populated.writes == writes_before


def test_get_consistency_detects_a_stored_balance_that_was_altered(
    populated: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    populated.balance = Decimal("1000.00")

    result = GetConsistency(populated, region).execute()

    assert result.report.consistent is False
    assert result.report.balances_match is False


def test_get_consistency_asks_for_one_row_beyond_the_limit(
    populated: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    GetConsistency(populated, region, limit=1).execute()

    assert populated.ledger_limits == [1]


def test_get_consistency_reports_truncation_when_the_ledger_exceeds_the_limit(
    populated: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    result = GetConsistency(populated, region, limit=1).execute()

    assert result.report.truncated is True
    assert result.report.consistent is None


def test_unavailable_database_propagates(
    repository: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    repository.unavailable = True

    with pytest.raises(DatabaseUnavailable):
        GetAccount(repository, region).execute()
    with pytest.raises(DatabaseUnavailable):
        GetConsistency(repository, region).execute()
