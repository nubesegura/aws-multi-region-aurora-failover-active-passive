"""Use case: read the account (criterion 2.2). Reads only."""

from dataclasses import dataclass
from decimal import Decimal

from dr_demo.application.ports.region_provider import RegionProvider
from dr_demo.application.ports.transaction_repository import TransactionRepository
from dr_demo.domain.transaction import DatabaseRole


@dataclass(frozen=True)
class AccountResult:
    balance: Decimal
    movement_count: int
    served_by_region: str
    database_role: DatabaseRole


class GetAccount:
    def __init__(self, repository: TransactionRepository, region: RegionProvider) -> None:
        self._repository = repository
        self._region = region

    def execute(self) -> AccountResult:
        account = self._repository.account()
        return AccountResult(
            balance=account.balance,
            movement_count=account.movement_count,
            served_by_region=self._region.region(),
            database_role=self._repository.database_role(),
        )
