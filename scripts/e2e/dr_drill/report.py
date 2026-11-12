"""Run report: Markdown from the timeline, the metrics and the ledger, and the run outputs on disk.

`render_run_report` is pure. `write_run_outputs` writes `run.log`, `timeline.json` and `report.md`
into the run folder (`reports/e2e/<UTC date-time>/`), also after an abort; every
text goes through the output guard first and a refused text is not written.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dr_drill.drill import DrillResult
from dr_drill.ledger import (
    RPO_LOSS,
    RPO_NOT_VERIFIABLE,
    RPO_ZERO_LOSS,
    LedgerReport,
)
from dr_drill.metrics import (
    CATEGORIES,
    DirectionMetrics,
    LagStats,
    Measure,
    PhaseStat,
    RunMetrics,
    WorkflowTotal,
)
from dr_drill.phases import (
    VERDICT_ABORTED,
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
    VERDICT_PASS,
)
from dr_drill.probe import format_utc
from dr_drill.redact import OutputGuard, RefusedOutput
from dr_drill.timeline import Timeline

RUN_LOG = "run.log"
TIMELINE_FILE = "timeline.json"
REPORT_FILE = "report.md"
FOLDER_FORMAT = "%Y-%m-%dT%H-%M-%SZ"

OUTCOME_EFFECT = {
    "deposited": "posted: +1 certain in the expected movement count",
    "skipped": "not posted: adds nothing",
    "rejected": "HTTP error, counted as not posted: adds nothing (warning)",
    "unknown": "log missing, cut or not recognised: +1 uncertain, widens the band",
}

WORKFLOW_RESOLUTION = "1 s (GitHub timestamps), workstation clock skew not corrected"
WRITE_SOURCE = "write probe, one attempt per second (timeline `request` events)"
READ_SOURCE = "read probe, GET /account every 5 s (timeline `request` events)"
WRITE_RESOLUTION = "about 1 s (write probe cadence)"

NOT_MEASURED = (
    "RPO of an unplanned failover: the drill performs planned switchovers only.",
    "Detection time: how long a real incident takes to be noticed; the outage is simulated "
    "and starts when the fence run is dispatched.",
    "DNS as seen by other resolvers, browsers and caches: only one resolver is polled, every 2 s.",
    "Client-side behaviour beyond the probe: browsers, retries of real users and TTL caches.",
    "Behaviour under production load: the probe offers about one write per second.",
)

LIMITATIONS = (
    "One run from one workstation and one network path: the numbers are a sample, not a "
    "distribution.",
    "Probe cadence bounds the resolution: writes about 1 s, reads and health 5 s, sign-in 10 s, "
    "DNS 2 s, replication lag 60 s.",
    "GitHub timestamps have a 1 s resolution and come from another clock than the workstation's.",
    "The RPO is verified from the ledger of net-zero probe movements: movements that were not "
    "acknowledged may or may not have committed (uncertainty band), and a band greater than 0 can "
    "hide a loss.",
    "The failover probe deposit is classified from the step log; when the log cannot be read it "
    "stays uncertain.",
    "Replication lag is a CloudWatch maximum per 60 s period: it bounds the lag, it does not "
    "measure the RPO of this planned switchover.",
)


@dataclass(frozen=True)
class RunMeta:
    """What the report says about the run besides the timeline: verdict, regions, warnings."""

    verdict: str
    reason: str | None
    detail: str | None
    primary: str
    secondary: str
    warnings: tuple[str, ...] = ()


def run_meta_from_result(result: DrillResult, primary: str, secondary: str) -> RunMeta:
    """The report metadata of a finished (or aborted) drill."""
    return RunMeta(
        result.verdict, result.reason, result.detail, primary, secondary, result.warnings
    )


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "/")


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return [*lines, ""]


def _value(measure: Measure, unit: str = "s", digits: int = 2) -> str:
    if measure.value is None:
        return f"not measured: {measure.reason}"
    return f"{measure.value:.{digits}f} {unit}".strip()


def _share(measure: Measure) -> str:
    if measure.value is None:
        return f"not measured: {measure.reason}"
    return f"{measure.value * 100:.1f} %"


def _count(measure: Measure) -> str:
    if measure.value is None:
        return f"not measured: {measure.reason}"
    return f"{int(measure.value)}"


def _money(cents: int | None) -> str:
    if cents is None:
        return "not read"
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _stamp(moment: datetime | None) -> str:
    return "-" if moment is None else format_utc(moment)


def _verdict_lines(meta: RunMeta) -> list[str]:
    verdict = meta.verdict
    if verdict == VERDICT_PASS:
        text = "PASS"
    elif verdict == VERDICT_FAIL:
        text = f"FAIL - stop reason: {meta.reason or 'unknown'}"
    elif verdict == VERDICT_INCONCLUSIVE:
        text = f"INCONCLUSIVE - {meta.reason or 'the RPO could not be proven'}"
    elif verdict == VERDICT_ABORTED:
        text = "ABORTED - interrupted before the drill finished"
    else:
        text = verdict
    lines = [f"**Verdict: {text}**", ""]
    if meta.detail:
        lines += [f"Detail: {meta.detail}", ""]
    if verdict in (VERDICT_ABORTED, VERDICT_FAIL):
        lines += [
            "A dispatched workflow run may still be running: check it in GitHub before anything "
            "else. Do not retry and do not unfence by hand; follow `docs/dr-runbook.md`.",
            "",
        ]
    if meta.warnings:
        lines += ["Warnings:", "", *[f"- {_cell(warning)}" for warning in meta.warnings], ""]
    return lines


def _environment_lines(timeline: Timeline, meta: RunMeta) -> list[str]:
    events = timeline.events()
    counts: dict[str, int] = {}
    for observation in timeline.observations():
        counts[observation.probe] = counts.get(observation.probe, 0) + 1
    probes = ", ".join(f"{name} {counts[name]}" for name in sorted(counts)) or "none"
    return [
        "## Environment",
        "",
        f"- Run: `{timeline.run_id}`",
        f"- Primary region: {meta.primary}; secondary region: {meta.secondary}",
        f"- First event: {_stamp(events[0].time_utc if events else None)}; "
        f"last event: {_stamp(events[-1].time_utc if events else None)}",
        f"- Probe attempts recorded: {probes}",
        "",
    ]


def _phase_lines(metrics: RunMetrics) -> list[str]:
    rows = []
    for window in metrics.phases.values():
        duration = (
            "-" if window.end is None else f"{(window.end - window.start).total_seconds():.1f} s"
        )
        rows.append((window.name, _stamp(window.start), _stamp(window.end), duration))
    if not rows:
        return ["## Phases", "", "No phase started.", ""]
    return ["## Phases", "", *_table(("Phase", "Start (UTC)", "End (UTC)", "Duration"), rows)]


def _direction_rows(direction: DirectionMetrics) -> list[tuple[str, str, str, str, str]]:
    name, old, new = direction.name, direction.old_region, direction.new_region
    auth = direction.auth
    return [
        (
            f"{name}: write RTO",
            _value(direction.write_rto),
            f"Last 2xx write answered by {old} before the first failed or retried write, to the "
            f"first 2xx write answered by {new}.",
            WRITE_SOURCE,
            WRITE_RESOLUTION,
        ),
        (
            f"{name}: read RTO",
            _value(direction.read_rto),
            f"Same definition with GET /account: last 2xx answered by {old} before the first "
            f"failed read, to the first 2xx answered by {new}.",
            READ_SOURCE,
            "about 5 s (read probe cadence)",
        ),
        (
            f"{name}: write unavailability",
            _value(direction.write_unavailability),
            f"First failed write to the first 2xx write answered by {new}.",
            WRITE_SOURCE,
            WRITE_RESOLUTION,
        ),
        (
            f"{name}: secondary sign-in availability",
            _share(auth.share),
            "Share of successful secondary-region sign-ins from the start of the outage to the "
            f"end of the steady phase ({auth.attempts} attempts, {auth.failures} failed).",
            "auth probe, one sign-in every 10 s",
            "10 s (auth probe cadence)",
        ),
        (
            f"{name}: longest sign-in failure run",
            f"{_count(auth.longest_failure_run)} sign-ins, {_value(auth.longest_failure_seconds)}",
            "Longest run of consecutive failed sign-ins and its duration.",
            "auth probe, one sign-in every 10 s",
            "10 s (auth probe cadence)",
        ),
        (
            f"{name}: DNS convergence",
            _value(direction.dns_convergence),
            "End of the traffic-switch step to the first changed DNS answer (negative when the "
            "answer changed before the step ended).",
            "DNS watcher (`dns` events) and the step times from GitHub",
            "2 s (DNS poll), one resolver",
        ),
        (
            f"{name}: decision time",
            _value(direction.decision_time),
            "End of the fence run to the dispatch of the failover.",
            "GitHub run end time and the dispatch time",
            WORKFLOW_RESOLUTION,
        ),
    ]


def _workflow_rows(total: WorkflowTotal) -> list[tuple[str, str, str, str, str]]:
    label = f"{total.workflow} ({total.phase})"
    source = "GitHub run times and the dispatch time"
    return [
        (
            f"{label}: queue time",
            _value(total.queue),
            "Dispatch to the start of the run.",
            source,
            WORKFLOW_RESOLUTION,
        ),
        (
            f"{label}: run time",
            _value(total.run),
            "Start of the run to its last update.",
            source,
            WORKFLOW_RESOLUTION,
        ),
        (
            f"{label}: total",
            _value(total.total),
            "Dispatch to the end of the run.",
            source,
            WORKFLOW_RESOLUTION,
        ),
    ]


def _lag_rows(lag: LagStats) -> list[tuple[str, str, str, str, str]]:
    source = f"CloudWatch {lag.metric}, maximum per period"
    resolution = f"{lag.period_seconds} s period (milliseconds)"
    return [
        (
            f"{lag.metric}: {name}",
            _value(measure, "ms", 1),
            f"{name} of the {lag.samples} samples taken during the run.",
            source,
            resolution,
        )
        for name, measure in (
            ("maximum", lag.max_ms),
            ("95th percentile", lag.p95_ms),
            ("mean", lag.mean_ms),
        )
    ]


def _metrics_lines(metrics: RunMetrics) -> list[str]:
    rows: list[tuple[str, str, str, str, str]] = []
    for direction in metrics.directions:
        rows += _direction_rows(direction)
    for total in metrics.workflow_totals:
        rows += _workflow_rows(total)
    for lag in metrics.lag.values():
        rows += _lag_rows(lag)
    headers = ("Metric", "Value", "Definition", "Source", "Resolution")
    return [
        "## Metrics",
        "",
        "Percentiles are nearest-rank. A metric without data is reported as not measured, "
        "never as zero.",
        "",
        *_table(headers, rows),
    ]


def _breakdown_lines(metrics: RunMetrics) -> list[str]:
    lines = ["## Breakdown per direction", ""]
    for direction in metrics.directions:
        breakdown = direction.breakdown
        lines += [f"### {direction.name}", ""]
        rows = [(name, _value(breakdown.categories[name])) for name in CATEGORIES]
        lines += _table(("Category", "Duration"), rows)
        if breakdown.steps:
            steps = [
                (step.phase, step.name, step.category, f"{step.seconds:.1f} s")
                for step in breakdown.steps
            ]
            lines += _table(("Phase", "Workflow step", "Category", "Duration"), steps)
        if breakdown.other_steps:
            lines += [
                "Steps not recognised (counted under `other`): " + "; ".join(breakdown.other_steps),
                "",
            ]
    return lines


def _rpo_statement(ledger: LedgerReport) -> str:
    if ledger.rpo_status == RPO_ZERO_LOSS:
        return "0 acknowledged writes lost (range 0 to 0)."
    if ledger.rpo_status == RPO_LOSS:
        return (
            f"Definite loss: between {ledger.rpo_lost_min} and {ledger.rpo_lost_max} "
            "acknowledged writes are missing from the ledger."
        )
    if ledger.rpo_status == RPO_NOT_VERIFIABLE:
        return (
            f"Not verifiable: between {ledger.rpo_lost_min} and {ledger.rpo_lost_max} "
            "acknowledged writes may be missing, the uncertainty band hides it."
        )
    return "Not verified: the movement count could not be checked."


def _deposit_outcomes(timeline: Timeline) -> list[tuple[str, str]]:
    return [
        (str(event.data.get("phase")), str(event.data.get("outcome")))
        for event in timeline.events("workflow_step")
        if event.data.get("event") == "probe_deposit"
    ]


def _rpo_lines(timeline: Timeline, metrics: RunMetrics, ledger: LedgerReport | None) -> list[str]:
    lines = ["## Measured RPO and ledger", ""]
    if ledger is None:
        lines += ["The ledger was not checked: the run ended before the consistency phase.", ""]
    else:
        lines += [f"Measured RPO: {_rpo_statement(ledger)}", ""]
        rows = [
            ("Baseline movements", ledger.baseline_count),
            (
                "Expected movements (baseline + acknowledged + proven deposits)",
                ledger.acked_expected,
            ),
            (
                "Stored movements",
                "not read" if ledger.stored_count is None else ledger.stored_count,
            ),
            ("Uncertainty band", ledger.band),
            ("Unexpected extra movements", ledger.unexpected_extra),
            (
                "Expected balance range",
                f"{_money(ledger.balance_expected_min)} to {_money(ledger.balance_expected_max)}",
            ),
            ("Stored balance", _money(ledger.stored_balance_cents)),
            ("Balance within the range", ledger.balance_ok),
        ]
        lines += _table(("Ledger", "Value"), rows)
        lines += _consistency_lines(ledger)
        lines += [f"- Problem: {_cell(problem)}" for problem in ledger.problems]
        lines += [f"- Note: {_cell(note)}" for note in ledger.notes]
        if ledger.problems or ledger.notes:
            lines.append("")
    lag = metrics.lag.get("AuroraGlobalDBReplicationLag")
    if lag is not None:
        lines += [
            "Replication lag bound: "
            f"maximum {_value(lag.max_ms, 'ms', 1)}, "
            f"95th percentile {_value(lag.p95_ms, 'ms', 1)}, "
            f"mean {_value(lag.mean_ms, 'ms', 1)} over {lag.samples} samples "
            f"({lag.period_seconds} s period; samples of both regions together).",
            "",
            lag.bound,
            "",
        ]
    return [*lines, *_band_lines(timeline, ledger)]


def _consistency_lines(ledger: LedgerReport) -> list[str]:
    consistency = ledger.consistency
    if consistency is None:
        return ["Consistency: not read.", ""]
    rows: list[tuple[str, object]] = [
        ("Consistent", consistency.consistent),
        ("Truncated", consistency.truncated),
        ("Replayed balance", _money(consistency.replayed_balance_cents)),
        ("Stored balance", _money(consistency.stored_balance_cents)),
        ("Duplicate keys", consistency.duplicate_keys),
        ("Balance chain intact", consistency.chain_ok),
        ("Balances match", consistency.balances_match),
    ]
    rows += [
        (f"Movements processed in {region}", count)
        for region, count in sorted(consistency.counts_by_region.items())
    ]
    return ["### Consistency", "", *_table(("Check", "Value"), rows)]


def _band_lines(timeline: Timeline, ledger: LedgerReport | None) -> list[str]:
    outcomes = _deposit_outcomes(timeline)
    lines = ["### Band and failover probe deposits", ""]
    if outcomes:
        rows = [
            (phase, outcome, OUTCOME_EFFECT.get(outcome, "unrecognised outcome"))
            for phase, outcome in outcomes
        ]
        lines += _table(("Failover", "Deposit outcome", "Effect on the ledger"), rows)
    else:
        lines += ["No failover probe deposit was classified (no failover finished).", ""]
    if ledger is not None:
        uncertain = sum(1 for _, outcome in outcomes if outcome == "unknown")
        lines += [
            f"Uncertainty band: {ledger.band} = {ledger.band - uncertain} abandoned or pending "
            f"probe movements + {uncertain} uncertain failover deposits. The stored count may "
            f"legitimately be between the expected count and the expected count plus "
            f"{ledger.band}.",
            "",
        ]
    return lines


def _stat_row(stat: PhaseStat) -> tuple[object, ...]:
    return (
        stat.probe,
        stat.phase,
        stat.region,
        stat.attempts,
        stat.errors,
        f"{stat.error_rate * 100:.1f} %",
        stat.retries,
        _value(stat.latency_median_ms, "ms", 1),
        _value(stat.latency_p95_ms, "ms", 1),
        _value(stat.latency_max_ms, "ms", 1),
    )


def _stats_lines(metrics: RunMetrics) -> list[str]:
    headers = (
        "Probe",
        "Phase",
        "Region",
        "Attempts",
        "Errors",
        "Error rate",
        "Retries",
        "Latency median",
        "Latency p95",
        "Latency max",
    )
    lines = [
        "## Errors, retries and latency",
        "",
        "A failed attempt has no answering region and is listed under `unknown`. Latency is "
        "computed over successful attempts only.",
        "",
    ]
    if not metrics.phase_stats:
        return [*lines, "No probe attempt was recorded.", ""]
    return [*lines, *_table(headers, [_stat_row(stat) for stat in metrics.phase_stats])]


def _unmeasured_lines(metrics: RunMetrics) -> list[str]:
    lines = ["## What was not measured", "", *[f"- {item}" for item in NOT_MEASURED]]
    gaps = []
    for direction in metrics.directions:
        for label, measure in (
            ("write RTO", direction.write_rto),
            ("read RTO", direction.read_rto),
            ("write unavailability", direction.write_unavailability),
            ("DNS convergence", direction.dns_convergence),
        ):
            if measure.value is None:
                gaps.append(f"{direction.name} {label}: {measure.reason}")
    lines += [f"- In this run, {gap}" for gap in gaps]
    return [*lines, ""]


def render_run_report(
    timeline: Timeline, metrics: RunMetrics, ledger: LedgerReport | None, run_meta: RunMeta
) -> str:
    """Markdown report of a run; pure, deterministic for the same inputs."""
    lines = [f"# DR drill run report - {timeline.run_id}", ""]
    lines += _verdict_lines(run_meta)
    lines += _environment_lines(timeline, run_meta)
    lines += _phase_lines(metrics)
    lines += _metrics_lines(metrics)
    lines += _breakdown_lines(metrics)
    lines += _rpo_lines(timeline, metrics, ledger)
    lines += _stats_lines(metrics)
    lines += _unmeasured_lines(metrics)
    lines += ["## Limitations", "", *[f"- {item}" for item in LIMITATIONS], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def run_folder(base: Path, now: datetime) -> Path:
    """Folder of a run: `<base>/<UTC date-time>`, e.g. `2026-11-11T12-00-00Z`."""
    return base / now.astimezone(UTC).strftime(FOLDER_FORMAT)


@dataclass(frozen=True)
class RunOutputs:
    """What `write_run_outputs` did: files written and the refusals (field and reason, no value)."""

    folder: Path
    written: tuple[Path, ...]
    refused: tuple[str, ...]


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def write_run_outputs(
    folder: Path,
    timeline: Timeline,
    report: str,
    guard: OutputGuard,
    run_log: str = "",
) -> RunOutputs:
    """Write `run.log`, `timeline.json` and `report.md`; each text passes the guard first.

    A refused text is not written and is listed in `refused`; the others are still written, so an
    aborted run leaves what exists. `run_log` empty keeps a `run.log` already appended by `RunLog`
    in the folder.
    """
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    refused: list[str] = []
    texts: list[tuple[str, str | None]] = [(RUN_LOG, run_log or None), (REPORT_FILE, report)]
    try:
        texts.append((TIMELINE_FILE, timeline.to_json()))
    except RefusedOutput as error:
        refused.append(str(error))
    for name, text in texts:
        if text is None:
            continue
        reason = guard.reason(text)
        if reason is not None:
            refused.append(f"{name} was not written because {reason}")
            continue
        _write(folder / name, text if text.endswith("\n") else text + "\n")
        written.append(folder / name)
    return RunOutputs(folder, tuple(written), tuple(refused))
