"""Use case: check the ledger against the stored balance (criteria 7.1 and 7.2). Reads only."""

from dataclasses import dataclass

from dr_demo.application.ports.region_provider import RegionProvider
from dr_demo.application.ports.transaction_repository import TransactionRepository
from dr_demo.domain.account import CONSISTENCY_LIMIT, ConsistencyReport, check_consistency


@dataclass(frozen=True)
class ConsistencyResult:
    report: ConsistencyReport
    served_by_region: str


class GetConsistency:
    def __init__(
        self,
        repository: TransactionRepository,
        region: RegionProvider,
        limit: int = CONSISTENCY_LIMIT,
    ) -> None:
        self._repository = repository
        self._region = region
        self._limit = limit

    def execute(self) -> ConsistencyResult:
        snapshot = self._repository.ledger(self._limit)
        report = check_consistency(snapshot.rows, snapshot.stored_balance, self._limit)
        return ConsistencyResult(report=report, served_by_region=self._region.region())
