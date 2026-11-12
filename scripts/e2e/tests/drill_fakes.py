"""Fakes for the drill tests: a guarded fake clock, scripted workflows and a probe scenario."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from dr_drill.api import FieldValue, Sample
from dr_drill.auth import AuthError
from dr_drill.drill import Drill, DrillConfig, DrillDeps, DrillResult, ProbeMonitor
from dr_drill.github import JobTiming, RunDetails, StepTiming
from dr_drill.probe import Abandoned, Acknowledged, Observation, Pending
from dr_drill.redact import OutputGuard
from dr_drill.timeline import Timeline
from tests.probe_fakes import START, FakeTime

MAX_SLEEPS = 5000
PRIMARY = "us-east-2"
SECONDARY = "us-west-2"
FENCE_INPUTS = {"action": "fence", "region": PRIMARY, "confirm": PRIMARY}
TO_SECONDARY = {"direction": "to-secondary", "confirm": "failover"}
TO_PRIMARY = {"direction": "to-primary", "confirm": "failover"}
VALIDATE = "Group 3 - validate the service on the custom domain"
KINDS = ("deposit", "withdrawal")
BASELINE_COUNT = 10
BASELINE_BALANCE = 10_000


class DrillTime(FakeTime):
    """Fake clock that ends the test (fails) if a loop does not stop, and ticks the scenario."""

    def __init__(self) -> None:
        super().__init__()
        self.ticker: Callable[[], None] = lambda: None
        self.sleep_calls = 0

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1
        assert self.sleep_calls < MAX_SLEEPS, "a fake clock loop did not stop"
        super().sleep(seconds)
        self.ticker()


class FakeWrite:
    """Counters of the write probe (`WriteStats`)."""

    def __init__(self) -> None:
        self.acked: list[Acknowledged] = []
        self.abandoned: list[Abandoned] = []
        self.pending: list[Pending] = []
        self.probe_errors = 0


class FakeProbes:
    """`ProbesPort`: records starts and stops."""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.stuck = False

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> bool:
        self.stops += 1
        return not self.stuck


class Scenario:
    """Plays the probe: one acknowledged write per fake second, served by the current region."""

    def __init__(self, time: DrillTime, monitor: ProbeMonitor, write: FakeWrite) -> None:
        self.time = time
        self.monitor = monitor
        self.write = write
        self.region = PRIMARY
        self.role = "writer"
        self.balance = BASELINE_BALANCE
        self.deposits = 0
        self.count = 0
        self.answering = True
        self.balance_glitch_at: int | None = None
        self.wrong_region_after: float | None = None
        self.on_tick: Callable[[Scenario], None] = lambda scenario: None

    def tick(self) -> None:
        self.on_tick(self)
        if not self.answering:
            return
        self.count += 1
        kind = KINDS[(self.count - 1) % 2]
        self.balance += 1 if kind == "deposit" else -1
        if self.balance_glitch_at == self.count:
            self.balance += 5
        region = self.region
        if self.wrong_region_after is not None and self.time.now >= self.wrong_region_after:
            region = SECONDARY if self.region == PRIMARY else PRIMARY
        key = f"key-{self.count}"
        self.write.acked.append(Acknowledged(f"w{self.count}", key, kind, self.balance, 201))
        self.monitor.feed(
            Observation(
                probe="write",
                label=f"w{self.count}",
                attempt=1,
                start_utc=self.time.utc(),
                end_utc=self.time.utc(),
                ok=True,
                status=201,
                latency_ms=10.0,
                key=key,
                kind=kind,
                served_by_region=region,
                processed_region=region,
                database_role=self.role,
                balance_cents=self.balance,
            )
        )

    def failover_probe_deposit(self) -> None:
        self.balance += 1
        self.deposits += 1


class FakeWorkflows:
    """`Workflows`: records the calls; `wait` advances the clock and plays the switch."""

    def __init__(self, time: DrillTime, scenario: Scenario) -> None:
        self.time = time
        self.scenario = scenario
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.conclusions: dict[str, str] = {}
        self.interrupt_on: str | None = None
        self.probe_deposits = True
        self.wait_ok: bool | None = None
        self.run_status = "completed"
        self.dispatch_error: Exception | None = None
        self.details_error: Exception | None = None
        self.step_logs: dict[str, str] = {}
        self.log_reads: list[tuple[int, str]] = []
        self.log_error: Exception | None = None
        self._current = ""
        self._runs = 0

    @staticmethod
    def _key(workflow: str, inputs: Mapping[str, str]) -> str:
        return workflow if workflow != "failover" else f"failover:{inputs['direction']}"

    def dispatch(self, workflow: str, inputs: Mapping[str, str]) -> datetime:
        if self.dispatch_error:
            raise self.dispatch_error
        self.calls.append(("dispatch", workflow, dict(inputs)))
        self._current = self._key(workflow, inputs)
        return self.time.utc()

    def find_run(self, workflow: str, after: datetime) -> int:
        self._runs += 1
        return 1000 + self._runs

    def wait(self, run_id: int) -> bool:
        self.time.advance(240.0)
        if self.interrupt_on == self._current:
            raise KeyboardInterrupt
        conclusion = self.conclusions.get(self._current, "success")
        if conclusion == "success":
            if self._current == "failover:to-secondary":
                self.scenario.region = SECONDARY
            if self._current == "failover:to-primary":
                self.scenario.region = PRIMARY
            if self._current.startswith("failover") and self.probe_deposits:
                self.scenario.failover_probe_deposit()
        return conclusion == "success" if self.wait_ok is None else self.wait_ok

    def step_log(self, run_id: int, step_name: str) -> str:
        self.log_reads.append((run_id, step_name))
        if self.log_error:
            raise self.log_error
        return self.step_logs.get(self._current, "")

    def details(self, run_id: int) -> RunDetails:
        if self.details_error:
            raise self.details_error
        conclusion = self.conclusions.get(self._current, "success")
        start = self.time.utc() - timedelta(seconds=240)
        steps = (StepTiming("Run 10-simulate-outage.sh", conclusion, start, self.time.utc()),)
        if self._current.startswith("failover"):
            steps = (StepTiming(VALIDATE, conclusion, start, self.time.utc()),)
        job = JobTiming("prod", conclusion, start, self.time.utc(), steps)
        return RunDetails(
            run_id, self.run_status, conclusion, start, start, self.time.utc(), (job,)
        )


class FakeObservers:
    """`StateObserver`: a fixed state; can fail."""

    def __init__(self) -> None:
        self.fail = False
        self.fenced_regions: set[str] = set()
        self.writer = PRIMARY
        self.calls = 0

    def fence_state(self, region: str) -> dict[str, int | None]:
        self._maybe_fail()
        value = 0 if region in self.fenced_regions else None
        return {f"fnc-{region.replace('-', '')}-drdemo-fn{n}-prod": value for n in range(6)}

    def writer_region(self) -> str | None:
        self._maybe_fail()
        return self.writer

    def route53_weights(self) -> dict[str, int]:
        self._maybe_fail()
        return {"use2": 255, "usw2": 0}

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")


class FakeLedgerApi:
    """`LedgerApi`: the first `/account` is the baseline, the next one is the final reading."""

    def __init__(self, scenario: Scenario, write: FakeWrite) -> None:
        self.scenario = scenario
        self.write = write
        self.account_calls = 0
        self.baseline_status = 200
        self.final_count_delta = 0
        self.consistent: bool | None = True
        self.truncated = False
        self.consistency_status = 200
        self.consistency_calls = 0
        self.baseline_consistent = True

    def get_account(self, token: str) -> Sample:
        self.account_calls += 1
        if self.account_calls == 1:
            fields: dict[str, FieldValue] = {
                "balance_cents": BASELINE_BALANCE,
                "movement_count": BASELINE_COUNT,
            }
            return Sample(START, START, self.baseline_status, 1.0, body_fields=fields)
        count = BASELINE_COUNT + len(self.write.acked) + self.scenario.deposits
        fields = {
            "balance_cents": self.scenario.balance,
            "movement_count": count + self.final_count_delta,
        }
        return Sample(START, START, 200, 1.0, body_fields=fields)

    def get_consistency(self, token: str) -> Sample:
        self.consistency_calls += 1
        fields: dict[str, FieldValue] = {"truncated": self.truncated, "duplicate_keys": 0}
        if self.consistent is not None:
            fields["consistent"] = self.consistent
        if self.consistency_calls == 1:  # the baseline reading
            fields["consistent"] = self.baseline_consistent
        return Sample(START, START, self.consistency_status, 1.0, body_fields=fields)


class Rig:
    """Everything wired together; `run()` returns the result."""

    def __init__(self, steady: int = 60, grace: int = 30) -> None:
        self.time = DrillTime()
        self.guard = OutputGuard()
        self.timeline = Timeline("run-1", self.guard, self.time.utc)
        self.write = FakeWrite()
        self.monitor = ProbeMonitor(self.time.monotonic, lambda: tuple(self.write.abandoned))
        self.scenario = Scenario(self.time, self.monitor, self.write)
        self.time.ticker = self.scenario.tick
        self.workflows = FakeWorkflows(self.time, self.scenario)
        self.observers = FakeObservers()
        self.api = FakeLedgerApi(self.scenario, self.write)
        self.probes = FakeProbes()
        self.status: list[tuple[str, str, str]] = []
        self.out: list[str] = []
        self.finalized: list[DrillResult] = []
        self.token_calls = 0
        self.failing_token_calls: set[int] = set()
        self.config = DrillConfig(PRIMARY, SECONDARY, steady_seconds=steady, grace_seconds=grace)

    def drill(self) -> Drill:
        deps = DrillDeps(
            workflows=self.workflows,
            observers=self.observers,
            api=self.api,
            token=self._token,
            probes=self.probes,
            write=self.write,
            monitor=self.monitor,
            timeline=self.timeline,
            clock=self.time.clock(),
            status=lambda phase, region, result: self.status.append((phase, region, result)),
            out=self.out.append,
            finalize=self.finalized.append,
        )
        return Drill(self.config, deps)

    def _token(self) -> str:
        self.token_calls += 1
        if self.token_calls in self.failing_token_calls:
            raise AuthError("sign-in failed")
        return "fake-token"

    def run(self) -> DrillResult:
        return self.drill().run()

    def phases(self) -> list[str]:
        return [
            str(event.data["name"])
            for event in self.timeline.events("phase")
            if event.data.get("state") == "start"
        ]

    def dispatches(self) -> list[tuple[str, dict[str, str]]]:
        return [(workflow, inputs) for _, workflow, inputs in self.workflows.calls]

    def abort_reason(self) -> str | None:
        for event in self.timeline.events("phase"):
            if event.data.get("name") == "ABORT":
                return str(event.data["reason"])
        return None
