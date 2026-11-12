import pytest

from dr_drill.metrics import (
    LAG_METRIC,
    LAG_PERIOD_SECONDS,
    compute_metrics,
    lag_from_timeline,
    lag_statistics,
)

from .timeline_builder import PRIMARY, SECONDARY, TimelineBuilder


def test_statistics_of_known_samples() -> None:
    stats = lag_statistics([100.0, 200.0, 300.0, 400.0])
    assert stats.samples == 4
    assert stats.period_seconds == LAG_PERIOD_SECONDS == 60
    assert stats.max_ms.value == 400.0
    assert stats.p95_ms.value == 400.0
    assert stats.mean_ms.value == pytest.approx(250.0)
    assert "unplanned" in stats.bound


def test_a_single_sample_is_its_own_statistics() -> None:
    stats = lag_statistics([123.0])
    assert stats.samples == 1
    assert stats.max_ms.value == stats.p95_ms.value == stats.mean_ms.value == 123.0


def test_no_samples_give_none_with_the_reason() -> None:
    stats = lag_statistics([])
    assert stats.samples == 0
    for measure in (stats.max_ms, stats.p95_ms, stats.mean_ms):
        assert measure.value is None
        assert measure.reason is not None
        assert LAG_METRIC in measure.reason


def test_timeline_samples_are_grouped_by_metric() -> None:
    builder = TimelineBuilder()
    builder.lag(60, 100.0)
    builder.lag(120, 300.0)
    builder.lag(120, 5.0, metric="AuroraGlobalDBRPOLag")
    builder.timeline.add("metric_sample", {"metric": LAG_METRIC, "milliseconds": "bad"})
    builder.timeline.add("metric_sample", {"other": 1})
    lag = lag_from_timeline(builder.timeline)
    assert lag[LAG_METRIC].samples == 2
    assert lag[LAG_METRIC].mean_ms.value == pytest.approx(200.0)
    assert lag["AuroraGlobalDBRPOLag"].max_ms.value == 5.0


def test_timeline_without_samples_still_reports_the_main_metric_as_missing() -> None:
    lag = compute_metrics(TimelineBuilder().timeline, PRIMARY, SECONDARY).lag
    assert set(lag) == {LAG_METRIC}
    assert lag[LAG_METRIC].max_ms.value is None
