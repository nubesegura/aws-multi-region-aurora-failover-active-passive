"""In-memory implementations of the application ports, for tests."""

from collections import Counter
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from dr_demo.domain.account import Account, LedgerRow, LedgerSnapshot, next_balance
from dr_demo.domain.errors import DatabaseReadOnly, DatabaseUnavailable
from dr_demo.domain.transaction import (
    DatabaseRole,
    MovementDraft,
    Transaction,
    TransactionStats,
)


class InMemoryTransactionRepository:
    """Single-threaded stand-in for the ledger: no lock, same idempotency and balance rules."""

    def __init__(self, role: DatabaseRole = "writer") -> None:
        self.role: DatabaseRole = role
        self.unavailable = False
        self.stored: list[Transaction] = []
        self.balance = Decimal(0)
        self.writes = 0
        self.ledger_limits: list[int] = []

    def _check_available(self) -> None:
        if self.unavailable:
            raise DatabaseUnavailable

    def apply_movement(self, draft: MovementDraft) -> Transaction:
        self._check_available()
        if self.role == "reader":
            raise DatabaseReadOnly
        if draft.idempotency_key is not None:
            known = self._lookup(draft.idempotency_key)
            if known is not None:
                return known
        balance_after = next_balance(self.balance, draft.type, draft.amount)
        stored = draft.completed(balance_after)
        self.stored.append(stored)
        self.balance = balance_after
        self.writes += 1
        return stored

    def find_by_idempotency_key(self, key: str) -> Transaction | None:
        self._check_available()
        return self._lookup(key)

    def _lookup(self, key: str) -> Transaction | None:
        return next((item for item in self.stored if item.idempotency_key == key), None)

    def account(self) -> Account:
        self._check_available()
        return Account(self.balance, len(self.stored))

    def ledger(self, limit: int) -> LedgerSnapshot:
        self._check_available()
        self.ledger_limits.append(limit)
        rows = tuple(
            LedgerRow(
                item.id,
                item.type,
                item.amount,
                item.balance_after,
                item.idempotency_key,
                item.processed_region,
            )
            for item in self.stored[: limit + 1]
        )
        return LedgerSnapshot(rows, self.balance)

    def list_latest(self, limit: int) -> tuple[Transaction, ...]:
        self._check_available()
        newest_first = sorted(self.stored, key=lambda item: item.processed_at, reverse=True)
        return tuple(newest_first[:limit])

    def stats(self, latest_count: int) -> TransactionStats:
        self._check_available()
        counts = Counter(item.processed_region for item in self.stored)
        last: dict[str, datetime] = {}
        for item in self.stored:
            last[item.processed_region] = max(
                item.processed_at, last.get(item.processed_region, item.processed_at)
            )
        return TransactionStats(
            total_count=len(self.stored),
            counts_by_region=dict(counts),
            last_processed_at_by_region=last,
            latest=self.list_latest(latest_count),
        )

    def database_role(self) -> DatabaseRole:
        self._check_available()
        return self.role


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class SequentialIdGenerator:
    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> UUID:
        self._counter += 1
        return UUID(int=self._counter, version=4)


class FixedRegionProvider:
    def __init__(self, region: str) -> None:
        self._region = region

    def region(self) -> str:
        return self._region
