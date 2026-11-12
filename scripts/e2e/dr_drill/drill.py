"""The drill state machine: phases, stop rules, interruption and the final ledger check.

Phases, in order: `baseline` (steady, primary), `fence` (dispatch and wait `simulate-outage`),
`to-secondary` (dispatch and wait `failover`), `steady-secondary`, `to-primary`,
`steady-primary` and `consistency` (probe stopped, final ledger check).

Verdicts: `PASS`; `FAIL` (a stop condition, with its reason); `INCONCLUSIVE` (the run completed but
the ledger could not prove the RPO: truncated or missing answer, or an uncertainty band that
hides a possible loss); `ABORTED` (interrupted by the owner, exit code 130).

Stop conditions (requirement 3.3): a workflow that does not conclude `success`; a probe answer
whose region or role does not fit a steady phase; a broken balance chain; an inconsistent ledger;
plus the runner's own: an unreadable baseline, no conforming answer in a steady phase and a
permanent probe error in a steady phase. On a stop nothing else is dispatched, there is no retry,
no unfence and no `--force`: the probe is stopped after one last state snapshot and the state is
printed so the owner can follow the runbook. `probe_errors` (movements abandoned with a permanent
client error) stop the run in a steady phase (`PROBE_ERROR`) and are only a warning in the switch
windows, where the API is expected to misbehave.

Timeline schema written here: `phase` events `{name, state: start|end}` plus `{name: ABORT,
reason, detail}`, `{name: INTERRUPTED}` and `{name: RESULT, verdict, reason}`; `workflow_step`
events with `event` of `dispatch`, `run_found`, `run`, `job` or `step`; `state_snapshot` events
`{label, fence, weights, writer, errors}` and the `ledger` snapshot.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from dr_drill.api import Sample
from dr_drill.auth import AuthError
from dr_drill.github import GithubError, RunDetails
from dr_drill.ledger import (
    LedgerReading,
    LedgerReport,
    read_account,
    read_consistency,
    verify_ledger,
)
from dr_drill.phases import (
    PHASE_BASELINE,
    PHASE_CONSISTENCY,
    PHASE_FENCE,
    PHASE_STEADY_PRIMARY,
    PHASE_STEADY_SECONDARY,
    PHASE_TO_PRIMARY,
    PHASE_TO_SECONDARY,
    VERDICT_ABORTED,
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
    VERDICT_PASS,
)
from dr_drill.probe import (
    Abandoned,
    Acknowledged,
    Observation,
    Pending,
    ProbeClock,
    format_utc,
)
from dr_drill.redact import RefusedOutput
from dr_drill.timeline import Timeline
from dr_drill.validate_log import VALIDATE_STEP, ProbeDeposit, classify_probe_deposit

REASON_WORKFLOW_FAILED = "WORKFLOW_FAILED"
REASON_WORKFLOW_ERROR = "WORKFLOW_ERROR"
REASON_WRONG_REGION_OR_ROLE = "WRONG_REGION_OR_ROLE"
REASON_BALANCE_CHAIN = "BALANCE_CHAIN"
REASON_INCONSISTENT = "INCONSISTENT"
REASON_LEDGER_MISMATCH = "LEDGER_MISMATCH"
REASON_PROBE_ERROR = "PROBE_ERROR"
REASON_NOT_CONVERGED = "NOT_CONVERGED"
REASON_BASELINE_UNREADABLE = "BASELINE_UNREADABLE"
REASON_RPO_NOT_VERIFIED = "RPO_NOT_VERIFIED"
REASON_UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_INTERRUPTED = 130
RUNBOOK = "docs/dr-runbook.md"

POLL_SECONDS = 1.0
DEFAULT_STEADY_SECONDS = 60.0
DEFAULT_GRACE_SECONDS = 30.0
WRITER_ROLE = "writer"
FAILOVER_CONFIRMATION = "failover"
CHECKED_PROBES = frozenset({"write", "account", "health"})
SUCCESS_RANGE = range(200, 300)
CREATED = 201
DEPOSIT = "deposit"
KINDS = frozenset({"deposit", "withdrawal"})

Violation = tuple[str, str]


class Workflows(Protocol):
    """The GitHub adapter as the drill uses it."""

    def dispatch(self, workflow: str, inputs: Mapping[str, str]) -> datetime:
        """Dispatch an allow-listed workflow; returns the dispatch UTC time."""
        ...

    def find_run(self, workflow: str, after: datetime) -> int:
        """Run id of the dispatch."""
        ...

    def wait(self, run_id: int) -> bool:
        """One blocking wait; True when the run succeeded."""
        ...

    def details(self, run_id: int) -> RunDetails:
        """Conclusion, times, jobs and steps of the run."""
        ...

    def step_log(self, run_id: int, step_name: str) -> str:
        """Read-only: the log lines of one step of the run."""
        ...


class StateObserver(Protocol):
    """The read-only observers the snapshots use."""

    def fence_state(self, region: str) -> dict[str, int | None]:
        """Reserved concurrency of the API functions of a region."""
        ...

    def writer_region(self) -> str | None:
        """Region of the Aurora writer."""
        ...

    def route53_weights(self) -> dict[str, int]:
        """Weights of the API domain records."""
        ...


class LedgerApi(Protocol):
    """The two reads of the ledger check."""

    def get_account(self, token: str) -> Sample:
        """GET /account."""
        ...

    def get_consistency(self, token: str) -> Sample:
        """GET /consistency."""
        ...


class WriteStats(Protocol):
    """Accounting of the write probe."""

    @property
    def acked(self) -> Sequence[Acknowledged]:
        """Acknowledged movements."""
        ...

    @property
    def abandoned(self) -> Sequence[Abandoned]:
        """Abandoned movements."""
        ...

    @property
    def pending(self) -> Sequence[Pending]:
        """The movement in flight at stop."""
        ...

    @property
    def probe_errors(self) -> int:
        """Movements abandoned because of a permanent client error."""
        ...


class ProbesPort(Protocol):
    """Start and stop of every probe together (`ProbeSet`)."""

    def start(self) -> None:
        """Start the probes."""
        ...

    def stop(self) -> bool:
        """Stop the probes; False when one did not end in time."""
        ...


class ProbeMonitor:
    """Watches the probe answers: region and role of a steady phase, and the balance chain.

    It is the sink tap of the probes (`feed`). Answers of the auth probe and failed attempts are
    not checked. Region and role are enforced only after the grace of the phase has passed or a
    write has already conformed. The balance of a `201` must equal the anchor plus the net of the
    acknowledged movements, within the drift the failover probe deposits and the abandoned
    movements can explain. A failover deposit is allowed (0 or 1) while its workflow runs; once the
    validate step log is read, a deposit proven not to have run (`settle_failover_deposit`) is no
    longer allowed. A proven deposit stays an allowed upper bound only: a response of a write that
    began before it may still arrive stale.
    """

    def __init__(
        self,
        monotonic: Callable[[], float],
        abandoned: Callable[[], Sequence[Abandoned]] = lambda: (),
    ) -> None:
        self._monotonic = monotonic
        self._abandoned = abandoned
        self._lock = threading.Lock()
        self._region: str | None = None
        self._role: str | None = None
        self._grace_until = 0.0
        self._converged = False
        self._violation: Violation | None = None
        self._anchor: int | None = None
        self._net = 0
        self._seen: set[str] = set()
        self._possible = 0

    @property
    def violation(self) -> Violation | None:
        """The first violation seen: (reason, detail), or None."""
        with self._lock:
            return self._violation

    @property
    def converged(self) -> bool:
        """True once a write answered from the expected region with the expected role."""
        with self._lock:
            return self._converged

    def expect(self, region: str | None, role: str | None, grace_seconds: float = 0.0) -> None:
        """Start a phase: answers must come from `region` with `role` (None = switch window)."""
        with self._lock:
            self._region = region
            self._role = role
            self._grace_until = self._monotonic() + grace_seconds
            self._converged = False

    def anchor(self, balance_cents: int) -> None:
        """The balance read before the first probe write."""
        with self._lock:
            self._anchor = balance_cents - self._net

    def allow_failover_deposit(self) -> None:
        """A failover is about to run: it may add one 0.01 probe deposit (0 or 1)."""
        with self._lock:
            self._possible += 1

    def settle_failover_deposit(self, outcome: ProbeDeposit) -> None:
        """The log of the failover is read: a deposit that did not run is no longer allowed."""
        if outcome in (ProbeDeposit.SKIPPED, ProbeDeposit.REJECTED):
            with self._lock:
                self._possible -= 1

    def feed(self, observation: Observation) -> None:
        """Check one probe observation."""
        if not observation.ok:
            return
        with self._lock:
            if observation.probe in CHECKED_PROBES:
                self._check_location(observation)
            if observation.probe == "write":
                self._check_chain(observation)

    def _violate(self, reason: str, detail: str) -> None:
        if self._violation is None:
            self._violation = (reason, detail)

    def _fits(self, observation: Observation) -> bool:
        role_ok = observation.database_role == self._role or (
            observation.database_role is None and observation.probe != "write"
        )
        processed_ok = observation.processed_region in (None, self._region)
        return observation.served_by_region == self._region and role_ok and processed_ok

    def _check_location(self, observation: Observation) -> None:
        if self._region is None or observation.served_by_region is None:
            return
        if self._fits(observation):
            self._converged = self._converged or observation.probe == "write"
        elif self._converged or self._monotonic() >= self._grace_until:
            self._violate(
                REASON_WRONG_REGION_OR_ROLE,
                f"{observation.probe} {observation.label} was answered by "
                f"{observation.served_by_region} with role {observation.database_role}, "
                f"expected {self._region} with role {self._role}",
            )

    def _drift_limits(self) -> tuple[int, int]:
        items = self._abandoned()
        deposits = sum(1 for item in items if item.kind == DEPOSIT)
        withdrawals = len(items) - deposits
        return -withdrawals, self._possible + deposits

    def _check_chain(self, observation: Observation) -> None:
        key, kind = observation.key, observation.kind
        if key is None or kind not in KINDS or key in self._seen:
            return
        self._seen.add(key)
        self._net += 1 if kind == DEPOSIT else -1
        balance = observation.balance_cents
        if observation.status != CREATED or balance is None:
            return
        if self._anchor is None:
            self._anchor = balance - self._net
            return
        drift = balance - (self._anchor + self._net)
        low, high = self._drift_limits()
        if not low <= drift <= high:
            self._violate(
                REASON_BALANCE_CHAIN,
                f"write {observation.label} left a balance drift of {drift} cents, "
                f"explainable range {low} to {high}",
            )


@dataclass(frozen=True)
class DrillConfig:
    """Regions and timings of the drill."""

    primary: str
    secondary: str
    steady_seconds: float = DEFAULT_STEADY_SECONDS
    grace_seconds: float = DEFAULT_GRACE_SECONDS


@dataclass
class DrillResult:
    """What a drill produced; the caller writes the outputs from it and the timeline."""

    verdict: str
    exit_code: int
    reason: str | None
    detail: str | None
    ledger: LedgerReport | None
    runs: tuple[RunDetails, ...]
    warnings: tuple[str, ...]


@dataclass
class DrillDeps:
    """Collaborators of the drill; tests replace every one of them with a fake."""

    workflows: Workflows
    observers: StateObserver
    api: LedgerApi
    token: Callable[[], str]
    probes: ProbesPort
    write: WriteStats
    monitor: ProbeMonitor
    timeline: Timeline
    clock: ProbeClock
    status: Callable[[str, str, str], object]
    out: Callable[[str], object]
    finalize: Callable[[DrillResult], object]


class _Stop(Exception):  # noqa: N818 - a control-flow signal, not an error
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class _Snapshot:
    label: str
    fence: dict[str, dict[str, int | None]]
    weights: dict[str, int] | None
    writer: str | None
    errors: tuple[str, ...]

    def to_data(self) -> dict[str, object]:
        return {
            "label": self.label,
            "fence": self.fence,
            "weights": self.weights,
            "writer": self.writer,
            "errors": list(self.errors),
        }


class Drill:
    """Runs the phases in order; every exit path stops the probe and returns a result."""

    def __init__(self, config: DrillConfig, deps: DrillDeps) -> None:
        self._config = config
        self._deps = deps
        self._probes_started = False
        self._probes_stopped = False
        self._runs: list[RunDetails] = []
        self._deposits: list[ProbeDeposit] = []
        self._dispatched = False
        self._warnings: list[str] = []
        self._ledger: LedgerReport | None = None
        self._last_snapshot: _Snapshot | None = None

    def run(self) -> DrillResult:
        """Play the drill; an unexpected error is recorded and written, then propagates."""
        verdict, reason, detail, error = self._play()
        result = DrillResult(
            verdict=verdict,
            exit_code=self._exit_code(verdict),
            reason=reason,
            detail=detail,
            ledger=self._ledger,
            runs=tuple(self._runs),
            warnings=tuple(self._warnings),
        )
        self._record_phase(
            {"name": "RESULT", "verdict": verdict, "reason": reason, "detail": detail}
        )
        self._deps.finalize(result)
        if error is not None:
            raise error
        return result

    @staticmethod
    def _exit_code(verdict: str) -> int:
        if verdict == VERDICT_PASS:
            return EXIT_OK
        return EXIT_INTERRUPTED if verdict == VERDICT_ABORTED else EXIT_STOPPED

    def _play(self) -> tuple[str, str | None, str | None, Exception | None]:
        verdict: str = VERDICT_PASS
        reason: str | None = None
        detail: str | None = None
        unexpected: Exception | None = None
        label = "stopped"
        try:
            self._sequence()
        except _Stop as stop:
            verdict, reason, detail = VERDICT_FAIL, stop.reason, stop.detail
            self._record_phase({"name": "ABORT", "reason": stop.reason, "detail": stop.detail})
        except KeyboardInterrupt:
            verdict, label = VERDICT_ABORTED, "interrupted"
            self._record_phase({"name": "INTERRUPTED"})
        except Exception as error:
            unexpected = error
            verdict, reason, detail = VERDICT_FAIL, REASON_UNEXPECTED_ERROR, type(error).__name__
            self._record_phase({"name": "ABORT", "reason": reason, "detail": detail})
        finally:
            self._shutdown(label)
        if verdict in (VERDICT_FAIL, VERDICT_ABORTED):
            self._print_state(reason, detail)
        elif self._ledger is not None and (
            not self._ledger.verified or self._ledger.rpo_status == "not_verifiable"
        ):
            verdict, reason = VERDICT_INCONCLUSIVE, REASON_RPO_NOT_VERIFIED
            detail = "; ".join(self._ledger.notes)
            self._deps.out(f"Inconclusive: {detail}")
        return verdict, reason, detail, unexpected

    def _record_phase(self, data: dict[str, Any]) -> None:
        """A phase event of an end path; a refused one becomes a note, so the drill still ends."""
        try:
            self._deps.timeline.add("phase", data)
        except RefusedOutput as refused:
            self._deps.timeline.note(f"phase {data['name']} event refused: {refused.reason}")

    def _sequence(self) -> None:
        config = self._config
        self._snapshot("probe-start")
        baseline = self._read_baseline()
        self._probes_started = True
        self._deps.probes.start()
        self._steady(PHASE_BASELINE, config.primary)
        fence = {"action": "fence", "region": config.primary, "confirm": config.primary}
        self._workflow(PHASE_FENCE, "simulate-outage", fence, config.primary)
        to_secondary = {"direction": PHASE_TO_SECONDARY, "confirm": FAILOVER_CONFIRMATION}
        self._workflow(PHASE_TO_SECONDARY, "failover", to_secondary, config.secondary, True)
        self._steady(PHASE_STEADY_SECONDARY, config.secondary)
        to_primary = {"direction": PHASE_TO_PRIMARY, "confirm": FAILOVER_CONFIRMATION}
        self._workflow(PHASE_TO_PRIMARY, "failover", to_primary, config.primary, True)
        self._steady(PHASE_STEADY_PRIMARY, config.primary)
        self._final_ledger(baseline)

    def _begin(self, phase: str, region: str) -> None:
        self._deps.timeline.add("phase", {"name": phase, "state": "start"})
        self._deps.status(phase, region, "started")

    def _finish(self, phase: str, region: str, result: str = "done") -> None:
        self._deps.timeline.add("phase", {"name": phase, "state": "end"})
        self._deps.status(phase, region, result)

    def _check_live(self, errors_before: int | None = None) -> None:
        violation = self._deps.monitor.violation
        if violation is not None:
            raise _Stop(*violation)
        if errors_before is not None and self._deps.write.probe_errors > errors_before:
            raise _Stop(
                REASON_PROBE_ERROR,
                "a probe movement was rejected with a permanent client error",
            )

    def _read_baseline(self) -> LedgerReading:
        try:
            token = self._deps.token()
            account = read_account(self._deps.api.get_account(token))
            consistency = read_consistency(self._deps.api.get_consistency(token))
        except AuthError:
            raise _Stop(REASON_BASELINE_UNREADABLE, "the e2e user could not sign in") from None
        if account is None:
            raise _Stop(REASON_BASELINE_UNREADABLE, "GET /account did not answer a usable body")
        if consistency is not None and consistency.consistent is False:
            raise _Stop(REASON_INCONSISTENT, "the ledger was already inconsistent before the drill")
        if consistency is None:
            self._warn("the baseline GET /consistency could not be read")
        self._deps.monitor.anchor(account.balance_cents)
        return account

    def _steady(self, phase: str, region: str) -> None:
        clock = self._deps.clock
        self._begin(phase, region)
        self._deps.monitor.expect(region, WRITER_ROLE, self._config.grace_seconds)
        errors_before = self._deps.write.probe_errors
        end = clock.monotonic() + self._config.steady_seconds
        while (now := clock.monotonic()) < end:
            clock.sleep(min(POLL_SECONDS, end - now))
            self._check_live(errors_before)
        if not self._deps.monitor.converged:
            raise _Stop(
                REASON_NOT_CONVERGED,
                f"no write was answered by {region} with role {WRITER_ROLE} during {phase}",
            )
        self._finish(phase, region)

    def _workflow(
        self,
        phase: str,
        workflow: str,
        inputs: Mapping[str, str],
        region: str,
        failover: bool = False,
    ) -> None:
        deps = self._deps
        self._check_live()
        self._begin(phase, region)
        deps.monitor.expect(None, None)
        errors_before = deps.write.probe_errors
        if failover:
            deps.monitor.allow_failover_deposit()
        try:
            details = self._run_workflow(phase, workflow, inputs, region)
        except GithubError as error:
            raise _Stop(
                REASON_WORKFLOW_ERROR,
                f"{error}; a dispatched run may still be running: check it in GitHub before "
                "anything else",
            ) from None
        self._runs.append(details)
        if failover and details.conclusion is not None:
            self._settle_probe_deposit(phase, details.run_id)
        self._snapshot(f"after-{phase}")
        new_errors = deps.write.probe_errors - errors_before
        if new_errors > 0:
            self._warn(f"{new_errors} probe error(s) during {phase}: movements rejected")
        if details.conclusion != "success":
            raise _Stop(REASON_WORKFLOW_FAILED, _not_success(workflow, details))
        self._check_live()
        self._finish(phase, region)

    def _run_workflow(
        self, phase: str, workflow: str, inputs: Mapping[str, str], region: str
    ) -> RunDetails:
        deps = self._deps
        self._dispatched = True
        dispatched = deps.workflows.dispatch(workflow, inputs)
        base = {"phase": phase, "workflow": workflow}
        deps.timeline.add(
            "workflow_step", {**base, "event": "dispatch", "inputs": dict(inputs)}, dispatched
        )
        deps.status(phase, region, f"dispatched {workflow}")
        run_id = deps.workflows.find_run(workflow, dispatched)
        deps.timeline.add(
            "workflow_step", {**base, "event": "run_found", "run_id": run_id}, dispatched
        )
        succeeded = deps.workflows.wait(run_id)
        details = deps.workflows.details(run_id)
        self._record_run(base, details, dispatched)
        deps.status(phase, region, f"run {details.conclusion or 'unknown'}")
        if not succeeded and details.conclusion == "success":
            self._warn(f"the wait for {workflow} failed but the run says success: not trusted")
            return replace(details, conclusion=None)
        return details

    def _record_run(self, base: dict[str, str], details: RunDetails, dispatched: datetime) -> None:
        timeline = self._deps.timeline
        created = details.created or dispatched
        timeline.add(
            "workflow_step",
            {
                **base,
                "event": "run",
                "run_id": details.run_id,
                "status": details.status,
                "conclusion": details.conclusion,
                "created": _stamp(details.created),
                "started": _stamp(details.started),
                "updated": _stamp(details.updated),
            },
            created,
        )
        for job in details.jobs:
            job_data = {
                **base,
                "event": "job",
                "job": job.name,
                "conclusion": job.conclusion,
                "start": _stamp(job.started),
                "end": _stamp(job.completed),
            }
            timeline.add("workflow_step", job_data, job.started or created)
            for step in job.steps:
                step_data = {
                    **base,
                    "event": "step",
                    "job": job.name,
                    "step": step.name,
                    "conclusion": step.conclusion,
                    "start": _stamp(step.started),
                    "end": _stamp(step.completed),
                }
                timeline.add("workflow_step", step_data, step.started or created)

    def _settle_probe_deposit(self, phase: str, run_id: int) -> None:
        """Learn from the validate step log whether the failover posted its 0.01 deposit.

        Also read when the run failed: a rejected probe fails the validate step, and the abort
        report needs the accounting. The failure itself still stops the drill.
        """
        deps = self._deps
        try:
            outcome = classify_probe_deposit(deps.workflows.step_log(run_id, VALIDATE_STEP))
        except GithubError:
            outcome = ProbeDeposit.UNKNOWN
            deps.timeline.note(f"the validate step log of {phase} could not be read")
        if outcome is ProbeDeposit.REJECTED:
            self._warn(
                f"the probe deposit of {phase} returned an HTTP error: counted as not posted"
            )
        deps.monitor.settle_failover_deposit(outcome)
        self._deposits.append(outcome)
        deps.timeline.add(
            "workflow_step",
            {
                "phase": phase,
                "workflow": "failover",
                "event": "probe_deposit",
                "outcome": outcome.value,
            },
        )

    def _final_ledger(self, baseline: LedgerReading) -> None:
        deps = self._deps
        self._begin(PHASE_CONSISTENCY, self._config.primary)
        self._stop_probes("probe-end")
        final = consistency = None
        try:
            token = deps.token()
            final = read_account(deps.api.get_account(token))
            consistency = read_consistency(deps.api.get_consistency(token))
        except AuthError:
            self._warn("the final ledger readings could not be taken: sign-in failed")
        report = verify_ledger(
            baseline,
            final,
            consistency,
            deps.write.acked,
            deps.write.abandoned,
            deps.write.pending,
            self._deposits.count(ProbeDeposit.DEPOSITED),
            self._deposits.count(ProbeDeposit.UNKNOWN),
        )
        self._ledger = report
        deps.timeline.add(
            "state_snapshot",
            {
                "label": "ledger",
                "failover_deposit_outcomes": [outcome.value for outcome in self._deposits],
                **report.to_data(),
            },
        )
        if consistency is not None and consistency.consistent is False:
            raise _Stop(REASON_INCONSISTENT, "; ".join(report.problems))
        if report.problems:
            raise _Stop(REASON_LEDGER_MISMATCH, "; ".join(report.problems))
        self._finish(PHASE_CONSISTENCY, self._config.primary)

    def _warn(self, text: str) -> None:
        self._warnings.append(text)
        self._deps.timeline.note(f"warning: {text}")

    def _stop_probes(self, label: str) -> None:
        if self._probes_stopped:
            return
        self._probes_stopped = True
        try:
            stopped = self._deps.probes.stop()
        finally:
            self._snapshot(label)
        if not stopped:
            self._warn("a probe did not stop in time")

    def _shutdown(self, label: str) -> None:
        if self._probes_started:
            self._stop_probes(label)

    def _snapshot(self, label: str) -> _Snapshot:
        observers = self._deps.observers
        errors: list[str] = []
        fence: dict[str, dict[str, int | None]] = {}
        for region in (self._config.primary, self._config.secondary):
            try:
                fence[region] = observers.fence_state(region)
            except Exception as error:
                errors.append(f"fence {region}: {type(error).__name__}")
        weights = writer = None
        try:
            weights = observers.route53_weights()
        except Exception as error:
            errors.append(f"weights: {type(error).__name__}")
        try:
            writer = observers.writer_region()
        except Exception as error:
            errors.append(f"writer: {type(error).__name__}")
        snapshot = _Snapshot(label, fence, weights, writer, tuple(errors))
        self._last_snapshot = snapshot
        self._deps.timeline.add("state_snapshot", snapshot.to_data())
        return snapshot

    def _print_state(self, reason: str | None, detail: str | None) -> None:
        out = self._deps.out
        if reason:
            out(f"Stopped: {reason}" + (f" ({detail})" if detail else ""))
        snapshot = self._last_snapshot
        out(f"State: writer region {(snapshot.writer if snapshot else None) or 'unknown'}")
        for region in (self._config.primary, self._config.secondary):
            state = snapshot.fence.get(region) if snapshot else None
            if state is None:
                out(f"Fence {region}: unknown")
                continue
            fenced = sum(1 for value in state.values() if value is not None)
            out(f"Fence {region}: {fenced} of {len(state)} functions have reserved concurrency set")
        if self._dispatched:
            out("A dispatched workflow run may still be running: check it in GitHub first.")
        out(f"Do not retry and do not unfence by hand. Follow {RUNBOOK}.")


def _not_success(workflow: str, details: RunDetails) -> str:
    text = f"{workflow} ended with conclusion {details.conclusion or 'unknown'}"
    if details.conclusion is None or details.status != "completed":
        text += "; the run may still be running: check it in GitHub before anything else"
    return text


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else format_utc(moment)
