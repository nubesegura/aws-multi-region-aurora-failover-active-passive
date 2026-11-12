"""Use case: list the newest transactions (criteria 2.3 and 2.4)."""

from dr_demo.application.ports.transaction_repository import TransactionRepository
from dr_demo.domain.transaction import Transaction, parse_limit


class ListTransactions:
    def __init__(self, repository: TransactionRepository) -> None:
        self._repository = repository

    def execute(self, raw_limit: str | None) -> tuple[Transaction, ...]:
        """Return the newest transactions; ``raw_limit`` is the untrusted query value."""
        return self._repository.list_latest(parse_limit(raw_limit))
