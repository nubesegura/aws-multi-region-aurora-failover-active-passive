from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from dr_drill.api import Sample
from dr_drill.github import GithubError, RunDetails
from dr_drill.probe import Abandoned, Observation
from dr_drill.redact import RefusedOutput
from dr_drill.validate_log import VALIDATE_STEP
from tests.drill_fakes import (
    BASELINE_COUNT,
    FENCE_INPUTS,
    PRIMARY,
    SECONDARY,
    TO_PRIMARY,
    TO_SECONDARY,
    Rig,
    Scenario,
)

PHASES = [
    "baseline",
    "fence",
    "to-secondary",
    "steady-secondary",
    "to-primary",
    "steady-primary",
    "consistency",
]


def assert_probes_stopped(rig: Rig) -> None:
    assert rig.probes.starts == 1
    assert rig.probes.stops == 1


def test_the_happy_path_runs_the_phases_in_order_with_the_exact_dispatch_inputs() -> None:
    rig = Rig()

    result = rig.run()

    assert result.verdict == "PASS"
    assert result.exit_code == 0
    assert result.reason is None
    assert rig.phases() == PHASES
    assert rig.dispatches() == [
        ("simulate-outage", FENCE_INPUTS),
        ("failover", TO_SECONDARY),
        ("failover", TO_PRIMARY),
    ]
    assert_probes_stopped(rig)
    assert rig.finalized == [result]
    assert result.ledger is not None
    assert result.ledger.rpo_status == "zero_loss"
    assert result.ledger.band == 2
    assert result.warnings == ()
    assert len(result.runs) == 3


def test_the_happy_path_takes_snapshots_at_the_start_the_end_and_after_each_workflow() -> None:
    rig = Rig()

    rig.run()

    labels = [event.data["label"] for event in rig.timeline.events("state_snapshot")]
    assert labels == [
        "probe-start",
        "after-fence",
        "after-to-secondary",
        "after-to-primary",
        "probe-end",
        "ledger",
    ]
    snapshot = rig.timeline.events("state_snapshot")[0].data
    assert snapshot["writer"] == PRIMARY
    assert snapshot["weights"] == {"use2": 255, "usw2": 0}
    assert set(snapshot["fence"]) == {PRIMARY, SECONDARY}


def test_each_steady_phase_lasts_the_configured_time_and_writes_nothing_in_the_switch_phases() -> (
    None
):
    rig = Rig(steady=60)

    rig.run()

    starts = {
        event.data["name"]: event.time_utc
        for event in rig.timeline.events("phase")
        if event.data.get("state") == "start"
    }
    ends = {
        event.data["name"]: event.time_utc
        for event in rig.timeline.events("phase")
        if event.data.get("state") == "end"
    }
    for name in ("baseline", "steady-secondary", "steady-primary"):
        assert (ends[name] - starts[name]).total_seconds() >= 60
    assert rig.status[0][0] == "baseline"


def test_the_ledger_baseline_is_read_before_the_probe_starts() -> None:
    rig = Rig()
    seen: list[int] = []
    rig.probes.start = lambda: seen.append(rig.api.account_calls)  # type: ignore[method-assign]

    rig.run()

    assert seen == [1]


@pytest.mark.parametrize(
    ("failing", "dispatched"),
    [
        ("simulate-outage", 1),
        ("failover:to-secondary", 2),
        ("failover:to-primary", 3),
    ],
)
def test_a_workflow_that_does_not_succeed_stops_without_another_dispatch(
    failing: str, dispatched: int
) -> None:
    rig = Rig()
    rig.workflows.conclusions[failing] = "failure"

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "WORKFLOW_FAILED"
    assert result.exit_code == 1
    assert len(rig.workflows.calls) == dispatched
    assert rig.abort_reason() == "WORKFLOW_FAILED"
    assert_probes_stopped(rig)
    assert rig.finalized == [result]
    assert result.ledger is None
    assert any("Do not retry" in line for line in rig.out)


def test_a_cancelled_workflow_stops() -> None:
    rig = Rig()
    rig.workflows.conclusions["failover:to-secondary"] = "cancelled"

    assert rig.run().reason == "WORKFLOW_FAILED"


def test_a_failed_workflow_keeps_the_steps_it_ran_in_the_timeline() -> None:
    rig = Rig()
    rig.workflows.conclusions["simulate-outage"] = "failure"

    rig.run()

    steps = [e.data for e in rig.timeline.events("workflow_step") if e.data["event"] == "step"]
    assert steps[0]["step"] == "Run 10-simulate-outage.sh"
    assert steps[0]["conclusion"] == "failure"
    events = [e.data["event"] for e in rig.timeline.events("workflow_step")]
    assert events[:3] == ["dispatch", "run_found", "run"]


def test_a_github_error_stops_and_says_the_run_may_still_be_active() -> None:
    rig = Rig()
    rig.workflows.details_error = GithubError("gh run view failed")

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "WORKFLOW_ERROR"
    assert result.detail is not None
    assert "may still be running" in result.detail
    assert_probes_stopped(rig)


def test_a_failed_dispatch_stops_before_any_workflow_ran() -> None:
    rig = Rig()
    rig.workflows.dispatch_error = GithubError("gh workflow run failed")

    result = rig.run()

    assert result.reason == "WORKFLOW_ERROR"
    assert rig.workflows.calls == []
    assert_probes_stopped(rig)


def test_a_probe_answer_from_the_wrong_region_after_the_switch_stops() -> None:
    rig = Rig()
    rig.scenario.on_tick = lambda s: None
    rig.workflows.conclusions = {}
    original_wait = rig.workflows.wait

    def wait(run_id: int) -> bool:
        ok = original_wait(run_id)
        if rig.workflows.calls[-1][2] == TO_SECONDARY:
            rig.scenario.wrong_region_after = rig.time.now + 40.0
        return ok

    rig.workflows.wait = wait  # type: ignore[method-assign]

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "WRONG_REGION_OR_ROLE"
    assert [call[1] for call in rig.workflows.calls] == ["simulate-outage", "failover"]
    assert_probes_stopped(rig)


def test_a_probe_answer_with_the_wrong_role_stops() -> None:
    rig = Rig()
    original_wait = rig.workflows.wait

    def wait(run_id: int) -> bool:
        ok = original_wait(run_id)
        if rig.workflows.calls[-1][2] == TO_SECONDARY:
            rig.scenario.role = "reader"
        return ok

    rig.workflows.wait = wait  # type: ignore[method-assign]

    result = rig.run()

    assert result.reason == "WRONG_REGION_OR_ROLE"
    assert len(rig.workflows.calls) == 2


def test_the_switch_windows_and_the_grace_after_a_switch_are_not_violations() -> None:
    rig = Rig()
    original_wait = rig.workflows.wait

    def wait(run_id: int) -> bool:
        ok = original_wait(run_id)
        if rig.workflows.calls[-1][2] == TO_SECONDARY:
            rig.scenario.region = PRIMARY
            rig.scenario.on_tick = _converge_after(rig, 10.0)
        return ok

    rig.workflows.wait = wait  # type: ignore[method-assign]

    result = rig.run()

    assert result.verdict == "PASS"


def _converge_after(rig: Rig, seconds: float) -> Callable[[Scenario], None]:
    started = rig.time.now
    state = {"done": False}

    def tick(scenario: Scenario) -> None:
        if not state["done"] and rig.time.now - started >= seconds:
            rig.scenario.region = SECONDARY
            state["done"] = True

    return tick


def test_a_steady_phase_without_any_conforming_answer_stops() -> None:
    rig = Rig()
    original_wait = rig.workflows.wait

    def wait(run_id: int) -> bool:
        ok = original_wait(run_id)
        if rig.workflows.calls[-1][2] == TO_SECONDARY:
            rig.scenario.answering = False
        return ok

    rig.workflows.wait = wait  # type: ignore[method-assign]

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "NOT_CONVERGED"
    assert len(rig.workflows.calls) == 2


def test_a_broken_balance_chain_stops() -> None:
    rig = Rig()
    rig.scenario.balance_glitch_at = 12

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "BALANCE_CHAIN"
    assert rig.workflows.calls == []
    assert_probes_stopped(rig)


def test_the_two_failover_probe_deposits_do_not_break_the_chain_but_a_third_does() -> None:
    rig = Rig()
    original_wait = rig.workflows.wait

    def wait(run_id: int) -> bool:
        ok = original_wait(run_id)
        if rig.workflows.calls[-1][2] == TO_PRIMARY:
            rig.scenario.balance += 1
        return ok

    rig.workflows.wait = wait  # type: ignore[method-assign]

    result = rig.run()

    assert result.reason == "BALANCE_CHAIN"
    assert [call[2] for call in rig.workflows.calls][-1] == TO_PRIMARY


def test_an_abandoned_movement_that_committed_does_not_break_the_chain() -> None:
    rig = Rig()
    rig.scenario.on_tick = _abandon_once(rig)

    result = rig.run()

    assert result.verdict == "INCONCLUSIVE"
    assert result.reason == "RPO_NOT_VERIFIED"
    assert result.ledger is not None
    assert result.ledger.band == 3


def _abandon_once(rig: Rig) -> Callable[[Scenario], None]:
    state = {"done": False}

    def tick(scenario: Scenario) -> None:
        if not state["done"] and rig.scenario.count == 3:
            state["done"] = True
            rig.write.abandoned.append(Abandoned("w900", "k900", "deposit", 600, 503, "max_outage"))
            rig.scenario.balance += 1

    return tick


def test_an_inconsistent_ledger_at_the_end_stops_after_the_whole_sequence() -> None:
    rig = Rig()
    rig.api.consistent = False

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "INCONSISTENT"
    assert result.ledger is not None
    assert len(rig.workflows.calls) == 3
    assert_probes_stopped(rig)


def test_an_inconsistent_baseline_stops_before_the_probe_starts() -> None:
    rig = Rig()
    rig.api.baseline_consistent = False

    result = rig.run()

    assert result.reason == "INCONSISTENT"
    assert rig.workflows.calls == []
    assert rig.probes.starts == 0
    assert rig.probes.stops == 0


def test_an_unreadable_baseline_stops_before_the_probe_starts() -> None:
    rig = Rig()
    rig.api.baseline_status = 503

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "BASELINE_UNREADABLE"
    assert rig.probes.starts == 0
    assert rig.workflows.calls == []
    assert rig.finalized == [result]


def test_a_loss_the_failover_deposit_band_can_hide_is_inconclusive_not_a_pass() -> None:
    rig = Rig()
    rig.api.final_count_delta = -1

    result = rig.run()

    assert (result.verdict, result.reason) == ("INCONCLUSIVE", "RPO_NOT_VERIFIED")


def test_a_lost_acknowledged_write_beyond_the_band_fails_the_run() -> None:
    rig = Rig()
    rig.api.final_count_delta = -3

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "LEDGER_MISMATCH"
    assert result.ledger is not None
    assert result.ledger.rpo_status == "loss"


def test_a_truncated_consistency_answer_is_inconclusive_not_a_pass() -> None:
    rig = Rig()
    rig.api.consistent = None
    rig.api.truncated = True

    result = rig.run()

    assert result.verdict == "INCONCLUSIVE"
    assert result.exit_code == 1
    assert result.reason == "RPO_NOT_VERIFIED"
    assert result.ledger is not None
    assert result.ledger.rpo_status == "not_verified"


def test_a_probe_error_in_a_steady_phase_stops() -> None:
    rig = Rig()
    rig.scenario.on_tick = lambda s: setattr(rig.write, "probe_errors", 1)

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "PROBE_ERROR"
    assert rig.workflows.calls == []
    assert_probes_stopped(rig)


def test_a_probe_error_in_a_switch_window_only_warns() -> None:
    rig = Rig()
    original_wait = rig.workflows.wait

    def wait(run_id: int) -> bool:
        if rig.workflows.calls[-1][1] == "simulate-outage":
            rig.write.probe_errors += 1
        return original_wait(run_id)

    rig.workflows.wait = wait  # type: ignore[method-assign]

    result = rig.run()

    assert result.verdict == "PASS"
    assert len(result.warnings) == 1
    assert "probe error" in result.warnings[0]
    notes = [event.data["text"] for event in rig.timeline.events("note")]
    assert any("probe error" in note for note in notes)


def test_an_interruption_snapshots_prints_the_state_and_exits_130() -> None:
    rig = Rig()
    rig.workflows.interrupt_on = "failover:to-secondary"
    rig.observers.fenced_regions = {PRIMARY}
    rig.observers.writer = SECONDARY

    result = rig.run()

    assert result.verdict == "ABORTED"
    assert result.exit_code == 130
    assert len(rig.workflows.calls) == 2
    assert_probes_stopped(rig)
    assert rig.finalized == [result]
    text = "\n".join(rig.out)
    assert f"writer region {SECONDARY}" in text
    assert f"Fence {PRIMARY}: 6 of 6" in text
    assert f"Fence {SECONDARY}: 0 of 6" in text
    assert "docs/dr-runbook.md" in text
    assert rig.timeline.events("state_snapshot")[-1].data["label"] == "interrupted"
    assert rig.abort_reason() is None
    results = [e.data for e in rig.timeline.events("phase") if e.data.get("name") == "RESULT"]
    assert results[-1]["verdict"] == "ABORTED"


def test_an_unreadable_state_is_said_not_hidden() -> None:
    rig = Rig()
    rig.workflows.conclusions["simulate-outage"] = "failure"
    rig.observers.fail = True

    result = rig.run()

    assert result.reason == "WORKFLOW_FAILED"
    text = "\n".join(rig.out)
    assert "writer region unknown" in text
    snapshot_errors = [e.data["errors"] for e in rig.timeline.events("state_snapshot")]
    assert all(errors for errors in snapshot_errors)


def test_an_unexpected_error_stops_the_probe_writes_the_outputs_and_propagates() -> None:
    rig = Rig()
    rig.workflows.details_error = RuntimeError("secret detail")

    with pytest.raises(RuntimeError):
        rig.run()

    assert_probes_stopped(rig)
    assert len(rig.finalized) == 1
    result = rig.finalized[0]
    assert (result.verdict, result.reason, result.exit_code) == ("FAIL", "UNEXPECTED_ERROR", 1)
    assert result.detail == "RuntimeError"
    assert rig.abort_reason() == "UNEXPECTED_ERROR"
    assert "secret detail" not in " ".join(rig.out)
    assert any("may still be running" in line for line in rig.out)


def test_a_probe_set_that_does_not_stop_is_a_warning() -> None:
    rig = Rig()
    rig.probes.stuck = True

    result = rig.run()

    assert result.verdict == "PASS"
    assert any("did not stop" in warning for warning in result.warnings)


def test_the_run_never_dispatches_anything_else() -> None:
    rig = Rig()
    rig.workflows.conclusions["failover:to-primary"] = "failure"

    rig.run()

    assert {call[1] for call in rig.workflows.calls} <= {"simulate-outage", "failover"}
    assert all("force" not in call[2] for call in rig.workflows.calls)


def _write_observation(
    label: str, status: int, balance: int, kind: str = "deposit", key: str | None = None
) -> Observation:
    moment = datetime(2026, 11, 11, tzinfo=UTC)
    return Observation(
        "write",
        label,
        1,
        moment,
        moment,
        200 <= status < 300,
        status,
        1.0,
        key=key or label,
        kind=kind,
        served_by_region=PRIMARY,
        processed_region=PRIMARY,
        database_role="writer",
        balance_cents=balance,
    )


def test_the_monitor_ignores_replays_and_failed_attempts_for_the_chain() -> None:
    rig = Rig()
    monitor = rig.monitor
    monitor.anchor(100)

    monitor.feed(_write_observation("w1", 503, 0))
    monitor.feed(_write_observation("w1", 201, 101))
    monitor.feed(_write_observation("w1", 200, 55))
    monitor.feed(_write_observation("w2", 201, 100, "withdrawal"))

    assert monitor.violation is None


def test_the_monitor_anchors_on_the_first_acknowledged_write_when_there_is_no_baseline() -> None:
    rig = Rig()

    rig.monitor.feed(_write_observation("w1", 201, 500))
    rig.monitor.feed(_write_observation("w2", 201, 499, "withdrawal"))

    assert rig.monitor.violation is None
    rig.monitor.feed(_write_observation("w3", 201, 600))
    assert rig.monitor.violation is not None
    assert rig.monitor.violation[0] == "BALANCE_CHAIN"


def test_the_monitor_does_not_check_the_secondary_auth_probe_or_failed_answers() -> None:
    rig = Rig()
    rig.monitor.expect(PRIMARY, "writer", grace_seconds=0.0)
    moment = datetime(2026, 11, 11, tzinfo=UTC)

    rig.monitor.feed(
        Observation("auth", "u1", 1, moment, moment, True, 200, 1.0, served_by_region=SECONDARY)
    )
    rig.monitor.feed(
        Observation("write", "w1", 1, moment, moment, False, 503, 1.0, served_by_region=SECONDARY)
    )

    assert rig.monitor.violation is None
    assert not rig.monitor.converged


def test_the_first_violation_is_kept() -> None:
    rig = Rig()
    rig.monitor.expect(PRIMARY, "writer", grace_seconds=0.0)
    moment = datetime(2026, 11, 11, tzinfo=UTC)

    for label, region in (("a1", SECONDARY), ("a2", "eu-west-1")):
        rig.monitor.feed(
            Observation(
                "account", label, 1, moment, moment, True, 200, 1.0,
                served_by_region=region, database_role="writer",
            )
        )  # fmt: skip

    assert rig.monitor.violation is not None
    assert "a1" in rig.monitor.violation[1]


def _failover_deposits_only(rig: Rig, count: int) -> None:
    """The failover workflows succeed but only the first `count` of them post the deposit."""
    original = rig.scenario.failover_probe_deposit
    state = {"left": count}

    def deposit() -> None:
        if state["left"] > 0:
            state["left"] -= 1
            original()

    rig.scenario.failover_probe_deposit = deposit  # type: ignore[method-assign]


@pytest.mark.parametrize("deposits", [0, 1, 2])
def test_a_failover_probe_deposit_is_uncertain_whether_or_not_the_validate_step_succeeded(
    deposits: int,
) -> None:
    rig = Rig()
    _failover_deposits_only(rig, deposits)

    result = rig.run()

    assert result.reason != "BALANCE_CHAIN"
    assert result.reason != "LEDGER_MISMATCH"
    assert result.ledger is not None
    assert result.ledger.band == 2
    assert result.ledger.problems == ()
    if deposits == 2:
        assert result.verdict == "PASS"
    else:
        assert (result.verdict, result.exit_code) == ("INCONCLUSIVE", 1)
        assert result.reason == "RPO_NOT_VERIFIED"
        assert any(line.startswith("Inconclusive:") for line in rig.out)


HEADER = "step 3/3: probe movement (deposit of 0.01)"
SKIPPED_LOG = "\n".join(
    [HEADER, "  skipped (no COGNITO_ID_TOKEN provided for authenticated probe)"]
)
DEPOSITED_LOG = "\n".join([HEADER, "  HTTP 201 processed_region=us-west-2 id=abc balance=1.00"])
REJECTED_LOG = "\n".join([HEADER, "WARN  probe returned HTTP 400"])
AMBIGUOUS_LOG = "\n".join([HEADER, "WARN  probe returned HTTP 504"])


def _logs(rig: Rig, to_secondary: str, to_primary: str) -> None:
    rig.workflows.step_logs = {
        "failover:to-secondary": to_secondary,
        "failover:to-primary": to_primary,
    }


def test_two_skipped_probe_deposits_give_an_exact_pass_with_band_zero() -> None:
    rig = Rig()
    rig.workflows.probe_deposits = False
    _logs(rig, SKIPPED_LOG, SKIPPED_LOG)

    result = rig.run()

    assert (result.verdict, result.exit_code, result.reason) == ("PASS", 0, None)
    assert result.ledger is not None
    assert result.ledger.band == 0
    assert result.ledger.rpo_status == "zero_loss"
    assert (result.ledger.rpo_lost_min, result.ledger.rpo_lost_max) == (0, 0)
    assert [name for _, name in rig.workflows.log_reads] == [VALIDATE_STEP] * 2


def test_a_deposited_then_skipped_failover_counts_one_certain_deposit() -> None:
    rig = Rig()
    _failover_deposits_only(rig, 1)
    _logs(rig, DEPOSITED_LOG, SKIPPED_LOG)

    result = rig.run()

    assert result.verdict == "PASS"
    assert result.ledger is not None
    assert result.ledger.band == 0
    assert result.ledger.acked_expected == BASELINE_COUNT + len(rig.write.acked) + 1


def test_a_deposit_the_log_proves_but_the_ledger_lacks_is_a_loss() -> None:
    rig = Rig()
    rig.workflows.probe_deposits = False
    _logs(rig, DEPOSITED_LOG, SKIPPED_LOG)

    result = rig.run()

    assert (result.verdict, result.reason) == ("FAIL", "LEDGER_MISMATCH")
    assert result.ledger is not None
    assert result.ledger.rpo_status == "loss"


def test_a_deposit_the_log_denies_but_the_balance_shows_is_a_broken_chain() -> None:
    rig = Rig()
    _logs(rig, SKIPPED_LOG, SKIPPED_LOG)

    result = rig.run()

    assert result.verdict == "FAIL"
    assert result.reason == "BALANCE_CHAIN"


@pytest.mark.parametrize("log", ["", "no marker", HEADER, "\n".join([HEADER, "  something else"])])
def test_an_unreadable_validate_log_keeps_the_band_and_is_inconclusive(log: str) -> None:
    rig = Rig()
    rig.workflows.probe_deposits = False
    _logs(rig, log, log)

    result = rig.run()

    assert (result.verdict, result.reason) == ("INCONCLUSIVE", "RPO_NOT_VERIFIED")
    assert result.ledger is not None
    assert result.ledger.band == 2


def test_a_log_that_cannot_be_read_is_uncertain_like_an_unknown_one() -> None:
    rig = Rig()
    rig.workflows.probe_deposits = False
    rig.workflows.log_error = GithubError("gh run view --log failed")

    result = rig.run()

    assert (result.verdict, result.reason) == ("INCONCLUSIVE", "RPO_NOT_VERIFIED")
    assert result.ledger is not None
    assert result.ledger.band == 2
    assert result.warnings == ()
    notes = [str(event.data["text"]) for event in rig.timeline.events("note")]
    assert any("could not be read" in note for note in notes)


def _deposit_outcomes(rig: Rig) -> list[object]:
    return [
        event.data["outcome"]
        for event in rig.timeline.events("workflow_step")
        if event.data["event"] == "probe_deposit"
    ]


def test_a_rejected_probe_fails_the_run_but_its_log_closes_the_deposit_slot() -> None:
    rig = Rig()
    rig.workflows.probe_deposits = False
    rig.workflows.conclusions["failover:to-secondary"] = "failure"
    _logs(rig, REJECTED_LOG, SKIPPED_LOG)

    result = rig.run()

    assert result.reason == "WORKFLOW_FAILED"
    assert [name for _, name in rig.workflows.log_reads] == [VALIDATE_STEP]
    assert any("HTTP error" in warning for warning in result.warnings)
    assert _deposit_outcomes(rig) == ["rejected"]


def test_an_ambiguous_http_error_keeps_the_deposit_slot_open() -> None:
    rig = Rig()
    rig.workflows.probe_deposits = False
    rig.workflows.conclusions["failover:to-secondary"] = "failure"
    _logs(rig, AMBIGUOUS_LOG, SKIPPED_LOG)

    result = rig.run()

    assert result.reason == "WORKFLOW_FAILED"
    assert result.warnings == ()
    assert _deposit_outcomes(rig) == ["unknown"]


def test_a_failed_workflow_still_stops_the_drill_after_reading_the_log() -> None:
    rig = Rig()
    rig.workflows.conclusions["failover:to-secondary"] = "failure"
    _logs(rig, SKIPPED_LOG, SKIPPED_LOG)

    result = rig.run()

    assert (result.verdict, result.reason) == ("FAIL", "WORKFLOW_FAILED")
    assert len(rig.workflows.log_reads) == 1


def test_each_failover_outcome_is_recorded_in_the_timeline() -> None:
    rig = Rig()
    rig.workflows.probe_deposits = False
    _logs(rig, SKIPPED_LOG, "")

    rig.run()

    outcomes = [
        event.data["outcome"]
        for event in rig.timeline.events("workflow_step")
        if event.data["event"] == "probe_deposit"
    ]
    assert outcomes == ["skipped", "unknown"]


def test_a_missing_validate_step_keeps_the_deposit_slot_open() -> None:
    rig = Rig()
    _failover_deposits_only(rig, 0)
    rig.workflows.details = _without_steps(rig.workflows.details)  # type: ignore[method-assign,assignment]

    result = rig.run()

    assert result.reason == "RPO_NOT_VERIFIED"


def _without_steps(original: Callable[[int], RunDetails]) -> Callable[[int], RunDetails]:
    def details(run_id: int) -> RunDetails:
        run = original(run_id)
        return replace(run, jobs=())

    return details


def test_a_wait_that_failed_is_not_trusted_even_if_the_run_says_success() -> None:
    rig = Rig()
    rig.workflows.wait_ok = False

    result = rig.run()

    assert result.reason == "WORKFLOW_FAILED"
    assert result.detail is not None
    assert "unknown" in result.detail
    assert "may still be running" in result.detail
    assert any("not trusted" in warning for warning in result.warnings)
    assert any("may still be running" in line for line in rig.out)


def test_a_run_that_has_not_completed_says_it_may_still_be_running() -> None:
    rig = Rig()
    rig.workflows.run_status = "in_progress"
    rig.workflows.conclusions["simulate-outage"] = "cancelled"

    result = rig.run()

    assert result.detail is not None
    assert "cancelled" in result.detail
    assert "may still be running" in result.detail


def test_a_sign_in_failure_at_the_baseline_stops_before_the_probe_starts() -> None:
    rig = Rig()
    rig.failing_token_calls = {1}

    result = rig.run()

    assert (result.verdict, result.reason) == ("FAIL", "BASELINE_UNREADABLE")
    assert rig.probes.starts == 0
    assert not any("may still be running" in line for line in rig.out)


def test_a_sign_in_failure_at_the_end_leaves_the_ledger_not_verified() -> None:
    rig = Rig()
    rig.failing_token_calls = {2}

    result = rig.run()

    assert (result.verdict, result.reason) == ("INCONCLUSIVE", "RPO_NOT_VERIFIED")
    assert any("sign-in failed" in warning for warning in result.warnings)
    assert result.ledger is not None
    assert result.ledger.rpo_status == "not_verified"


def test_an_interruption_while_the_probes_start_still_stops_them() -> None:
    rig = Rig()

    def start() -> None:
        rig.probes.starts += 1
        raise KeyboardInterrupt

    rig.probes.start = start  # type: ignore[method-assign]

    result = rig.run()

    assert result.verdict == "ABORTED"
    assert rig.probes.stops == 1


def test_an_interruption_during_the_final_ledger_is_aborted_with_the_probe_stopped_once() -> None:
    rig = Rig()
    original = rig.api.get_account

    def get_account(token: str) -> Sample:
        if rig.api.account_calls >= 1:
            raise KeyboardInterrupt
        return original(token)

    rig.api.get_account = get_account  # type: ignore[method-assign]

    result = rig.run()

    assert result.verdict == "ABORTED"
    assert result.exit_code == 130
    assert_probes_stopped(rig)
    assert rig.finalized == [result]


def test_a_second_interrupt_during_the_snapshot_cannot_skip_the_probe_stop() -> None:
    rig = Rig()
    rig.workflows.interrupt_on = "failover:to-secondary"

    def fence_state(region: str) -> dict[str, int | None]:
        if rig.probes.stops > 0:
            raise KeyboardInterrupt
        return {}

    rig.observers.fence_state = fence_state  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        rig.run()

    assert_probes_stopped(rig)


def _refuse_phase_events(rig: Rig, names: set[str]) -> None:
    original = rig.timeline.add

    def add(kind: str, data: dict[str, Any], time_utc: datetime | None = None) -> None:
        if kind == "phase" and data.get("name") in names:
            raise RefusedOutput("phase event", "it contains an ARN")
        original(kind, data, time_utc)

    rig.timeline.add = add  # type: ignore[method-assign]


def test_a_refused_abort_event_is_a_note_and_the_drill_still_finalizes() -> None:
    rig = Rig()
    rig.workflows.conclusions["simulate-outage"] = "failure"
    _refuse_phase_events(rig, {"ABORT"})

    result = rig.run()

    assert result.verdict == "FAIL"
    assert rig.finalized == [result]
    assert_probes_stopped(rig)
    notes = [event.data["text"] for event in rig.timeline.events("note")]
    assert "phase ABORT event refused: it contains an ARN" in notes


def test_a_refused_interrupted_event_is_a_note_and_the_drill_still_finalizes() -> None:
    rig = Rig()
    rig.workflows.interrupt_on = "failover:to-secondary"
    _refuse_phase_events(rig, {"INTERRUPTED"})

    result = rig.run()

    assert result.verdict == "ABORTED"
    assert rig.finalized == [result]


def test_a_refused_unexpected_error_event_still_finalizes_and_reraises() -> None:
    rig = Rig()
    rig.workflows.dispatch_error = RuntimeError("boom")
    _refuse_phase_events(rig, {"ABORT", "RESULT"})

    with pytest.raises(RuntimeError):
        rig.run()

    assert len(rig.finalized) == 1
    assert rig.finalized[0].reason == "UNEXPECTED_ERROR"
