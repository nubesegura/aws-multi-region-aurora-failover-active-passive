from datetime import timedelta
from uuid import UUID

from dr_demo.adapters.outbound.system import StaticRegionProvider, SystemClock, UuidGenerator


def test_clock_is_timezone_aware_utc() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)


def test_generated_ids_are_unique_uuids() -> None:
    generator = UuidGenerator()

    first, second = generator.new_id(), generator.new_id()

    assert isinstance(first, UUID)
    assert first != second


def test_static_region() -> None:
    assert StaticRegionProvider("us-west-2").region() == "us-west-2"
