"""Outbound adapters for time, identifiers and the serving region."""

from datetime import UTC, datetime
from uuid import UUID, uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidGenerator:
    def new_id(self) -> UUID:
        return uuid4()


class StaticRegionProvider:
    def __init__(self, region: str) -> None:
        self._region = region

    def region(self) -> str:
        return self._region
