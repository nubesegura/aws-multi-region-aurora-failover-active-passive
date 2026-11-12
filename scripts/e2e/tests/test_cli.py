import io
import json
import runpy
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest

from dr_drill import cli
from dr_drill.api import Sample
from dr_drill.auth import AuthProbeResult
from dr_drill.aws_ports import build_ports
from dr_drill.cli import (
    Dependencies,
    ProbeRig,
    ProbeWiring,
    RunContext,
    Services,
    build_probes,
    default_services,
    main,
    tee,
)
from dr_drill.config import SettingsError
from dr_drill.github import GithubError
from dr_drill.observers import LagSample
from dr_drill.probe import Abandoned, Acknowledged, Observation, Pending
from dr_drill.redact import OutputGuard, RefusedOutput
from dr_drill.runlog import RunLog
from dr_drill.timeline import Timeline
from tests.drill_fakes import (
    DrillTime,
    FakeLedgerApi,
    FakeObservers,
    FakeProbes,
    FakeWorkflows,
    FakeWrite,
    Scenario,
)
from tests.fakes import ACCOUNT, FakePorts, make_settings
from tests.probe_fakes import START

MOMENT = datetime(2026, 11, 11, 12, 0, 0, 5000, tzinfo=UTC)
PASSWORD = "Fake-Password-123!"
SKIPPED_LOG = "step 3/3: probe movement (deposit of 0.01)\n  skipped (no COGNITO_ID_TOKEN provided)"
RUN_FILES = {"run.log", "timeline.json", "report.md"}


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this module may start `gh`, resolve a name or open an AWS session."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test reached a real service")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(boto3, "Session", refuse)


class Harness:
    def __init__(self, tmp_path: Path, answers: list[str] | None = None) -> None:
        self.fake = FakePorts()
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.prompts: list[str] = []
        self.answers = answers if answers is not None else ["drill"]
        self.contexts: list[RunContext] = []
        self.reports: list[list[Path]] = []
        self.drill_code = 0
        self.deps = Dependencies(
            load_settings=make_settings,
            ports_factory=lambda settings, guard: self.fake.ports(),
            drill=self._drill,
            probe_smoke=self._drill,
            formal_report=self._report,
            input_fn=self._input,
            out=self.out,
            err=self.err,
            clock=lambda: MOMENT,
            runs_dir=tmp_path / "runs",
        )

    def _input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def _drill(self, context: RunContext) -> int:
        self.contexts.append(context)
        return self.drill_code

    def _report(self, runs: Sequence[Path]) -> int:
        self.reports.append(list(runs))
        return 0

    def run(self, *argv: str) -> int:
        return main(list(argv), self.deps)


def test_check_prints_one_line_per_check_and_exits_zero(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    assert harness.run("check") == 0

    lines = harness.out.getvalue().splitlines()
    assert len(lines) == 8
    assert lines[0] == "PASS aws identity: account ********9012"
    assert ACCOUNT not in harness.out.getvalue()


def test_check_with_a_failed_precondition_exits_three(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.fake.health = 503

    assert harness.run("check") == 3
    assert "FAIL api health: GET /health answered 503" in harness.out.getvalue()


def test_run_shows_the_plan_and_starts_after_the_typed_confirmation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    assert harness.run("run") == 0

    shown = harness.out.getvalue()
    assert "Account: ********9012" in shown
    assert "primary us-east-2, secondary us-west-2, global us-east-1" in shown
    assert "probe movements" in shown
    assert shown.count("gh workflow run") == 3
    assert "action=fence region=us-east-2 confirm=us-east-2" in shown
    assert "direction=to-secondary confirm=failover" in shown
    assert "direction=to-primary confirm=failover" in shown
    assert harness.prompts == ["Type 'drill' to start: "]
    assert len(harness.contexts) == 1


def test_wrong_confirmation_refuses_and_writes_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, answers=["yes"])

    assert harness.run("run") == 1

    assert harness.contexts == []
    assert "Confirmation refused" in harness.err.getvalue()
    assert not (tmp_path / "runs").exists()


def test_closed_input_counts_as_a_refusal(tmp_path: Path) -> None:
    harness = Harness(tmp_path, answers=[])

    assert harness.run("run") == 1


def test_authorized_skips_the_prompt_and_records_it(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    assert harness.run("run", "--authorized") == 0

    assert harness.prompts == []
    log = harness.contexts[0].log.path
    assert log == tmp_path / "runs" / "2026-11-11T12-00-00Z" / "run.log"
    line = log.read_text(encoding="utf-8").splitlines()[0]
    assert line == "2026-11-11T12:00:00.005Z | start | confirmation | authorized by the owner | -"
    assert line in harness.out.getvalue()


def test_status_line_format_goes_to_log_and_console(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.run("run", "--authorized")
    context = harness.contexts[0]

    line = cli.announce(context.log, context.out, "fence", "us-east-2", "dispatched")

    assert line == "2026-11-11T12:00:00.005Z | fence | phase | dispatched | us-east-2"
    assert harness.out.getvalue().splitlines()[-1] == line


def test_failed_preconditions_stop_a_run_before_the_prompt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.fake.fenced.add("fnc-useast2-drdemo-get-stats-prod")

    assert harness.run("run") == 3

    assert harness.prompts == []
    assert harness.contexts == []
    assert "nothing was written" in harness.err.getvalue()


def test_handler_exit_code_is_returned(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.drill_code = 1

    assert harness.run("run", "--authorized") == 1


def test_probe_smoke_lists_only_probe_writes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    assert harness.run("probe-smoke") == 0

    assert "gh workflow run" not in harness.out.getvalue()
    assert "probe movements" in harness.out.getvalue()


def test_missing_handler_is_a_usage_error_before_any_prompt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.deps.drill = None

    assert harness.run("run") == 2

    assert harness.prompts == []
    assert "not available" in harness.err.getvalue()


def test_formal_report_passes_the_run_folders(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    assert harness.run("formal-report", "--run", "a", "--run", "b") == 0

    assert harness.reports == [[Path("a"), Path("b")]]


def test_formal_report_without_handler(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.deps.formal_report = None

    assert harness.run("formal-report", "--run", "a") == 2


def test_settings_error_is_exit_two_without_values(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def broken() -> None:
        raise SettingsError("missing required settings: E2E_USERNAME")

    harness.deps.load_settings = broken  # type: ignore[assignment]

    assert harness.run("check") == 2
    assert "E2E_USERNAME" in harness.err.getvalue()


def test_a_settings_error_that_carries_a_secret_shape_is_refused(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def broken() -> None:
        raise SettingsError("bad value " + "Ab9_" * 12)

    harness.deps.load_settings = broken  # type: ignore[assignment]

    assert harness.run("check") == 2
    assert "Ab9_" not in harness.err.getvalue()
    assert "output refused" in harness.err.getvalue()


def test_missing_live_adapters_is_a_usage_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.deps.ports_factory = None

    assert harness.run("check") == 2


def test_usage_errors_exit_two_and_help_exits_zero(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    assert harness.run() == 2
    assert harness.run("formal-report") == 2
    assert harness.run("--help") == 0


def test_interruption_exits_130_and_points_to_the_runbook(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def interrupted(context: RunContext) -> int:
        raise KeyboardInterrupt

    harness.deps.drill = interrupted

    assert harness.run("run", "--authorized") == 130
    assert "docs/dr-runbook.md" in harness.err.getvalue()


def test_console_guard_refuses_a_secret(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.fake.account = "123456789012"
    harness.fake.stack_statuses["stk-useast2-drdemo-edge-prod"] = "Fake-Password-123!"

    harness.run("check")

    assert "Fake-Password-123!" not in harness.out.getvalue()
    assert "output refused: console" in harness.err.getvalue()


def test_default_dependencies_read_the_runner_folder() -> None:
    deps = cli.default_dependencies()

    assert deps.runs_dir.parts[-2:] == ("reports", "e2e")
    assert deps.ports_factory is build_ports


def test_module_entry_point_runs_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["dr_drill", "--help"])
    sys.modules.pop("dr_drill.__main__", None)

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("dr_drill", run_name="__main__")

    assert raised.value.code == 0


def test_an_unexpected_error_exits_four_with_the_class_name_only(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def broken(context: RunContext) -> int:
        raise RuntimeError("secret detail Fake-Password-123!")

    harness.deps.drill = broken

    assert harness.run("run", "--authorized") == 4
    assert "RuntimeError" in harness.err.getvalue()
    assert "Fake-Password-123!" not in harness.err.getvalue()
    assert "Traceback" not in harness.err.getvalue()


def test_a_refused_status_line_in_a_handler_exits_four(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def refused(context: RunContext) -> int:
        raise RefusedOutput("result", "it contains an ARN")

    harness.deps.drill = refused

    assert harness.run("run", "--authorized") == 4
    assert "RefusedOutput" in harness.err.getvalue()


def test_an_adapter_error_while_planning_exits_four(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    calls = {"count": 0}
    original = harness.fake.caller_account

    def flaky() -> str:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("down")
        return original()

    harness.fake.caller_account = flaky  # type: ignore[method-assign]

    assert harness.run("run", "--authorized") == 4


def test_a_refused_fail_line_is_replaced_by_a_redacted_one(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.fake.health = 503
    harness.fake.stack_statuses["stk-useast2-drdemo-edge-prod"] = None
    harness.fake.stack_statuses["stk-useast1-drdemo-dns-prod"] = "arn:aws:fake"

    harness.run("check")

    assert "FAIL stacks: [redacted]" in harness.out.getvalue().splitlines()


def test_the_plan_names_the_primary_region_from_the_settings(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.deps.load_settings = lambda: replace(
        make_settings(), region_primary="eu-west-1", region_secondary="eu-west-2"
    )
    harness.fake.writer = "eu-west-1"

    harness.run("run", "--authorized")

    assert "action=fence region=eu-west-1 confirm=eu-west-1" in harness.out.getvalue()


class LagObservers(FakeObservers):
    """The state observers plus the replication lag read of the finalize hook."""

    def __init__(self) -> None:
        super().__init__()
        self.lag_error: BaseException | None = None
        self.region = "us-east-2"
        self.windows: list[tuple[datetime, datetime]] = []

    def replication_lag(self, start: datetime, end: datetime) -> list[LagSample]:
        self.windows.append((start, end))
        if self.lag_error:
            raise self.lag_error
        metric = "AuroraGlobalDBReplicationLag"
        return [
            LagSample(self.region, metric, START + timedelta(minutes=1), 80.0),
            LagSample(self.region, metric, START + timedelta(minutes=2), 120.123456789012),
        ]


class SinkTap:
    """Stands in for the monitor of the scenario: its answers go through the real sink."""

    def __init__(self) -> None:
        self.feed: Any = lambda observation: None


class FakeSession:
    def id_token(self) -> str:
        return "fake-token"

    def authenticate_secondary(self) -> AuthProbeResult:
        return AuthProbeResult(True, 5.0, "us-west-2")


class SmokeProbes(FakeProbes):
    """Probes that play a fixed list of observations through the sink when started."""

    def __init__(self, sink: Any, observations: list[Observation]) -> None:
        super().__init__()
        self._sink = sink
        self._observations = observations

    def start(self) -> None:
        super().start()
        for observation in self._observations:
            self._sink(observation)


def _write_observation(status: int, latency: float, region: str = "us-east-2") -> Observation:
    ok = 200 <= status < 300
    return Observation(
        "write", "w0001", 1, START, START, ok, status, latency, served_by_region=region
    )


def acknowledge(fixture: "Fixture", *kinds: str) -> None:
    """The write probe acknowledged these movements; the fake ledger holds them."""
    for number, kind in enumerate(kinds, start=1):
        fixture.write.acked.append(Acknowledged(f"w{number:04d}", f"key-{number}", kind, None, 201))
        fixture.scenario.balance += 1 if kind == "deposit" else -1


class Fixture:
    """Fake services of a whole run: scripted workflows, observers, API and probes."""

    def __init__(self) -> None:
        self.time = DrillTime()
        self.write = FakeWrite()
        self.tap = SinkTap()
        self.scenario = Scenario(self.time, self.tap, self.write)  # type: ignore[arg-type]
        self.time.ticker = self.scenario.tick
        self.workflows = FakeWorkflows(self.time, self.scenario)
        self.workflows.probe_deposits = False
        self.workflows.step_logs = {
            "failover:to-secondary": SKIPPED_LOG,
            "failover:to-primary": SKIPPED_LOG,
        }
        self.observers = LagObservers()
        self.api = FakeLedgerApi(self.scenario, self.write)
        self.probes = FakeProbes()
        self.smoke_observations: list[Observation] = []
        self.wirings: list[ProbeWiring] = []
        self.settles: list[float] = []
        self.settle_hook: Any = None

    def _probes(self, wiring: ProbeWiring) -> ProbeRig:
        self.wirings.append(wiring)
        self.tap.feed = wiring.sink
        if self.smoke_observations:
            self.scenario.answering = False
            self.probes = SmokeProbes(wiring.sink, self.smoke_observations)
        return ProbeRig(self.probes, self.write)

    def _settle(self, seconds: float) -> None:
        if self.settle_hook is not None:
            self.settle_hook(seconds)
        self.settles.append(seconds)
        self.time.advance(seconds)

    def services(self, settings: Any = None, guard: Any = None) -> Services:
        return Services(
            workflows=self.workflows,
            observers=self.observers,
            api=self.api,  # type: ignore[arg-type]
            session=FakeSession(),
            resolver=lambda domain: ("192.0.2.1",),
            clock_factory=lambda stop: self.time.clock(),
            probes_factory=self._probes,
            settle=self._settle,
        )


class RunHarness(Harness):
    """The command line with the default handlers over fake services."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.fixture = Fixture()
        self.deps.drill = None
        self.deps.probe_smoke = None
        self.deps.services_factory = self.fixture.services
        self.folder = tmp_path / "runs" / "2026-11-11T12-00-00Z"

    def files(self) -> set[str]:
        return {path.name for path in self.folder.iterdir()} if self.folder.exists() else set()

    def text(self, name: str) -> str:
        return (self.folder / name).read_text(encoding="utf-8")

    def all_text(self) -> str:
        return "".join(self.text(name) for name in sorted(self.files()))


def test_a_full_fake_run_writes_the_three_files_and_exits_zero(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)

    assert harness.run("run", "--authorized") == 0

    assert harness.files() == RUN_FILES
    assert "PASS" in harness.text("report.md")
    log_lines = harness.text("run.log").splitlines()
    assert any(line.endswith("| baseline | phase | started | us-east-2") for line in log_lines[:5])
    assert log_lines[-1].endswith("| outputs | write | 2 of 2 files written | -")
    assert any("| result | verdict | PASS | -" in line for line in log_lines)
    assert json.loads(harness.text("timeline.json"))["run_id"] == "2026-11-11T12-00-00Z"
    assert harness.fixture.probes.starts == harness.fixture.probes.stops == 1


def test_lag_samples_become_metric_sample_events(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)

    harness.run("run", "--authorized")

    samples = [
        event["data"]
        for event in json.loads(harness.text("timeline.json"))["events"]
        if event["kind"] == "metric_sample"
    ]
    metric = "AuroraGlobalDBReplicationLag"
    assert samples == [
        {"metric": metric, "region": "us-east-2", "milliseconds": 80.0},
        {"metric": metric, "region": "us-east-2", "milliseconds": 120.123},
    ]
    start, end = harness.fixture.observers.windows[0]
    assert start < end
    assert "120" in harness.text("report.md")


def test_an_unreadable_lag_is_a_note_not_a_failure(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.observers.lag_error = RuntimeError("denied")

    assert harness.run("run", "--authorized") == 0

    assert "replication lag was not read (RuntimeError)" in harness.text("timeline.json")


def test_a_lag_sample_the_guard_refuses_is_dropped_and_noted(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.observers.region = PASSWORD

    assert harness.run("run", "--authorized") == 0

    assert PASSWORD not in harness.all_text()
    assert PASSWORD not in harness.out.getvalue() + harness.err.getvalue()
    assert "a replication lag sample was refused by the output guard" in harness.text(
        "timeline.json"
    )


def test_an_aborted_run_writes_the_three_files_and_exits_one(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.workflows.conclusions["simulate-outage"] = "failure"

    assert harness.run("run", "--authorized") == 1

    assert harness.files() == RUN_FILES
    assert "WORKFLOW_FAILED" in harness.text("report.md")
    assert len(harness.fixture.workflows.calls) == 1
    assert harness.fixture.probes.stops == 1


def test_an_interrupted_run_writes_the_three_files_and_exits_130(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.workflows.interrupt_on = "failover:to-secondary"

    assert harness.run("run", "--authorized") == 130

    assert harness.files() == RUN_FILES
    assert "ABORTED" in harness.text("report.md")
    assert "INTERRUPTED" in harness.text("timeline.json")
    assert harness.fixture.probes.stops == 1


def test_an_unexpected_error_still_writes_the_files_and_exits_four(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.workflows.dispatch_error = RuntimeError("secret detail " + PASSWORD)

    assert harness.run("run", "--authorized") == 4

    assert harness.files() == RUN_FILES
    assert PASSWORD not in harness.all_text() + harness.err.getvalue()
    assert "UNEXPECTED_ERROR" in harness.text("report.md")


def test_a_report_that_cannot_be_built_still_leaves_the_three_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RunHarness(tmp_path)

    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(cli, "compute_metrics", broken)

    assert harness.run("run", "--authorized") == 0

    assert harness.files() == RUN_FILES
    assert "could not be built (ValueError)" in harness.text("report.md")


def test_a_refused_output_is_not_written_and_is_logged(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.observers.writer = "arn:aws:fake"

    assert harness.run("run", "--authorized") == 4

    assert "arn:aws" not in harness.all_text()
    assert "arn:aws" not in harness.out.getvalue()


def test_a_report_the_guard_refuses_is_not_written_and_the_log_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RunHarness(tmp_path)
    monkeypatch.setattr(cli, "render_run_report", lambda *args: "see arn:aws:fake")

    assert harness.run("run", "--authorized") == 0

    assert harness.files() == {"run.log", "timeline.json"}
    assert "| outputs | refused | report.md was not written because" in harness.text("run.log")
    assert "1 of 2 files written" in harness.text("run.log")


def test_the_run_wires_the_sink_to_the_timeline_and_the_monitor(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)

    harness.run("run", "--authorized")

    wiring = harness.fixture.wirings[0]
    assert wiring.settings.api_domain_name == "api.example.invalid"
    events = json.loads(harness.text("timeline.json"))["events"]
    requests = [event for event in events if event["kind"] == "request"]
    assert len(requests) > 100
    assert requests[0]["data"]["probe"] == "write"


def test_the_sink_tee_calls_every_sink_even_when_one_fails() -> None:
    seen: list[str] = []

    def broken(observation: Observation) -> None:
        raise RuntimeError("sink down")

    sink = tee(broken, lambda observation: seen.append(observation.label))

    with pytest.raises(RuntimeError):
        sink(_write_observation(201, 10.0))

    assert seen == ["w0001"]


def test_probe_smoke_runs_only_the_probes_and_writes_the_three_files(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [
        _write_observation(201, 40.0),
        _write_observation(201, 60.0),
        Observation("auth", "u0001", 1, START, START, True, 200, 5.0, served_by_region="us-west-2"),
    ]
    acknowledge(harness.fixture, "deposit", "withdrawal")

    assert harness.run("probe-smoke", "--authorized") == 0

    assert harness.files() == RUN_FILES
    assert harness.fixture.workflows.calls == []
    assert harness.fixture.api.account_calls == 2
    assert harness.fixture.api.consistency_calls == 1
    shown = harness.out.getvalue()
    assert "us-east-2: 2 attempts, 0 errors, median 40.0 ms, max 60.0 ms" in shown
    assert "2 acknowledged, 0 abandoned, 0 pending; expected net balance effect 0.00" in shown
    assert (
        "ledger: 12 movements stored, expected 12 plus up to 0 unknown; consistent: True" in shown
    )
    assert "us-east-2: 2 attempts" in harness.text("report.md")
    assert harness.fixture.probes.stops == 1
    assert harness.fixture.time.now >= 60.0


def test_probe_smoke_with_a_failed_attempt_exits_one(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [
        _write_observation(201, 40.0),
        _write_observation(503, 90.0),
    ]

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "1 errors" in harness.out.getvalue()
    assert harness.files() == RUN_FILES


def test_an_interrupted_probe_smoke_stops_the_probes_writes_the_files_and_exits_130(
    tmp_path: Path,
) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    harness.fixture.time.sleep = interrupt  # type: ignore[method-assign]

    assert harness.run("probe-smoke", "--authorized") == 130

    assert harness.files() == RUN_FILES
    assert harness.fixture.probes.stops == 1


def test_probe_smoke_without_attempts_says_so(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.scenario.answering = False

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "no probe attempt was recorded" in harness.out.getvalue()


def test_a_probe_that_does_not_stop_in_time_is_noted_in_the_smoke(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.probes.stuck = True

    harness.run("probe-smoke", "--authorized")

    assert "a probe did not stop in time" in harness.text("timeline.json")


def test_a_smoke_attempt_without_a_region_is_listed_as_unknown(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [
        Observation("write", "w0001", 1, START, START, False, 0, 0.0, error="URLError")
    ]

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "unknown: 1 attempts, 1 errors" in harness.out.getvalue()


def test_the_default_handlers_need_the_services_factory(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.deps.services_factory = None

    assert harness.run("run", "--authorized") == 2
    assert harness.run("probe-smoke", "--authorized") == 2

    assert harness.files() == set()


def test_the_services_are_built_only_after_the_confirmation(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    built: list[int] = []

    def factory(settings: Any, guard: Any) -> Services:
        built.append(1)
        return harness.fixture.services()

    harness.deps.services_factory = factory
    harness.answers = ["no"]

    assert harness.run("run") == 1

    assert built == []


def test_no_secret_reaches_any_output_file(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.run("run", "--authorized")

    everything = harness.all_text() + harness.out.getvalue() + harness.err.getvalue()
    assert PASSWORD not in everything
    assert ACCOUNT not in everything


def test_default_services_hold_the_guard_and_never_touch_aws_or_gh() -> None:
    settings = make_settings()
    guard = OutputGuard(settings.secret_values())
    commands: list[list[str]] = []

    def gh_runner(command: Sequence[str], timeout: float) -> tuple[int, str]:
        commands.append(list(command))
        header = "validate\tGroup 3\tstep 3/3: probe movement"
        return 0, f"{header}\nvalidate\tGroup 3\tpassword {PASSWORD}\n"

    services = default_services(
        settings, guard, clients=lambda service, region: object(), gh_runner=gh_runner
    )

    with pytest.raises(GithubError, match="refused by the output guard"):
        services.workflows.step_log(7, "Group 3")
    assert commands == [["gh", "run", "view", "7", "--repo", "example/repo", "--log"]]


def test_default_dependencies_have_the_real_services_factory() -> None:
    deps = cli.default_dependencies()

    assert deps.services_factory is cli.default_services
    assert deps.drill is None
    assert deps.probe_smoke is None


class LiveApi:
    """The API of the real probes: always answers, so the threads have something to record."""

    def post_transaction(self, token: str, kind: str, amount: Any, idempotency_key: str) -> Sample:
        fields: dict[str, Any] = {"balance_cents": 5}
        return Sample(START, START, 201, 1.0, "us-east-2", "us-east-2", "writer", fields)

    def get_account(self, token: str) -> Sample:
        return Sample(START, START, 200, 1.0, "us-east-2", None, None, {"balance_cents": 5})

    def get_health(self, deep: bool = False) -> Sample:
        return Sample(START, START, 200, 1.0, "us-east-2", None, "writer", {})


def test_the_real_probe_set_feeds_the_sink_and_stops_on_demand() -> None:
    received = threading.Event()
    seen: list[Observation] = []

    def sink(observation: Observation) -> None:
        seen.append(observation)
        if observation.probe == "write":
            received.set()

    services = Fixture().services()
    services.api = LiveApi()  # type: ignore[assignment]
    stop = threading.Event()
    timeline = Timeline("run-1", OutputGuard(), lambda: START)
    wiring = ProbeWiring(services, make_settings(), sink, stop, timeline, cli.real_clock(stop))
    rig = build_probes(wiring)

    rig.probes.start()
    assert received.wait(5)
    stopped = rig.probes.stop()

    assert stopped
    assert "write" in {observation.probe for observation in seen}
    assert len(rig.write.acked) >= 1


def test_the_lag_is_read_after_the_settle_wait_of_two_minutes(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    fixture = harness.fixture
    reads_before_wait: list[int] = []
    fixture.settle_hook = lambda seconds: reads_before_wait.append(len(fixture.observers.windows))

    harness.run("run", "--authorized")

    assert fixture.settles == [120.0]
    assert reads_before_wait == [0]
    start, end = fixture.observers.windows[0]
    assert end - start >= timedelta(seconds=120)
    assert "| lag | settle | waiting 120 s for CloudWatch | -" in harness.text("run.log")


def test_an_interrupted_run_reads_the_lag_at_once_and_notes_the_missing_minutes(
    tmp_path: Path,
) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.workflows.interrupt_on = "failover:to-secondary"

    assert harness.run("run", "--authorized") == 130

    assert harness.fixture.settles == []
    assert "may be missing" in harness.text("timeline.json")


def test_ctrl_c_during_the_settle_wait_still_writes_the_outputs(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    harness.fixture.settle_hook = interrupt

    assert harness.run("run", "--authorized") == 130

    assert harness.files() == RUN_FILES
    assert "may be missing" in harness.text("timeline.json")
    assert "settle interrupted, lag may be incomplete" in harness.out.getvalue()


def test_a_second_ctrl_c_during_the_lag_read_still_writes_the_outputs(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.observers.lag_error = KeyboardInterrupt()

    assert harness.run("run", "--authorized") == 130

    assert harness.files() == RUN_FILES
    assert "replication lag read was interrupted" in harness.text("timeline.json")


def test_a_second_ctrl_c_while_writing_the_outputs_still_writes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RunHarness(tmp_path)
    real = cli.write_run_outputs
    calls: list[int] = []

    def interrupted_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "write_run_outputs", interrupted_once)

    assert harness.run("run", "--authorized") == 130

    assert harness.files() == RUN_FILES
    assert len(calls) == 2


def test_ctrl_c_on_every_output_attempt_is_reported_and_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RunHarness(tmp_path)

    def always(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "write_run_outputs", always)

    assert harness.run("run", "--authorized") == 130

    assert "not written (interrupted)" in harness.text("run.log")


def test_a_write_failure_in_the_outputs_keeps_the_verdict_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RunHarness(tmp_path)

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "write_run_outputs", broken)

    assert harness.run("run", "--authorized") == 0

    log = harness.text("run.log")
    assert "| result | verdict | PASS | -" in log
    assert "| outputs | error | not written (OSError) | -" in log


def test_a_log_line_that_cannot_be_logged_goes_to_the_console(tmp_path: Path) -> None:
    shown: list[str] = []

    class BrokenLog(RunLog):
        def append(self, step: str, action: str, result: str, region: str = "") -> str:
            raise OSError("disk full")

    context = RunContext(
        make_settings(),
        FakePorts().ports(),
        OutputGuard(),
        BrokenLog(tmp_path / "run.log", OutputGuard(), lambda: MOMENT),
        shown.append,
    )

    cli._log_line(context, "result", "verdict", "INCONCLUSIVE")

    assert shown == ["result verdict: INCONCLUSIVE (not logged: OSError)"]


def test_a_refused_status_text_is_redacted_not_raised(tmp_path: Path) -> None:
    shown: list[str] = []
    log = RunLog(tmp_path / "run.log", OutputGuard(), lambda: MOMENT)

    line = cli.announce(log, shown.append, "fence", "us-east-2", "arn:aws:fake")

    assert line.endswith("| fence | phase | [redacted result] | us-east-2")
    assert shown == [line]
    assert "arn:aws" not in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_a_refused_status_mid_drill_does_not_become_an_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RunHarness(tmp_path)
    original = cli.announce

    def announce(log: RunLog, out: Any, phase: str, region: str, result: str, *rest: str) -> str:
        text = "dispatched arn:aws:fake" if phase == "fence" and result == "dispatched" else result
        return original(log, out, phase, region, text, *rest)

    monkeypatch.setattr(cli, "announce", announce)

    assert harness.run("run", "--authorized") == 0

    assert harness.files() == RUN_FILES
    assert "arn:aws" not in harness.all_text()


def test_probe_smoke_with_an_odd_number_of_movements_reports_the_residue(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]
    acknowledge(harness.fixture, "deposit")

    assert harness.run("probe-smoke", "--authorized") == 0

    assert "1 acknowledged, 0 abandoned, 0 pending; expected net balance effect +0.01" in (
        harness.text("report.md")
    )


def test_probe_smoke_reports_the_abandoned_and_pending_movements(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]
    acknowledge(harness.fixture, "deposit", "withdrawal")
    harness.fixture.write.abandoned.append(Abandoned("w0003", "key-3", "deposit", 3))
    harness.fixture.write.pending.append(Pending("w0004", "key-4", "withdrawal", 1))

    assert harness.run("probe-smoke", "--authorized") == 0

    shown = harness.out.getvalue()
    assert "2 acknowledged, 1 abandoned, 1 pending; expected net balance effect 0.00" in shown
    assert "(each of 2 unknown may add or remove 0.01)" in shown
    assert "expected 12 plus up to 2 unknown" in shown


def test_probe_smoke_fails_when_the_ledger_is_inconsistent(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]
    harness.fixture.api.baseline_consistent = False

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "problem: the ledger check reports an inconsistency" in harness.out.getvalue()


def test_probe_smoke_fails_when_a_movement_is_missing_from_the_ledger(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]
    acknowledge(harness.fixture, "deposit", "withdrawal")
    harness.fixture.api.final_count_delta = -1

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "1 acknowledged writes are missing from the ledger" in harness.out.getvalue()


def test_probe_smoke_without_a_baseline_skips_the_ledger_check_and_fails(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]
    harness.fixture.api.baseline_status = 500

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "ledger check skipped: the baseline /account could not be read" in (
        harness.out.getvalue()
    )


def test_probe_smoke_with_a_baseline_read_that_raises_is_not_fatal(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]

    def broken(token: str) -> Sample:
        raise OSError("down")

    harness.fixture.api.get_account = broken  # type: ignore[method-assign]

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "the baseline /account could not be read" in harness.out.getvalue()


def test_probe_smoke_with_a_failing_final_read_skips_the_ledger_check(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]

    def broken(token: str) -> Sample:
        raise RuntimeError("down")

    harness.fixture.api.get_consistency = broken  # type: ignore[method-assign]

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "ledger check skipped (RuntimeError)" in harness.out.getvalue()


def test_probe_smoke_fails_on_an_answer_from_another_region(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 20.0, "us-west-2")]

    assert harness.run("probe-smoke", "--authorized") == 1

    assert "us-west-2: 1 attempts, 0 errors" in harness.out.getvalue()
    assert "(expected only us-east-2)" in harness.out.getvalue()


def test_probe_smoke_stops_the_probes_when_start_fails(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)

    def broken() -> None:
        raise RuntimeError("thread limit")

    harness.fixture.probes.start = broken  # type: ignore[method-assign]

    assert harness.run("probe-smoke", "--authorized") == 4

    assert harness.fixture.probes.stops == 1
    assert harness.files() == RUN_FILES
    assert "the smoke stopped on an error (RuntimeError)" in harness.text("timeline.json")


def test_probe_smoke_writes_the_outputs_when_the_summary_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RunHarness(tmp_path)

    def broken(*args: object) -> None:
        raise ValueError("bad observation")

    monkeypatch.setattr(cli, "_smoke_summary", broken)

    assert harness.run("probe-smoke", "--authorized") == 4

    assert harness.files() == RUN_FILES
    assert "the summary could not be built (ValueError)" in harness.text("report.md")


def test_a_second_ctrl_c_while_stopping_the_smoke_still_writes_the_outputs(
    tmp_path: Path,
) -> None:
    harness = RunHarness(tmp_path)

    def interrupt() -> bool:
        raise KeyboardInterrupt

    harness.fixture.probes.stop = interrupt  # type: ignore[method-assign]

    assert harness.run("probe-smoke", "--authorized") == 130

    assert harness.files() == RUN_FILES
    assert "interrupted while stopping the probes" in harness.text("timeline.json")


def test_ctrl_c_during_the_smoke_ledger_check_still_writes_the_outputs(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)
    harness.fixture.smoke_observations = [_write_observation(201, 40.0)]

    def interrupt(token: str) -> Sample:
        raise KeyboardInterrupt

    harness.fixture.api.get_consistency = interrupt  # type: ignore[method-assign]

    assert harness.run("probe-smoke", "--authorized") == 130

    assert harness.files() == RUN_FILES
    assert "the smoke summary was interrupted" in harness.text("report.md")


def test_a_failing_stop_of_the_smoke_probes_is_noted(tmp_path: Path) -> None:
    harness = RunHarness(tmp_path)

    def broken() -> bool:
        raise RuntimeError("join failed")

    harness.fixture.probes.stop = broken  # type: ignore[method-assign]

    harness.run("probe-smoke", "--authorized")

    assert harness.files() == RUN_FILES
    assert "could not be stopped cleanly (RuntimeError)" in harness.text("timeline.json")
