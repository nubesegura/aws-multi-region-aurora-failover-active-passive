"""Command line of the runner: `check`, `probe-smoke`, `run` and `formal-report`."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO

from dr_drill.api import ApiClient
from dr_drill.auth import Session
from dr_drill.aws_ports import ClientFactory, build_ports
from dr_drill.cognito_http import HttpCognitoGateway
from dr_drill.config import Settings, SettingsError, load_settings
from dr_drill.dns import DnsWatcher, Resolver, socket_resolver
from dr_drill.drill import (
    Drill,
    DrillConfig,
    DrillDeps,
    DrillResult,
    LedgerApi,
    ProbeMonitor,
    ProbesPort,
    StateObserver,
    Workflows,
    WriteStats,
)
from dr_drill.formal_report import formal_report_command
from dr_drill.github import GhRunner, Github, subprocess_gh_runner
from dr_drill.ledger import (
    RPO_NOT_VERIFIED,
    LedgerReading,
    LedgerReport,
    read_account,
    read_consistency,
    verify_ledger,
)
from dr_drill.metrics import STATS_PROBES, UNKNOWN_REGION, compute_metrics, percentile
from dr_drill.observers import LagSample, build_observers
from dr_drill.phases import LAG_SETTLE_SECONDS, VERDICT_ABORTED
from dr_drill.preconditions import (
    PreconditionPorts,
    PreconditionReport,
    mask_account,
    run_preconditions,
)
from dr_drill.probe import (
    AuthProbe,
    Observation,
    Probe,
    ProbeClock,
    ReadApi,
    ReadProbe,
    SecondaryAuth,
    Sink,
    WriteApi,
    WriteProbe,
    round_ms,
)
from dr_drill.redact import OutputGuard, RefusedOutput
from dr_drill.report import render_run_report, run_folder, run_meta_from_result, write_run_outputs
from dr_drill.runlog import RunLog
from dr_drill.timeline import ProbeSet, Timeline

EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_USAGE = 2
EXIT_PRECONDITIONS = 3
EXIT_INTERNAL_ERROR = 4
EXIT_INTERRUPTED = 130

CONFIRMATION_WORD = "drill"
RUNBOOK = "docs/dr-runbook.md"
RUNNER_DIR = Path(__file__).resolve().parents[1]
RUNS_DIR = RUNNER_DIR.parents[1] / "reports" / "e2e"

PROBE_WRITE = "probe movements: deposits and withdrawals of 0.01 (net zero) through the public API"
SMOKE_SECONDS = 60.0
OUTPUT_FILES = 2
OUTPUT_ATTEMPTS = 3


def dispatch_writes(settings: Settings) -> tuple[str, ...]:
    """The three workflow dispatches of the drill, with the primary region from the settings."""
    region = settings.region_primary
    return (
        f"gh workflow run simulate-outage (action=fence region={region} confirm={region})",
        "gh workflow run failover (direction=to-secondary confirm=failover)",
        "gh workflow run failover (direction=to-primary confirm=failover)",
    )


@dataclass(frozen=True)
class RunContext:
    """What a command that writes to prod receives, after the checks and the confirmation."""

    settings: Settings
    ports: PreconditionPorts
    guard: OutputGuard
    log: RunLog
    out: Callable[[str], None]


class RunObserver(StateObserver, Protocol):
    """The read-only observers of a run: state snapshots and the replication lag."""

    def replication_lag(self, start: datetime, end: datetime) -> Sequence[LagSample]:
        """Lag datapoints of the window."""
        ...


class RunApi(LedgerApi, WriteApi, ReadApi, Protocol):
    """Everything a run asks of the API: the probes and the two ledger reads."""


class RunSession(SecondaryAuth, Protocol):
    """The sign-in of the drill user."""

    def id_token(self) -> str:
        """A valid ID token."""
        ...


@dataclass(frozen=True)
class ProbeRig:
    """The probes (started and stopped together) plus the accounting of the write probe."""

    probes: ProbesPort
    write: WriteStats


@dataclass(frozen=True)
class ProbeWiring:
    """What a probe builder needs: the clients, the settings, the sink and the stop event."""

    services: Services
    settings: Settings
    sink: Sink
    stop: threading.Event
    timeline: Timeline
    clock: ProbeClock


def real_clock(stop: threading.Event) -> ProbeClock:
    """System time; sleeping is waiting on the stop event, so a stop is prompt."""
    return ProbeClock(time.monotonic, lambda: datetime.now(UTC), stop.wait)


def build_probes(wiring: ProbeWiring) -> ProbeRig:
    """The write, read, authentication and DNS probes, started and stopped together."""
    services, settings = wiring.services, wiring.settings
    token = services.session.id_token
    write = WriteProbe(
        services.api, token, wiring.clock, wiring.sink, wiring.stop, settings.probe_rate_seconds
    )
    probes: list[Probe] = [
        write,
        ReadProbe(services.api, token, wiring.clock, wiring.sink, wiring.stop),
        AuthProbe(services.session, wiring.clock, wiring.sink, wiring.stop),
        DnsWatcher(
            settings.api_domain_name,
            services.resolver,
            wiring.clock,
            wiring.timeline,
            wiring.stop,
        ),
    ]
    return ProbeRig(ProbeSet(probes, wiring.stop, wiring.timeline), write)


@dataclass
class Services:
    """The external clients of one run (`gh`, AWS reads, the API, Cognito, DNS); tests use fakes."""

    workflows: Workflows
    observers: RunObserver
    api: RunApi
    session: RunSession
    resolver: Resolver
    clock_factory: Callable[[threading.Event], ProbeClock] = real_clock
    settle: Callable[[float], object] = time.sleep
    probes_factory: Callable[[ProbeWiring], ProbeRig] = build_probes


def default_services(
    settings: Settings,
    guard: OutputGuard,
    clients: ClientFactory | None = None,
    gh_runner: GhRunner = subprocess_gh_runner,
) -> Services:
    """Real clients. `gh` output and every token pass through `guard`, which holds the secrets."""
    observers = build_observers(settings, guard, os.environ, clients)
    session = Session(
        HttpCognitoGateway(),
        observers,
        settings.username,
        settings.password,
        (settings.region_primary, settings.region_secondary),
        time.time,
        guard.register,
    )
    return Services(
        workflows=Github(
            settings.github_repository, gh_runner, fence_region=settings.region_primary, guard=guard
        ),
        observers=observers,
        api=ApiClient(f"https://{settings.api_domain_name}", lambda: datetime.now(UTC)),
        session=session,
        resolver=socket_resolver,
    )


@dataclass
class Dependencies:
    """Collaborators of the command line; tests replace them with fakes."""

    load_settings: Callable[[], Settings]
    ports_factory: Callable[[Settings, OutputGuard], PreconditionPorts] | None = None
    drill: Callable[[RunContext], int] | None = None
    probe_smoke: Callable[[RunContext], int] | None = None
    services_factory: Callable[[Settings, OutputGuard], Services] | None = None
    formal_report: Callable[[Sequence[Path]], int] | None = None
    input_fn: Callable[[str], str] = input
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    runs_dir: Path = RUNS_DIR


def default_dependencies() -> Dependencies:
    """Real collaborators: settings from the runner folder and the process environment."""
    return Dependencies(
        load_settings=lambda: load_settings(RUNNER_DIR, os.environ),
        ports_factory=build_ports,
        services_factory=default_services,
        formal_report=formal_report_command,
    )


def build_parser() -> argparse.ArgumentParser:
    """Argument parser with the four subcommands."""
    parser = argparse.ArgumentParser(prog="dr_drill", description="DR drill runner")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="read-only preconditions, nothing is written")
    for name, help_text in (
        ("probe-smoke", "short probe in the primary region only, no workflow"),
        ("run", "the full drill (fence, failover and failback)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--authorized",
            action="store_true",
            help="the owner already authorized this run; skip the typed confirmation",
        )
    report = commands.add_parser("formal-report", help="formal EN/ES report from run folders")
    report.add_argument("--run", dest="runs", action="append", type=Path, required=True)
    return parser


def _emit(deps: Dependencies, guard: OutputGuard, text: str) -> None:
    """Every console line goes through the output guard; a refused FAIL line keeps its name."""
    try:
        guard.ensure("console", text)
    except RefusedOutput as refused:
        deps.err.write(str(refused) + "\n")
        name = text.removeprefix("FAIL ").split(":", 1)[0]
        if text.startswith("FAIL ") and guard.reason(name) is None:
            deps.out.write(f"FAIL {name}: [redacted]\n")
        return
    deps.out.write(text + "\n")


def _emit_error(deps: Dependencies, text: str) -> None:
    """An error line through the heuristic guard (no secret is known yet when settings fail)."""
    reason = OutputGuard().reason(text)
    deps.err.write(text + "\n" if reason is None else f"output refused: {reason}\n")


def announce(
    log: RunLog,
    out: Callable[[str], None],
    phase: str,
    region: str,
    result: str,
    action: str = "phase",
) -> str:
    """Write one status line to the run log and to the console; returns the line.

    A result the guard refuses is replaced by `[redacted <field>]`: a status line never aborts.
    """
    try:
        line = log.append(phase, action, result, region)
    except RefusedOutput as refused:
        line = log.append(phase, action, f"[redacted {refused.field}]", region)
    out(line)
    return line


def _report_lines(report: PreconditionReport) -> list[str]:
    return [
        f"{'PASS' if check.ok else 'FAIL'} {check.name}: {check.detail}" for check in report.checks
    ]


def _check(
    deps: Dependencies, settings: Settings, ports: PreconditionPorts, guard: OutputGuard
) -> int:
    report = run_preconditions(ports, settings)
    for line in _report_lines(report):
        _emit(deps, guard, line)
    return EXIT_OK if report.ok else EXIT_PRECONDITIONS


def _plan(settings: Settings, ports: PreconditionPorts, writes: Sequence[str]) -> list[str]:
    return [
        f"Account: {mask_account(ports.identity.caller_account())}",
        f"Regions: primary {settings.region_primary}, secondary {settings.region_secondary}, "
        f"global {settings.region_global}",
        "Writes to prod during this run:",
        *(f"  - {write}" for write in writes),
    ]


def _confirmed(deps: Dependencies, authorized: bool) -> bool:
    if authorized:
        return True
    try:
        answer = deps.input_fn(f"Type '{CONFIRMATION_WORD}' to start: ")
    except EOFError:
        return False
    return answer.strip() == CONFIRMATION_WORD


def _writing_command(
    deps: Dependencies,
    handler: Callable[[RunContext], int] | None,
    settings: Settings,
    ports: PreconditionPorts,
    guard: OutputGuard,
    writes: Sequence[str],
    authorized: bool,
) -> int:
    if handler is None:
        deps.err.write("This command is not available in this build of the runner.\n")
        return EXIT_USAGE
    code = _check(deps, settings, ports, guard)
    if code != EXIT_OK:
        deps.err.write("Preconditions failed: nothing was written to prod.\n")
        return code
    for line in _plan(settings, ports, writes):
        _emit(deps, guard, line)
    if not _confirmed(deps, authorized):
        deps.err.write("Confirmation refused: nothing was written to prod.\n")
        return EXIT_ABORTED
    log = RunLog(run_folder(deps.runs_dir, deps.clock()) / "run.log", guard, deps.clock)
    context = RunContext(settings, ports, guard, log, lambda text: _emit(deps, guard, text))
    if authorized:
        announce(log, context.out, "start", "", "authorized by the owner", "confirmation")
    return handler(context)


def _handler(deps: Dependencies, command: str) -> Callable[[RunContext], int] | None:
    """The injected handler, else the real one over the services factory (None when neither).

    An explicit handler wins over the factory; it is a seam for tests.
    """
    explicit = deps.drill if command == "run" else deps.probe_smoke
    factory = deps.services_factory
    if explicit is not None or factory is None:
        return explicit
    play = run_drill if command == "run" else run_smoke
    return lambda context: play(context, factory(context.settings, context.guard))


def tee(*sinks: Sink) -> Sink:
    """One sink that feeds all of them; every sink is called even if an earlier one fails."""

    def feed(observation: Observation) -> None:
        failure: Exception | None = None
        for sink in sinks:
            try:
                sink(observation)
            except Exception as error:
                failure = failure or error
        if failure is not None:
            raise failure

    return feed


@dataclass(frozen=True)
class _Kit:
    clock: ProbeClock
    timeline: Timeline
    monitor: ProbeMonitor
    rig: ProbeRig
    started: datetime


def _build_kit(context: RunContext, services: Services) -> _Kit:
    stop = threading.Event()
    clock = services.clock_factory(stop)
    timeline = Timeline(context.log.path.parent.name, context.guard, clock.utc_now)
    rigs: list[ProbeRig] = []
    monitor = ProbeMonitor(clock.monotonic, lambda: rigs[0].write.abandoned if rigs else ())
    sink = tee(timeline.record_observation, monitor.feed)
    rig = services.probes_factory(
        ProbeWiring(services, context.settings, sink, stop, timeline, clock)
    )
    rigs.append(rig)
    return _Kit(clock, timeline, monitor, rig, clock.utc_now())


def _record_lag(timeline: Timeline, observers: RunObserver, start: datetime, end: datetime) -> None:
    """Lag datapoints as `metric_sample` events; an unreadable or refused sample is a note."""
    try:
        samples = observers.replication_lag(start, end)
    except Exception as error:
        timeline.note(f"replication lag was not read ({type(error).__name__})")
        return
    for sample in samples:
        data = {
            "metric": sample.metric,
            "region": sample.region,
            "milliseconds": round_ms(sample.milliseconds),
        }
        try:
            timeline.add("metric_sample", data, sample.time_utc)
        except RefusedOutput:
            timeline.note("a replication lag sample was refused by the output guard")


def _log_line(context: RunContext, phase: str, action: str, result: str) -> None:
    """A status line that never raises: when the log cannot take it, the console says so."""
    try:
        announce(context.log, context.out, phase, "", result, action)
    except Exception as error:
        context.out(f"{phase} {action}: {result} (not logged: {type(error).__name__})")


def _write_outputs(context: RunContext, timeline: Timeline, report: str) -> bool:
    """Write the outputs, trying again after a Ctrl-C; True when one arrived."""
    interrupted = False
    for _ in range(OUTPUT_ATTEMPTS):
        try:
            _write_once(context, timeline, report)
        except KeyboardInterrupt:
            interrupted = True
        else:
            return interrupted
    _log_line(context, "outputs", "error", "not written (interrupted)")
    return True


def _write_once(context: RunContext, timeline: Timeline, report: str) -> None:
    """`timeline.json` and `report.md` next to `run.log`; refusals and the result go to the log."""
    try:
        outputs = write_run_outputs(context.log.path.parent, timeline, report, context.guard)
    except Exception as error:
        _log_line(context, "outputs", "error", f"not written ({type(error).__name__})")
        return
    for refusal in outputs.refused:
        _log_line(context, "outputs", "refused", refusal)
    written = f"{len(outputs.written)} of {OUTPUT_FILES} files written"
    _log_line(context, "outputs", "write", written)


def _settle_before_lag(
    context: RunContext, services: Services, kit: _Kit, result: DrillResult
) -> bool:
    """Wait for CloudWatch to publish the last lag datapoints (1 to 3 minutes of delay).

    An interrupted run, or a Ctrl-C during the wait, reads at once and notes what may be missing;
    True when a Ctrl-C arrived during the wait.
    """
    waited = False
    interrupted = False
    if result.verdict != VERDICT_ABORTED:
        _log_line(context, "lag", "settle", f"waiting {LAG_SETTLE_SECONDS:.0f} s for CloudWatch")
        try:
            services.settle(LAG_SETTLE_SECONDS)
            waited = True
        except KeyboardInterrupt:
            interrupted = True
            context.out("settle interrupted, lag may be incomplete")
    if not waited:
        kit.timeline.note("the last 1 to 3 minutes of replication lag may be missing (CloudWatch)")
    return interrupted


def _read_lag(kit: _Kit, services: Services) -> bool:
    """The lag read; True when a Ctrl-C cut it short (the timeline keeps a note)."""
    try:
        _record_lag(kit.timeline, services.observers, kit.started, kit.clock.utc_now())
    except KeyboardInterrupt:
        kit.timeline.note("the replication lag read was interrupted")
        return True
    return False


def _finalize(context: RunContext, services: Services, kit: _Kit, result: DrillResult) -> bool:
    """After any end of the drill (success, stop, interruption, error): lag, metrics, outputs.

    Nothing here may lose the verdict or the outputs: every step degrades to a note or a line.
    Returns True when a Ctrl-C arrived in any step, so the run exits 130.
    """
    settings = context.settings
    _log_line(context, "result", "verdict", result.verdict)
    interrupted = _settle_before_lag(context, services, kit, result)
    interrupted = _read_lag(kit, services) or interrupted
    try:
        metrics = compute_metrics(kit.timeline, settings.region_primary, settings.region_secondary)
        meta = run_meta_from_result(result, settings.region_primary, settings.region_secondary)
        report = render_run_report(kit.timeline, metrics, result.ledger, meta)
    except Exception as error:
        report = (
            f"# DR drill run report - {kit.timeline.run_id}\n\n"
            f"The report could not be built ({type(error).__name__}); the timeline is complete.\n"
        )
    return _write_outputs(context, kit.timeline, report) or interrupted


def run_drill(context: RunContext, services: Services) -> int:
    """The full drill; returns its exit code. The outputs are written on every path."""
    settings = context.settings
    kit = _build_kit(context, services)
    finalized: list[bool] = []
    deps = DrillDeps(
        workflows=services.workflows,
        observers=services.observers,
        api=services.api,
        token=services.session.id_token,
        probes=kit.rig.probes,
        write=kit.rig.write,
        monitor=kit.monitor,
        timeline=kit.timeline,
        clock=kit.clock,
        status=lambda phase, region, result: announce(
            context.log, context.out, phase, region, result
        ),
        out=context.out,
        finalize=lambda result: finalized.append(_finalize(context, services, kit, result)),
    )
    config = DrillConfig(
        settings.region_primary,
        settings.region_secondary,
        steady_seconds=float(settings.phase_steady_seconds),
    )
    code = Drill(config, deps).run().exit_code
    return EXIT_INTERRUPTED if any(finalized) and code != EXIT_INTERNAL_ERROR else code


def _smoke_summary(timeline: Timeline, primary: str) -> tuple[list[str], bool]:
    """One line per region (attempts, errors, median and maximum latency) and whether any failed.

    The smoke runs in the primary region only: an answer from another region is a failure.
    """
    by_region: dict[str, list[Observation]] = {}
    for observation in timeline.observations():
        if observation.probe in STATS_PROBES:
            by_region.setdefault(observation.served_by_region or UNKNOWN_REGION, []).append(
                observation
            )
    if not by_region:
        return ["no probe attempt was recorded"], True
    lines = []
    failed = False
    for region, items in sorted(by_region.items()):
        errors = sum(1 for item in items if not item.ok)
        latencies = [item.latency_ms for item in items]
        misplaced = region not in (primary, UNKNOWN_REGION)
        failed = failed or errors > 0 or misplaced
        lines.append(
            f"{region}: {len(items)} attempts, {errors} errors, "
            f"median {percentile(latencies, 50) or 0.0:.1f} ms, max {max(latencies):.1f} ms"
            + (f" (expected only {primary})" if misplaced else "")
        )
    return lines, failed


def _read_baseline(services: Services) -> LedgerReading | None:
    """`GET /account` before the probes start; None when it cannot be read."""
    try:
        return read_account(services.api.get_account(services.session.id_token()))
    except Exception:
        return None


def _net_effect(acked: int) -> str:
    return "+0.01" if acked % 2 else "0.00"


def _ledger_lines(report: LedgerReport) -> list[str]:
    consistent = None if report.consistency is None else report.consistency.consistent
    lines = [
        f"ledger: {report.stored_count} movements stored, expected {report.acked_expected} "
        f"plus up to {report.band} unknown; consistent: {consistent}",
        *(f"problem: {problem}" for problem in report.problems),
        *(f"note: {note}" for note in report.notes),
    ]
    return lines


def _smoke_ledger(
    services: Services, kit: _Kit, baseline: LedgerReading | None
) -> tuple[list[str], bool]:
    """Movement accounting, the expected net effect and the ledger check after the smoke."""
    write = kit.rig.write
    acked, abandoned, pending = len(write.acked), len(write.abandoned), len(write.pending)
    unknown = abandoned + pending
    lines = [
        f"movements: {acked} acknowledged, {abandoned} abandoned, {pending} pending; "
        f"expected net balance effect {_net_effect(acked)}"
        + (f" (each of {unknown} unknown may add or remove 0.01)" if unknown else "")
    ]
    if baseline is None:
        return [*lines, "ledger check skipped: the baseline /account could not be read"], True
    try:
        token = services.session.id_token()
        final = read_account(services.api.get_account(token))
        consistency = read_consistency(services.api.get_consistency(token))
    except Exception as error:
        return [*lines, f"ledger check skipped ({type(error).__name__})"], True
    report = verify_ledger(
        baseline, final, consistency, write.acked, write.abandoned, write.pending
    )
    unverified = report.rpo_status == RPO_NOT_VERIFIED
    return [*lines, *_ledger_lines(report)], bool(report.problems) or unverified


def _stop_smoke_probes(kit: _Kit) -> bool:
    """Stop the probes; True when a Ctrl-C arrived while stopping (they still end: event set)."""
    try:
        if not kit.rig.probes.stop():
            kit.timeline.note("a probe did not stop in time")
    except KeyboardInterrupt:
        kit.timeline.note("interrupted while stopping the probes")
        return True
    except Exception as error:
        kit.timeline.note(f"the probes could not be stopped cleanly ({type(error).__name__})")
    return False


def run_smoke(context: RunContext, services: Services) -> int:
    """The probes alone for `SMOKE_SECONDS`, no workflow; the outputs are written on every path.

    The probes alternate a deposit and a withdrawal of 0.01 and stop by time, so an odd number of
    acknowledged movements leaves the balance 0.01 higher; the summary says so and checks the
    ledger (`/account`, `/consistency`) against the movements.
    """
    kit = _build_kit(context, services)
    _log_line(context, "probe-smoke", "phase", "started")
    baseline = _read_baseline(services)
    interrupted = False
    failure: str | None = None
    try:
        kit.rig.probes.start()
        kit.clock.sleep(SMOKE_SECONDS)
    except KeyboardInterrupt:
        interrupted = True
    except Exception as error:
        failure = type(error).__name__
        kit.timeline.note(f"the smoke stopped on an error ({failure})")
    interrupted = _stop_smoke_probes(kit) or interrupted
    try:
        lines, failed = _smoke_summary(kit.timeline, context.settings.region_primary)
        extra, ledger_failed = _smoke_ledger(services, kit, baseline)
        lines, failed = [*lines, *extra], failed or ledger_failed
    except KeyboardInterrupt:
        interrupted = True
        lines, failed = ["the smoke summary was interrupted"], True
    except Exception as error:
        failure = failure or type(error).__name__
        lines, failed = [f"the summary could not be built ({type(error).__name__})"], True
    for line in lines:
        context.out(line)
    heading = f"# Probe smoke report - {kit.timeline.run_id}"
    report = "\n".join([heading, "", *[f"- {line}" for line in lines]])
    interrupted = _write_outputs(context, kit.timeline, report) or interrupted
    if interrupted:
        return EXIT_INTERRUPTED
    if failure is not None:
        return EXIT_INTERNAL_ERROR
    return EXIT_ABORTED if failed else EXIT_OK


def _dispatch(args: argparse.Namespace, deps: Dependencies) -> int:
    if args.command == "formal-report":
        if deps.formal_report is None:
            deps.err.write("formal-report is not available in this build of the runner.\n")
            return EXIT_USAGE
        return deps.formal_report(args.runs)
    settings = deps.load_settings()
    if deps.ports_factory is None:
        deps.err.write("The live adapters are not available in this build of the runner.\n")
        return EXIT_USAGE
    guard = OutputGuard(settings.secret_values())
    ports = deps.ports_factory(settings, guard)
    if args.command == "check":
        return _check(deps, settings, ports, guard)
    if args.command == "probe-smoke":
        return _writing_command(
            deps,
            _handler(deps, "probe-smoke"),
            settings,
            ports,
            guard,
            [PROBE_WRITE],
            args.authorized,
        )
    return _writing_command(
        deps,
        _handler(deps, "run"),
        settings,
        ports,
        guard,
        [PROBE_WRITE, *dispatch_writes(settings)],
        args.authorized,
    )


def main(argv: Sequence[str] | None = None, deps: Dependencies | None = None) -> int:
    """Run the command line; returns the exit code (0, 1, 2, 3, 4 or 130).

    4 is an unexpected internal error: only the exception class name is printed, because a
    traceback would bypass the output guard. 1 stays reserved for stop conditions.
    """
    deps = deps or default_dependencies()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)
    try:
        return _dispatch(args, deps)
    except SettingsError as error:
        _emit_error(deps, f"Settings error: {error}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        deps.err.write(f"Interrupted. Check the state with `check` and follow {RUNBOOK}.\n")
        return EXIT_INTERRUPTED
    except Exception as error:
        deps.err.write(f"Unexpected error ({type(error).__name__}); nothing more was done.\n")
        return EXIT_INTERNAL_ERROR
