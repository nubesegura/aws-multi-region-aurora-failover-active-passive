"""Criteria 2.1, 2.2 and 2.5 (stats and health)."""

from datetime import timedelta
from uuid import UUID

import pytest

from dr_demo.application.use_cases.get_health import GetHealth
from dr_demo.application.use_cases.get_stats import GetStats
from dr_demo.domain.errors import DatabaseUnavailable
from tests.fakes.in_memory import FixedClock, FixedRegionProvider, InMemoryTransactionRepository
from tests.unit.application.conftest import NOW, stored_movement


def test_empty_stats_still_report_region_and_role(
    repository: InMemoryTransactionRepository, clock: FixedClock, region: FixedRegionProvider
) -> None:
    result = GetStats(repository, clock, region).execute()

    assert result.total_count == 0
    assert result.counts_by_region == {}
    assert result.last_processed_at_by_region == {}
    assert result.latest == ()
    assert result.served_by_region == "us-east-2"
    assert result.database_role == "writer"
    assert result.generated_at == NOW


def test_stats_aggregate_by_region_and_keep_latest_ten(
    repository: InMemoryTransactionRepository, clock: FixedClock, region: FixedRegionProvider
) -> None:
    for index in range(12):
        where = "us-east-2" if index < 8 else "us-west-2"
        repository.stored.append(stored_movement(index + 1, where, NOW + timedelta(seconds=index)))
    repository.role = "reader"

    result = GetStats(repository, clock, region).execute()

    assert result.total_count == 12
    assert result.counts_by_region == {"us-east-2": 8, "us-west-2": 4}
    assert result.last_processed_at_by_region["us-west-2"].second == 11
    assert len(result.latest) == 10
    assert result.latest[0].id == UUID(int=12)
    assert result.database_role == "reader"


def test_stats_propagate_unavailable_database(
    repository: InMemoryTransactionRepository, clock: FixedClock, region: FixedRegionProvider
) -> None:
    repository.unavailable = True

    with pytest.raises(DatabaseUnavailable):
        GetStats(repository, clock, region).execute()


def test_shallow_health_does_not_touch_the_database(
    repository: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    repository.unavailable = True

    result = GetHealth(repository, region).execute(deep=False)

    assert result.status == "ok"
    assert result.region == "us-east-2"
    assert result.database_role is None


def test_deep_health_reports_the_role(
    repository: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    repository.role = "reader"

    result = GetHealth(repository, region).execute(deep=True)

    assert result.database_role == "reader"


def test_deep_health_fails_when_database_is_unreachable(
    repository: InMemoryTransactionRepository, region: FixedRegionProvider
) -> None:
    repository.unavailable = True

    with pytest.raises(DatabaseUnavailable):
        GetHealth(repository, region).execute(deep=True)
