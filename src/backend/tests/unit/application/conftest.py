from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from dr_demo.domain.transaction import Transaction
from tests.fakes.in_memory import (
    FixedClock,
    FixedRegionProvider,
    InMemoryTransactionRepository,
    SequentialIdGenerator,
)

NOW = datetime(2026, 10, 4, 12, 0, 0, 456_789, tzinfo=UTC)


def stored_movement(index: int, region: str, moment: datetime) -> Transaction:
    """A deposit of 1.00 that produced balance ``index``."""
    return Transaction(
        id=UUID(int=index),
        type="deposit",
        amount=Decimal(1),
        balance_after=Decimal(index),
        processed_region=region,
        processed_at=moment,
        idempotency_key=None,
    )


@pytest.fixture
def repository() -> InMemoryTransactionRepository:
    return InMemoryTransactionRepository()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def region() -> FixedRegionProvider:
    return FixedRegionProvider("us-east-2")
