"""Use case: deposit or withdraw money (criteria 1.1 to 1.6)."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dr_demo.application.ports.clock import Clock
from dr_demo.application.ports.id_generator import IdGenerator
from dr_demo.application.ports.region_provider import RegionProvider
from dr_demo.application.ports.transaction_repository import TransactionRepository
from dr_demo.domain.transaction import NewTransaction, Transaction


@dataclass(frozen=True)
class CreateTransactionResult:
    transaction: Transaction
    created: bool

    @property
    def balance(self) -> Decimal:
        """The balance this movement produced (the original one on a replay)."""
        return self.transaction.balance_after


class CreateTransaction:
    def __init__(
        self,
        repository: TransactionRepository,
        clock: Clock,
        ids: IdGenerator,
        region: RegionProvider,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._ids = ids
        self._region = region

    def execute(
        self, payload: Mapping[str, Any] | object, idempotency_key: str | None
    ) -> CreateTransactionResult:
        """Validate, replay an earlier result for a known key, or apply a new movement."""
        new = NewTransaction.from_payload(payload, idempotency_key)
        if new.idempotency_key is not None:
            existing = self._repository.find_by_idempotency_key(new.idempotency_key)
            if existing is not None:
                return CreateTransactionResult(existing, created=False)
        draft = new.draft(self._ids.new_id(), self._region.region(), self._clock.now())
        stored = self._repository.apply_movement(draft)
        return CreateTransactionResult(stored, created=stored.id == draft.id)
