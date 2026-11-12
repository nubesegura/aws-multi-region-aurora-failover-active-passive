"""Use case: health check (criterion 2.5)."""

from dataclasses import dataclass

from dr_demo.application.ports.region_provider import RegionProvider
from dr_demo.application.ports.transaction_repository import TransactionRepository
from dr_demo.domain.transaction import DatabaseRole


@dataclass(frozen=True)
class HealthResult:
    status: str
    region: str
    database_role: DatabaseRole | None


class GetHealth:
    def __init__(self, repository: TransactionRepository, region: RegionProvider) -> None:
        self._repository = repository
        self._region = region

    def execute(self, *, deep: bool) -> HealthResult:
        """Shallow: no database access. Deep: also read the role, which proves connectivity."""
        role = self._repository.database_role() if deep else None
        return HealthResult(status="ok", region=self._region.region(), database_role=role)
