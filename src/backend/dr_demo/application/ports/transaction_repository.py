"""Port: persistence of the account and its ledger."""

from typing import Protocol

from dr_demo.domain.account import Account, LedgerSnapshot
from dr_demo.domain.transaction import (
    DatabaseRole,
    MovementDraft,
    Transaction,
    TransactionStats,
)


class TransactionRepository(Protocol):
    """Storage of the account balance and its movements.

    Every method may raise ``DatabaseUnavailable``; ``apply_movement`` may also raise
    ``DatabaseReadOnly``, ``DatabaseBusy``, ``InsufficientFunds`` and ``ValidationError`` (a
    deposit that would push the balance above the maximum).
    """

    def apply_movement(self, draft: MovementDraft) -> Transaction:
        """Store the movement and update the balance atomically; return the stored movement.

        The balance is computed under a lock with ``next_balance`` and written to ``balance_after``.
        If the idempotency key is already stored, nothing changes and the original movement is
        returned. A withdrawal above the balance raises ``InsufficientFunds`` and stores nothing.
        """
        ...

    def find_by_idempotency_key(self, key: str) -> Transaction | None:
        """Return the movement stored under the idempotency key, if any."""
        ...

    def account(self) -> Account:
        """Return the current balance and the number of movements (read only)."""
        ...

    def ledger(self, limit: int) -> LedgerSnapshot:
        """Return up to ``limit + 1`` rows in ledger order and the stored balance (read only)."""
        ...

    def list_latest(self, limit: int) -> tuple[Transaction, ...]:
        """Return the newest movements, newest first."""
        ...

    def stats(self, latest_count: int) -> TransactionStats:
        """Return counts per region, last time per region and the newest movements."""
        ...

    def database_role(self) -> DatabaseRole:
        """Return ``writer`` when the database accepts writes, otherwise ``reader``."""
        ...
