"""Criteria 2.3 and 2.4."""

from datetime import timedelta
from uuid import UUID

import pytest

from dr_demo.application.use_cases.list_transactions import ListTransactions
from dr_demo.domain.errors import DatabaseUnavailable, ValidationError
from tests.fakes.in_memory import InMemoryTransactionRepository
from tests.unit.application.conftest import NOW, stored_movement


def _fill(repository: InMemoryTransactionRepository, count: int) -> None:
    for index in range(count):
        moment = NOW + timedelta(seconds=index)
        repository.stored.append(stored_movement(index + 1, "us-east-2", moment))


def test_defaults_to_20_newest_first(repository: InMemoryTransactionRepository) -> None:
    _fill(repository, 25)

    items = ListTransactions(repository).execute(None)

    assert len(items) == 20
    assert items[0].id == UUID(int=25)
    assert items == tuple(sorted(items, key=lambda item: item.processed_at, reverse=True))


def test_limit_is_applied(repository: InMemoryTransactionRepository) -> None:
    _fill(repository, 5)

    assert len(ListTransactions(repository).execute("3")) == 3


def test_empty_database(repository: InMemoryTransactionRepository) -> None:
    assert ListTransactions(repository).execute(None) == ()


@pytest.mark.parametrize("raw", ["0", "101", "x"])
def test_invalid_limit_never_reaches_the_repository(
    repository: InMemoryTransactionRepository, raw: str
) -> None:
    repository.unavailable = True

    with pytest.raises(ValidationError):
        ListTransactions(repository).execute(raw)


def test_unavailable_database(repository: InMemoryTransactionRepository) -> None:
    repository.unavailable = True

    with pytest.raises(DatabaseUnavailable):
        ListTransactions(repository).execute(None)
