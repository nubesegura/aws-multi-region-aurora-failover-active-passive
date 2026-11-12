"""Use case: statistics for the dashboard (criteria 2.1 and 2.2)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from dr_demo.application.ports.clock import Clock
from dr_demo.application.ports.region_provider import RegionProvider
from dr_demo.application.ports.transaction_repository import TransactionRepository
from dr_demo.domain.transaction import LATEST_COUNT, DatabaseRole, Transaction


@dataclass(frozen=True)
class StatsResult:
    total_count: int
    counts_by_region: Mapping[str, int]
    last_processed_at_by_region: Mapping[str, datetime]
    latest: tuple[Transaction, ...]
    served_by_region: str
    database_role: DatabaseRole
    generated_at: datetime


class GetStats:
    def __init__(
        self, repository: TransactionRepository, clock: Clock, region: RegionProvider
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._region = region

    def execute(self) -> StatsResult:
        stats = self._repository.stats(LATEST_COUNT)
        return StatsResult(
            total_count=stats.total_count,
            counts_by_region=stats.counts_by_region,
            last_processed_at_by_region=stats.last_processed_at_by_region,
            latest=stats.latest,
            served_by_region=self._region.region(),
            database_role=self._repository.database_role(),
            generated_at=self._clock.now(),
        )
