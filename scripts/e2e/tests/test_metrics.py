import pytest

from dr_drill.metrics import (
    CATEGORIES,
    CATEGORY_FENCE,
    CATEGORY_OTHER,
    CATEGORY_QUEUE,
    CATEGORY_SWITCHOVER,
    CATEGORY_TRAFFIC,
    UNPHASED,
    DirectionMetrics,
    Measure,
    compute_metrics,
    percentile,
    phase_windows,
)
from dr_drill.metrics import missing as missing_measure

from .timeline_builder import PRIMARY, SECONDARY, TimelineBuilder, at

TRAFFIC = "Group 3 - switch the Route 53 weights"


def outage_builder(recover: bool = True, fail: bool = True) -> TimelineBuilder:
    """Primary writes every second, an outage from 10 s, recovery in the secondary at 15 s."""
    builder = TimelineBuilder()
    builder.phase("baseline", 0, 5)
    builder.phase("fence", 5, 20)
    builder.phase("steady-secondary", 20, 30)
    for second in range(5, 10):
        builder.request("write", second)
    if fail:
        for second in range(10, 15):
            builder.request("write", second, ok=False, attempt=second - 9)
    for second in range(15 if recover else 99, 30):
        builder.request("write", second, region=SECONDARY)
    return builder


def to_secondary(builder: TimelineBuilder) -> DirectionMetrics:
    return compute_metrics(builder.timeline, PRIMARY, SECONDARY).directions[0]


def test_percentile_nearest_rank_edges() -> None:
    assert percentile([], 95) is None
    assert percentile([7.0], 50) == 7.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 95) == 4.0
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 95) == 95.0
    assert percentile(values, 100) == 100.0
    assert percentile(values, 0) == 1.0


def test_measure_without_value_needs_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        Measure(None)
    assert missing_measure("because").reason == "because"


def test_write_rto_and_unavailability_from_the_old_to_the_new_region() -> None:
    direction = to_secondary(outage_builder())
    assert direction.old_region == PRIMARY
    assert direction.new_region == SECONDARY
    assert direction.write_rto.value == pytest.approx(6.0)
    assert direction.write_unavailability.value == pytest.approx(5.04)


def test_rto_is_none_with_a_reason_when_the_outage_never_recovers() -> None:
    direction = to_secondary(outage_builder(recover=False))
    assert direction.write_rto.value is None
    assert direction.write_rto.reason is not None
    assert "never recovered" in direction.write_rto.reason
    assert direction.write_unavailability.value is None


def test_rto_is_none_when_no_failure_was_seen() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    builder.phase("steady-secondary", 10, 20)
    for second in range(0, 20):
        builder.request("write", second, region=PRIMARY if second < 10 else SECONDARY)
    direction = to_secondary(builder)
    assert direction.write_rto.value is None
    assert direction.write_rto.reason is not None
    assert "no outage observed" in direction.write_rto.reason


def test_a_retried_request_counts_as_the_first_failure() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    builder.phase("steady-secondary", 10, 20)
    builder.request("write", 1)
    builder.request("write", 2, attempt=2)
    builder.request("write", 4, region=SECONDARY)
    assert to_secondary(builder).write_rto.value == pytest.approx(3.0)


def test_a_write_that_succeeded_after_a_retry_is_anchored_at_its_failed_first_attempt() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    builder.phase("steady-secondary", 10, 20)
    builder.request("write", 1)
    builder.request("write", 2, ok=False, attempt=1)
    builder.request("write", 3, attempt=2, region=PRIMARY)
    builder.request("write", 5, region=SECONDARY)
    direction = to_secondary(builder)
    assert direction.write_unavailability.value == pytest.approx(3.04)
    assert direction.write_rto.value == pytest.approx(4.0)


def test_a_permanent_client_error_does_not_anchor_the_outage() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    builder.phase("steady-secondary", 10, 20)
    builder.request("write", 1)
    builder.request("write", 2, ok=False, status=422)
    builder.request("write", 3)
    builder.request("write", 4, ok=False)
    builder.request("write", 6, region=SECONDARY)
    direction = to_secondary(builder)
    assert direction.write_unavailability.value == pytest.approx(2.04)
    assert direction.write_rto.value == pytest.approx(3.0)


def test_rto_is_none_without_a_successful_answer_from_the_old_region() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    builder.request("write", 1, ok=False)
    builder.request("write", 3, region=SECONDARY)
    reason = to_secondary(builder).write_rto.reason
    assert reason is not None
    assert "before the first failure" in reason


def test_read_rto_uses_the_account_probe() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 30)
    builder.request("account", 0)
    builder.request("account", 5)
    builder.request("account", 10, ok=False)
    builder.request("account", 15, ok=False)
    builder.request("account", 20, region=SECONDARY)
    direction = to_secondary(builder)
    assert direction.read_rto.value == pytest.approx(15.0)
    assert direction.write_rto.value is None


def test_return_direction_swaps_the_regions() -> None:
    builder = TimelineBuilder()
    builder.phase("to-primary", 0, 20)
    builder.phase("steady-primary", 20, 30)
    builder.request("write", 1, region=SECONDARY)
    builder.request("write", 2, ok=False)
    builder.request("write", 6, region=PRIMARY)
    direction = compute_metrics(builder.timeline, PRIMARY, SECONDARY).directions[1]
    assert direction.old_region == SECONDARY
    assert direction.write_rto.value == pytest.approx(5.0)
    assert direction.decision_time.value is None
    assert direction.decision_time.reason is not None
    assert "not applicable" in direction.decision_time.reason


def test_direction_without_its_phase_is_missing_not_zero() -> None:
    builder = TimelineBuilder()
    builder.phase("baseline", 0, 10)
    metrics = compute_metrics(builder.timeline, PRIMARY, SECONDARY)
    for direction in metrics.directions:
        assert direction.write_rto.value is None
        assert direction.read_rto.value is None
        assert direction.auth.share.value is None
        assert direction.dns_convergence.value is None
        assert direction.write_rto.reason is not None
        assert "did not start" in direction.write_rto.reason
    assert metrics.workflow_totals == ()
    assert metrics.phase_stats == ()


def test_auth_availability_share_and_longest_failure_run() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 100)
    for second, ok in ((0, True), (10, True), (20, False), (30, False), (40, True), (50, False)):
        builder.request("auth", second, ok=ok, region=SECONDARY)
    auth = to_secondary(builder).auth
    assert auth.attempts == 6
    assert auth.failures == 3
    assert auth.share.value == pytest.approx(0.5)
    assert auth.longest_failure_run.value == 2.0
    assert auth.longest_failure_seconds.value == pytest.approx(10.04)


def test_auth_availability_without_failures_is_a_measured_zero_run() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 100)
    builder.request("auth", 0, region=SECONDARY)
    auth = to_secondary(builder).auth
    assert auth.share.value == 1.0
    assert auth.longest_failure_run.value == 0.0
    assert auth.longest_failure_seconds.value == 0.0


def test_auth_availability_without_samples_is_missing() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 100)
    auth = to_secondary(builder).auth
    assert auth.share.value is None
    assert auth.share.reason is not None
    assert auth.longest_failure_run.value is None


def workflow_builder() -> TimelineBuilder:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 40)
    builder.dispatch("fence", "simulate-outage", 0)
    builder.run("fence", "simulate-outage", 0, 4, 40)
    builder.step("fence", "simulate-outage", "Run 10-simulate-outage.sh", 10, 25)
    builder.step("fence", "simulate-outage", "Check out the code", 5, 6)
    builder.phase("to-secondary", 50, 120)
    builder.dispatch("to-secondary", "failover", 50)
    builder.run("to-secondary", "failover", 50, 53, 118)
    builder.step("to-secondary", "failover", "AWS login (group 1)", 54, 56)
    builder.step("to-secondary", "failover", "Group 1 - unfence the primary region", 60, 63)
    builder.step(
        "to-secondary", "failover", "Group 2 - planned switchover to the secondary region", 70, 90
    )
    builder.step(
        "to-secondary",
        "failover",
        "Group 2 - planned switchover back to the primary region",
        None,
        None,
        "skipped",
    )
    builder.step("to-secondary", "failover", TRAFFIC, 92, 95)
    builder.step("to-secondary", "failover", "Brand new step", 96, 98)
    builder.dns(97, ["192.0.2.10"], first=True)
    builder.dns(99, ["198.51.100.7"])
    builder.dns(120, ["192.0.2.10"])
    return builder


def test_breakdown_matches_real_step_names_and_lists_unknown_steps_under_other() -> None:
    breakdown = to_secondary(workflow_builder()).breakdown
    categories = breakdown.categories
    assert tuple(categories) == CATEGORIES
    assert categories[CATEGORY_QUEUE].value == pytest.approx(7.0)
    assert categories[CATEGORY_FENCE].value == pytest.approx(18.0)
    assert categories[CATEGORY_SWITCHOVER].value == pytest.approx(20.0)
    assert categories[CATEGORY_TRAFFIC].value == pytest.approx(3.0)
    assert categories[CATEGORY_OTHER].value == pytest.approx(2.0)
    assert breakdown.other_steps == ("Brand new step",)
    assert categories["validation"].value is None
    assert categories["validation"].reason is not None
    assert all(
        step.name != "Group 2 - planned switchover back to the primary region"
        for step in breakdown.steps
    )


def test_dns_convergence_is_from_the_traffic_switch_end_to_the_first_change() -> None:
    assert to_secondary(workflow_builder()).dns_convergence.value == pytest.approx(4.0)


def test_dns_convergence_missing_cases() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    assert "did not run" in str(to_secondary(builder).dns_convergence.reason)
    builder.step("fence", "simulate-outage", TRAFFIC, 1, 2)
    assert "no changed DNS" in str(to_secondary(builder).dns_convergence.reason)
    builder.step("fence", "simulate-outage", TRAFFIC, None, None, "skipped")
    other = TimelineBuilder()
    other.phase("fence", 0, 10)
    other.step("fence", "simulate-outage", TRAFFIC, None, None, "skipped")
    assert "no recorded times" in str(to_secondary(other).dns_convergence.reason)


def test_decision_time_is_from_the_end_of_the_fence_run_to_the_failover_dispatch() -> None:
    assert to_secondary(workflow_builder()).decision_time.value == pytest.approx(10.0)


def test_decision_time_missing_cases() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    assert "no recorded end" in str(to_secondary(builder).decision_time.reason)
    builder.dispatch("fence", "simulate-outage", 0)
    builder.run("fence", "simulate-outage", 0, 1, 9)
    assert "not dispatched" in str(to_secondary(builder).decision_time.reason)


def test_workflow_totals() -> None:
    totals = compute_metrics(workflow_builder().timeline, PRIMARY, SECONDARY).workflow_totals
    assert [total.phase for total in totals] == ["fence", "to-secondary"]
    assert totals[0].workflow == "simulate-outage"
    assert totals[0].queue.value == pytest.approx(4.0)
    assert totals[0].run.value == pytest.approx(36.0)
    assert totals[0].total.value == pytest.approx(40.0)


def test_workflow_total_without_a_run_event_is_missing() -> None:
    builder = TimelineBuilder()
    builder.phase("fence", 0, 10)
    builder.dispatch("fence", "simulate-outage", 0)
    total = compute_metrics(builder.timeline, PRIMARY, SECONDARY).workflow_totals[0]
    assert total.queue.value is None
    assert total.total.reason is not None


def test_phase_stats_errors_retries_and_latency_per_phase_and_region() -> None:
    builder = TimelineBuilder()
    builder.phase("baseline", 0, 10)
    builder.phase("fence", 10, 20)
    for index, latency in enumerate((10.0, 20.0, 30.0, 40.0)):
        builder.request("write", index, latency_ms=latency)
    builder.request("write", 11, ok=False)
    builder.request("write", 12, ok=False, attempt=2)
    builder.request("write", 13, attempt=3, latency_ms=100.0)
    builder.request("health", 5, region=SECONDARY)
    builder.request("write", 50)
    stats = compute_metrics(builder.timeline, PRIMARY, SECONDARY).phase_stats
    keyed = {(s.probe, s.phase, s.region): s for s in stats}
    baseline = keyed[("write", "baseline", PRIMARY)]
    assert baseline.attempts == 4
    assert baseline.error_rate == 0.0
    assert baseline.latency_median_ms.value == 20.0
    assert baseline.latency_p95_ms.value == 40.0
    assert baseline.latency_max_ms.value == 40.0
    errors = keyed[("write", "fence", "unknown")]
    assert errors.attempts == 2
    assert errors.errors == 2
    assert errors.error_rate == 1.0
    assert errors.retries == 1
    assert errors.latency_median_ms.value is None
    assert errors.latency_max_ms.reason is not None
    assert keyed[("write", "fence", PRIMARY)].retries == 1
    assert keyed[("write", UNPHASED, PRIMARY)].attempts == 1
    assert keyed[("health", "baseline", SECONDARY)].attempts == 1


def test_phase_without_requests_has_no_stat_row() -> None:
    builder = TimelineBuilder()
    builder.phase("baseline", 0, 10)
    builder.phase("fence", 10, 20)
    builder.request("write", 1)
    phases = {s.phase for s in compute_metrics(builder.timeline, PRIMARY, SECONDARY).phase_stats}
    assert phases == {"baseline"}


def test_phase_windows_keep_an_open_phase_open() -> None:
    builder = TimelineBuilder()
    builder.phase("baseline", 0, 10)
    builder.phase("fence", 10)
    builder.timeline.add("phase", {"name": "ABORT", "reason": "X"}, at(12))
    windows = phase_windows(builder.timeline)
    assert windows["baseline"].end == at(10)
    assert windows["fence"].end is None
    assert "ABORT" not in windows
