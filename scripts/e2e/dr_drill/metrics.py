"""Metrics of a drill: pure functions over a `Timeline`, no I/O.

Every measure is a `Measure`: a value or `None` plus the reason it is missing. A missing datum is
never reported as zero. Percentiles use the nearest-rank method: for `n` sorted values the
`p`-th percentile is the value at rank `ceil(p / 100 * n)` (1-based), so it is always a real
sample and a single sample is its own median, 95th percentile and maximum.

A direction is one switch of the writer:

* `to-secondary`: the outage opens with the `fence` phase (the simulated outage of the primary
  region) and ends with the `steady-secondary` phase.
* `to-primary`: the planned return, from the `to-primary` phase to the `steady-primary` phase.

Timeline schema read here (written by `drill.py`, `dns.py` and the observers wiring): `request`
events (probe observations), `phase` events `{name, state: start|end}`, `workflow_step` events
`{phase, workflow, event: dispatch|run|job|step, ...}`, `dns` events `{first, answer, observed}`
and `metric_sample` events `{metric, region, milliseconds}`.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dr_drill.phases import (
    PHASE_BASELINE,
    PHASE_CONSISTENCY,
    PHASE_FENCE,
    PHASE_STEADY_PRIMARY,
    PHASE_STEADY_SECONDARY,
    PHASE_TO_PRIMARY,
    PHASE_TO_SECONDARY,
)
from dr_drill.probe import (
    PROBE_ACCOUNT,
    PROBE_AUTH,
    PROBE_HEALTH,
    PROBE_WRITE,
    Observation,
    is_permanent_error,
)
from dr_drill.timeline import Event, Timeline
from dr_drill.validate_log import VALIDATE_STEP

PHASES = (
    PHASE_BASELINE,
    PHASE_FENCE,
    PHASE_TO_SECONDARY,
    PHASE_STEADY_SECONDARY,
    PHASE_TO_PRIMARY,
    PHASE_STEADY_PRIMARY,
    PHASE_CONSISTENCY,
)
WORKFLOW_PHASES = (PHASE_FENCE, PHASE_TO_SECONDARY, PHASE_TO_PRIMARY)
UNPHASED = "unphased"
UNKNOWN_REGION = "unknown"
STATS_PROBES = (PROBE_WRITE, PROBE_ACCOUNT, PROBE_HEALTH)

CATEGORY_QUEUE = "queue"
CATEGORY_SETUP = "setup"
CATEGORY_PRE_CHECKS = "pre_checks"
CATEGORY_FENCE = "fence"
CATEGORY_COGNITO = "cognito_replica"
CATEGORY_SWITCHOVER = "switchover"
CATEGORY_TRAFFIC = "traffic_switch"
CATEGORY_VALIDATION = "validation"
CATEGORY_CONSISTENCY = "consistency"
CATEGORY_OTHER = "other"
CATEGORIES = (
    CATEGORY_QUEUE,
    CATEGORY_SETUP,
    CATEGORY_PRE_CHECKS,
    CATEGORY_FENCE,
    CATEGORY_COGNITO,
    CATEGORY_SWITCHOVER,
    CATEGORY_TRAFFIC,
    CATEGORY_VALIDATION,
    CATEGORY_CONSISTENCY,
    CATEGORY_OTHER,
)

TRAFFIC_STEP = "Group 3 - switch the Route 53 weights"

# Step names of `.github/workflows/failover.yml` and `simulate-outage.yml`; a step that is not
# listed here (and does not start with a listed prefix) is counted under `other`.
STEP_CATEGORY = {
    "Validate the branch, the inputs, the variables and the secrets": CATEGORY_SETUP,
    "Check out the code": CATEGORY_SETUP,
    "Prepare the step timer and the helper functions": CATEGORY_SETUP,
    "Record the writer region before": CATEGORY_SETUP,
    "Summary": CATEGORY_SETUP,
    "Set up job": CATEGORY_SETUP,
    "Complete job": CATEGORY_SETUP,
    "Group 1 - pre-checks (read only)": CATEGORY_PRE_CHECKS,
    "Group 1 - confirm that the primary region is fenced": CATEGORY_PRE_CHECKS,
    "Group 1 - confirm that the secondary region is NOT fenced": CATEGORY_PRE_CHECKS,
    "Group 1 - unfence the primary region": CATEGORY_FENCE,
    "Run 10-simulate-outage.sh": CATEGORY_FENCE,
    "Group 2 - activate the Cognito replica in the secondary region": CATEGORY_COGNITO,
    "Group 2 - deactivate the Cognito replica in the secondary region": CATEGORY_COGNITO,
    "Group 2 - planned switchover to the secondary region": CATEGORY_SWITCHOVER,
    "Group 2 - planned switchover back to the primary region": CATEGORY_SWITCHOVER,
    TRAFFIC_STEP: CATEGORY_TRAFFIC,
    VALIDATE_STEP: CATEGORY_VALIDATION,
    "Group 3 - consistency check of the ledger": CATEGORY_CONSISTENCY,
}
STEP_PREFIX_CATEGORY = (
    ("AWS login", CATEGORY_SETUP),
    ("Verify the AWS account", CATEGORY_SETUP),
    ("Post ", CATEGORY_SETUP),
)

LAG_METRIC = "AuroraGlobalDBReplicationLag"
LAG_PERIOD_SECONDS = 60
LAG_BOUND_STATEMENT = (
    "The RPO of an unplanned failover would be at most the replication lag at the moment of the "
    "loss. This drill performs planned switchovers and does not exercise an unplanned failover, "
    "so these figures bound what was observed during the run and are not a measured unplanned RPO."
)


@dataclass(frozen=True)
class Measure:
    """A value, or None with the reason it is missing (never a silent zero)."""

    value: float | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and not self.reason:
            raise ValueError("a missing measure needs a reason")


def missing(reason: str) -> Measure:
    """A measure without data."""
    return Measure(None, reason)


@dataclass(frozen=True)
class PhaseWindow:
    """Start and end (None when the run ended inside the phase) of a drill phase."""

    name: str
    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class AuthAvailability:
    """Share of successful secondary-region sign-ins and the longest run of failures."""

    share: Measure
    attempts: int
    failures: int
    longest_failure_run: Measure
    longest_failure_seconds: Measure


@dataclass(frozen=True)
class StepDuration:
    """One executed workflow step and the category it was matched to."""

    phase: str
    name: str
    category: str
    seconds: float


@dataclass(frozen=True)
class Breakdown:
    """Seconds per category of one direction, the executed steps and the unknown step names."""

    categories: dict[str, Measure]
    steps: tuple[StepDuration, ...]
    other_steps: tuple[str, ...]


@dataclass(frozen=True)
class DirectionMetrics:
    """Everything measured for one direction of the writer switch."""

    name: str
    old_region: str
    new_region: str
    write_rto: Measure
    read_rto: Measure
    write_unavailability: Measure
    auth: AuthAvailability
    dns_convergence: Measure
    decision_time: Measure
    breakdown: Breakdown


@dataclass(frozen=True)
class WorkflowTotal:
    """Queue, run and total (dispatch to end) time of one dispatched workflow run."""

    phase: str
    workflow: str
    queue: Measure
    run: Measure
    total: Measure


@dataclass(frozen=True)
class PhaseStat:
    """Requests of one probe in one phase answered by one region."""

    probe: str
    phase: str
    region: str
    attempts: int
    errors: int
    error_rate: float
    retries: int
    latency_median_ms: Measure
    latency_p95_ms: Measure
    latency_max_ms: Measure


@dataclass(frozen=True)
class LagStats:
    """Replication-lag statistics of one metric, in milliseconds."""

    metric: str
    samples: int
    period_seconds: int
    max_ms: Measure
    p95_ms: Measure
    mean_ms: Measure
    bound: str


@dataclass(frozen=True)
class RunMetrics:
    """All the metrics of a run."""

    phases: dict[str, PhaseWindow]
    directions: tuple[DirectionMetrics, ...]
    workflow_totals: tuple[WorkflowTotal, ...]
    phase_stats: tuple[PhaseStat, ...]
    lag: dict[str, LagStats]


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _seconds(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def phase_windows(timeline: Timeline) -> dict[str, PhaseWindow]:
    """Start and end of every phase that began, from the `phase` events."""
    starts: dict[str, datetime] = {}
    ends: dict[str, datetime] = {}
    for event in timeline.events("phase"):
        name = event.data.get("name")
        state = event.data.get("state")
        if name not in PHASES:
            continue
        if state == "start":
            starts.setdefault(name, event.time_utc)
        elif state == "end":
            ends[name] = event.time_utc
    return {
        name: PhaseWindow(name, starts[name], ends.get(name)) for name in PHASES if name in starts
    }


@dataclass(frozen=True)
class _Direction:
    name: str
    outage_phase: str
    workflow_phases: tuple[str, ...]
    steady_phase: str
    old_is_primary: bool


DIRECTIONS = (
    _Direction(
        "to-secondary", PHASE_FENCE, (PHASE_FENCE, PHASE_TO_SECONDARY), PHASE_STEADY_SECONDARY, True
    ),
    _Direction("to-primary", PHASE_TO_PRIMARY, (PHASE_TO_PRIMARY,), PHASE_STEADY_PRIMARY, False),
)


@dataclass(frozen=True)
class _Recovery:
    last_old_end: datetime
    first_failed_start: datetime
    first_new_end: datetime


def _anchors_outage(observation: Observation, retries_count: bool) -> bool:
    """True when the observation can be the start of an outage.

    An observation is one attempt with its own start time, so a failed first attempt is itself the
    anchor of a write that later succeeded after a retry. A retried attempt whose failed first
    attempt is not in the window (`retries_count`) still anchors. A permanent client error (for
    example a 422) is deterministic, not an outage, and never anchors.
    """
    if observation.ok:
        return retries_count and observation.attempt > 1
    return not is_permanent_error(observation.status, 0)


def _find_recovery(
    items: Sequence[Observation], old: str, new: str, retries_count: bool
) -> _Recovery | str:
    failed = next((o for o in items if _anchors_outage(o, retries_count)), None)
    if failed is None:
        return "no failed or retried request was seen in the window (no outage observed)"
    before = [
        o for o in items if o.ok and o.served_by_region == old and o.end_utc <= failed.start_utc
    ]
    if not before:
        return f"no successful request answered by {old} before the first failure"
    recovered = next(
        (
            o
            for o in items
            if o.ok and o.served_by_region == new and o.start_utc >= failed.start_utc
        ),
        None,
    )
    if recovered is None:
        return f"no successful request answered by {new} after the failure (never recovered)"
    return _Recovery(before[-1].end_utc, failed.start_utc, recovered.end_utc)


def _rto(recovery: _Recovery | str) -> Measure:
    if isinstance(recovery, str):
        return missing(recovery)
    return Measure(_seconds(recovery.last_old_end, recovery.first_new_end))


def _unavailability(recovery: _Recovery | str) -> Measure:
    if isinstance(recovery, str):
        return missing(recovery)
    return Measure(_seconds(recovery.first_failed_start, recovery.first_new_end))


def _in_window(
    items: Sequence[Observation], start: datetime, end: datetime | None
) -> list[Observation]:
    return [o for o in items if o.start_utc >= start and (end is None or o.start_utc < end)]


def _auth_availability(items: Sequence[Observation]) -> AuthAvailability:
    if not items:
        reason = "no authentication probe answer in the window"
        return AuthAvailability(missing(reason), 0, 0, missing(reason), missing(reason))
    failures = [o for o in items if not o.ok]
    longest: list[Observation] = []
    run: list[Observation] = []
    for item in items:
        run = [] if item.ok else [*run, item]
        if len(run) > len(longest):
            longest = run
    seconds = _seconds(longest[0].start_utc, longest[-1].end_utc) if longest else 0.0
    return AuthAvailability(
        share=Measure((len(items) - len(failures)) / len(items)),
        attempts=len(items),
        failures=len(failures),
        longest_failure_run=Measure(float(len(longest))),
        longest_failure_seconds=Measure(seconds),
    )


def _category(name: str) -> str:
    if name in STEP_CATEGORY:
        return STEP_CATEGORY[name]
    for prefix, category in STEP_PREFIX_CATEGORY:
        if name.startswith(prefix):
            return category
    return CATEGORY_OTHER


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value)


def _workflow_events(timeline: Timeline, phase: str, kind: str) -> list[Event]:
    return [
        event
        for event in timeline.events("workflow_step")
        if event.data.get("phase") == phase and event.data.get("event") == kind
    ]


def _run_times(
    timeline: Timeline, phase: str
) -> tuple[datetime | None, datetime | None, datetime | None]:
    """Dispatch, run start and run end (GitHub `updatedAt`) of the workflow of a phase."""
    dispatches = _workflow_events(timeline, phase, "dispatch")
    runs = _workflow_events(timeline, phase, "run")
    dispatched = dispatches[0].time_utc if dispatches else None
    started = _stamp(runs[0].data.get("started")) if runs else None
    ended = _stamp(runs[0].data.get("updated")) if runs else None
    return dispatched, started, ended


def _breakdown(timeline: Timeline, phases: Sequence[str]) -> Breakdown:
    totals: dict[str, float] = {}
    steps: list[StepDuration] = []
    for phase in phases:
        dispatched, started, _ = _run_times(timeline, phase)
        if dispatched is not None and started is not None:
            totals[CATEGORY_QUEUE] = totals.get(CATEGORY_QUEUE, 0.0) + _seconds(dispatched, started)
        for event in _workflow_events(timeline, phase, "step"):
            begin, end = _stamp(event.data.get("start")), _stamp(event.data.get("end"))
            if begin is None or end is None:
                continue
            name = str(event.data.get("step"))
            category = _category(name)
            seconds = _seconds(begin, end)
            totals[category] = totals.get(category, 0.0) + seconds
            steps.append(StepDuration(phase, name, category, seconds))
    categories = {
        category: Measure(totals[category])
        if category in totals
        else missing("no step of this category ran with recorded times")
        for category in CATEGORIES
    }
    other = tuple(dict.fromkeys(step.name for step in steps if step.category == CATEGORY_OTHER))
    return Breakdown(categories, tuple(steps), other)


def _dns_convergence(timeline: Timeline, workflow_phases: Sequence[str]) -> Measure:
    traffic = [
        event
        for phase in workflow_phases
        for event in _workflow_events(timeline, phase, "step")
        if event.data.get("step") == TRAFFIC_STEP
    ]
    if not traffic:
        return missing("the traffic-switch step did not run in this direction")
    begin, end = _stamp(traffic[0].data.get("start")), _stamp(traffic[0].data.get("end"))
    if begin is None or end is None:
        return missing("the traffic-switch step has no recorded times")
    changes = [
        event
        for event in timeline.events("dns")
        if not event.data.get("first") and event.time_utc >= begin
    ]
    if not changes:
        return missing("no changed DNS answer was observed after the traffic switch")
    return Measure(_seconds(end, changes[0].time_utc))


def _decision_time(timeline: Timeline, direction: _Direction) -> Measure:
    if direction.outage_phase != PHASE_FENCE:
        return missing("not applicable: the return is dispatched after a fixed steady phase")
    _, _, fence_end = _run_times(timeline, PHASE_FENCE)
    dispatched, _, _ = _run_times(timeline, PHASE_TO_SECONDARY)
    if fence_end is None:
        return missing("the fence run has no recorded end")
    if dispatched is None:
        return missing("the failover was not dispatched")
    return Measure(_seconds(fence_end, dispatched))


def _direction_metrics(
    timeline: Timeline,
    windows: dict[str, PhaseWindow],
    observations: Sequence[Observation],
    direction: _Direction,
    primary: str,
    secondary: str,
) -> DirectionMetrics:
    old, new = (primary, secondary) if direction.old_is_primary else (secondary, primary)
    breakdown = _breakdown(timeline, direction.workflow_phases)
    dns = _dns_convergence(timeline, direction.workflow_phases)
    decision = _decision_time(timeline, direction)
    opening = windows.get(direction.outage_phase)
    if opening is None:
        reason = f"the {direction.outage_phase} phase did not start"
        gone = missing(reason)
        return DirectionMetrics(
            direction.name,
            old,
            new,
            gone,
            gone,
            gone,
            _auth_availability(()),
            dns,
            decision,
            breakdown,
        )
    steady = windows.get(direction.steady_phase)
    window_end = None if steady is None else steady.end
    items = _in_window(observations, opening.start, window_end)
    writes = [o for o in items if o.probe == PROBE_WRITE]
    reads = [o for o in items if o.probe == PROBE_ACCOUNT]
    auth = [o for o in items if o.probe == PROBE_AUTH]
    write_recovery = _find_recovery(writes, old, new, True)
    return DirectionMetrics(
        name=direction.name,
        old_region=old,
        new_region=new,
        write_rto=_rto(write_recovery),
        read_rto=_rto(_find_recovery(reads, old, new, False)),
        write_unavailability=_unavailability(write_recovery),
        auth=_auth_availability(auth),
        dns_convergence=dns,
        decision_time=decision,
        breakdown=breakdown,
    )


def _workflow_totals(timeline: Timeline) -> tuple[WorkflowTotal, ...]:
    totals = []
    for phase in WORKFLOW_PHASES:
        dispatches = _workflow_events(timeline, phase, "dispatch")
        if not dispatches:
            continue
        dispatched, started, ended = _run_times(timeline, phase)
        workflow = str(dispatches[0].data.get("workflow"))
        no_run = "the run has no recorded times"
        queue = (
            missing(no_run)
            if dispatched is None or started is None
            else Measure(_seconds(dispatched, started))
        )
        run = (
            missing(no_run)
            if started is None or ended is None
            else Measure(_seconds(started, ended))
        )
        total = (
            missing(no_run)
            if dispatched is None or ended is None
            else Measure(_seconds(dispatched, ended))
        )
        totals.append(WorkflowTotal(phase, workflow, queue, run, total))
    return tuple(totals)


def _phase_of(moment: datetime, windows: dict[str, PhaseWindow]) -> str:
    for name in PHASES:
        window = windows.get(name)
        if window and window.start <= moment and (window.end is None or moment < window.end):
            return name
    return UNPHASED


def _latency(values: list[float], pct: float | None) -> Measure:
    if not values:
        return missing("no successful request in this group")
    chosen = max(values) if pct is None else percentile(values, pct)
    return Measure(chosen)


def _phase_stats(
    observations: Sequence[Observation], windows: dict[str, PhaseWindow]
) -> tuple[PhaseStat, ...]:
    groups: dict[tuple[str, str, str], list[Observation]] = {}
    for item in observations:
        if item.probe in STATS_PROBES:
            key = (
                item.probe,
                _phase_of(item.start_utc, windows),
                item.served_by_region or UNKNOWN_REGION,
            )
            groups.setdefault(key, []).append(item)
    order = {name: index for index, name in enumerate((*PHASES, UNPHASED))}
    stats = []
    for (probe, phase, region), items in sorted(
        groups.items(), key=lambda entry: (entry[0][0], order[entry[0][1]], entry[0][2])
    ):
        latencies = [o.latency_ms for o in items if o.ok]
        errors = sum(1 for o in items if not o.ok)
        stats.append(
            PhaseStat(
                probe=probe,
                phase=phase,
                region=region,
                attempts=len(items),
                errors=errors,
                error_rate=errors / len(items),
                retries=sum(1 for o in items if o.attempt > 1),
                latency_median_ms=_latency(latencies, 50),
                latency_p95_ms=_latency(latencies, 95),
                latency_max_ms=_latency(latencies, None),
            )
        )
    return tuple(stats)


def lag_statistics(values: Sequence[float], metric: str = LAG_METRIC) -> LagStats:
    """Maximum, 95th percentile and mean (ms) of lag samples; no samples give None plus a reason."""
    if not values:
        gone = missing(f"no {metric} samples were recorded")
        return LagStats(metric, 0, LAG_PERIOD_SECONDS, gone, gone, gone, LAG_BOUND_STATEMENT)
    return LagStats(
        metric=metric,
        samples=len(values),
        period_seconds=LAG_PERIOD_SECONDS,
        max_ms=Measure(max(values)),
        p95_ms=_latency(list(values), 95),
        mean_ms=Measure(statistics.fmean(values)),
        bound=LAG_BOUND_STATEMENT,
    )


def lag_from_timeline(timeline: Timeline) -> dict[str, LagStats]:
    """Statistics per lag metric from the `metric_sample` events (all regions together)."""
    values: dict[str, list[float]] = {LAG_METRIC: []}
    for event in timeline.events("metric_sample"):
        metric, milliseconds = event.data.get("metric"), event.data.get("milliseconds")
        if isinstance(metric, str) and isinstance(milliseconds, int | float):
            values.setdefault(metric, []).append(float(milliseconds))
    return {metric: lag_statistics(samples, metric) for metric, samples in values.items()}


def compute_metrics(timeline: Timeline, primary: str, secondary: str) -> RunMetrics:
    """All the metrics of a run from its timeline."""
    windows = phase_windows(timeline)
    observations = timeline.observations()
    return RunMetrics(
        phases=windows,
        directions=tuple(
            _direction_metrics(timeline, windows, observations, direction, primary, secondary)
            for direction in DIRECTIONS
        ),
        workflow_totals=_workflow_totals(timeline),
        phase_stats=_phase_stats(observations, windows),
        lag=lag_from_timeline(timeline),
    )
