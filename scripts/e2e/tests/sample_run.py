"""Synthetic drill used by the report golden test (no real data).

Run `python -m tests.sample_run` from `scripts/e2e` to regenerate `tests/fixtures/`.
"""

from __future__ import annotations

from pathlib import Path

from dr_drill.formal_report import build_documents, build_run
from dr_drill.ledger import (
    ConsistencyReading,
    LedgerReading,
    LedgerReport,
    verify_ledger,
)
from dr_drill.metrics import compute_metrics
from dr_drill.phases import VERDICT_PASS
from dr_drill.probe import Acknowledged
from dr_drill.report import RunMeta, render_run_report

from .timeline_builder import PRIMARY, SECONDARY, TimelineBuilder, at

FIXTURES = Path(__file__).parent / "fixtures"
TIMELINE_FIXTURE = FIXTURES / "sample_timeline.json"
REPORT_FIXTURE = FIXTURES / "sample_report.md"
FORMAL_FIXTURES = {
    (runs, lang): FIXTURES / f"formal_{runs}_{lang}.md" for runs in (1, 2) for lang in ("en", "es")
}
TECHNICAL_SAMPLE = (
    "| Layer | Choice |\n"
    "|---|---|\n"
    "| Database | Aurora PostgreSQL Global Database, provisioned, `db.x1.sample`, engine "
    "`9.9` by default, one instance per cluster |\n"
)
SECOND_RUN_ID = "2026-11-12T12-00-00Z"
END = 181
BASELINE = LedgerReading(movement_count=10, balance_cents=10_000)
FENCE_WF = "simulate-outage"
FAILOVER = "failover"
VALIDATE = "Group 3 - validate the service on the custom domain"
TRAFFIC = "Group 3 - switch the Route 53 weights"

TO_SECONDARY_STEPS = (
    ("Validate the branch, the inputs, the variables and the secrets", 47, 48),
    ("Check out the code", 48, 49),
    ("Prepare the step timer and the helper functions", 49, 50),
    ("AWS login (group 1)", 50, 52),
    ("Verify the AWS account (group 1)", 52, 53),
    ("Record the writer region before", 53, 54),
    ("Group 1 - pre-checks (read only)", 54, 58),
    ("Group 1 - confirm that the primary region is fenced", 58, 60),
    ("Group 1 - confirm that the secondary region is NOT fenced", 60, 62),
    ("Group 1 - unfence the primary region", 62, 65),
    ("AWS login (group 2)", 65, 67),
    ("Verify the AWS account (group 2)", 67, 68),
    ("Group 2 - activate the Cognito replica in the secondary region", 68, 73),
    ("Group 2 - planned switchover to the secondary region", 73, 85),
    ("AWS login (group 3)", 85, 87),
    ("Verify the AWS account (group 3)", 87, 88),
    (TRAFFIC, 88, 91),
    (VALIDATE, 91, 97),
    ("Group 3 - consistency check of the ledger", 97, 100),
    ("Summary", 100, 101),
)
TO_SECONDARY_SKIPPED = (
    "Group 2 - planned switchover back to the primary region",
    "Group 2 - deactivate the Cognito replica in the secondary region",
)
TO_PRIMARY_STEPS = (
    ("Validate the branch, the inputs, the variables and the secrets", 127, 128),
    ("Check out the code", 128, 129),
    ("Prepare the step timer and the helper functions", 129, 130),
    ("AWS login (group 1)", 130, 132),
    ("Verify the AWS account (group 1)", 132, 133),
    ("Record the writer region before", 133, 134),
    ("Group 1 - pre-checks (read only)", 134, 138),
    ("AWS login (group 2)", 138, 140),
    ("Verify the AWS account (group 2)", 140, 141),
    ("Group 2 - planned switchover back to the primary region", 141, 148),
    ("Group 2 - deactivate the Cognito replica in the secondary region", 148, 150),
    ("AWS login (group 3)", 150, 151),
    ("Verify the AWS account (group 3)", 151, 152),
    (TRAFFIC, 152, 154),
    (VALIDATE, 154, 158),
    ("Group 3 - consistency check of the ledger", 158, 159),
    ("Upload diagnostic bundle", 159, 160),
)


def _region_at(second: int) -> str | None:
    """Region answering at a second of the run; None while the writer is unavailable."""
    if second < 34:
        return PRIMARY
    if second < 92:
        return None
    if second < 146:
        return SECONDARY
    if second < 154:
        return None
    return PRIMARY


def _add_probes(builder: TimelineBuilder) -> tuple[list[Acknowledged], int]:
    acked: list[Acknowledged] = []
    movement, attempt = 1, 1
    for second in range(0, END, 2):
        region = _region_at(second)
        latency = 35.0 + (second % 5) * 5
        label = f"w{movement:04d}"
        builder.request(
            "write", second, ok=region is not None, region=region, attempt=attempt,
            latency_ms=latency, label=label,
        )  # fmt: skip
        if region is None:
            attempt += 1
            continue
        kind = "deposit" if movement % 2 == 1 else "withdrawal"
        acked.append(Acknowledged(label, f"key-{label}", kind, None, 201))
        movement, attempt = movement + 1, 1
    for second in range(5, END, 10):
        region = _region_at(second)
        builder.request(
            "account", second, ok=region is not None, region=region, label=f"a{second:04d}"
        )
        probe_region = _region_at(second + 2)
        builder.request(
            "health",
            second + 2,
            ok=probe_region is not None,
            region=probe_region,
            label=f"h{second:04d}",
        )
    for second in range(0, END, 20):
        builder.request(
            "auth", second, ok=second not in (60, 80), region=SECONDARY, label=f"u{second:04d}"
        )
    return acked, movement


def _add_workflows(builder: TimelineBuilder) -> None:
    builder.dispatch("fence", FENCE_WF, 20.5)
    builder.run("fence", FENCE_WF, 21, 24, 40)
    for name, start, end in (
        ("Validate the branch, the inputs, the variables and the secrets", 24, 25),
        ("Check out the code", 25, 26),
        ("AWS login", 26, 28),
        ("Run 10-simulate-outage.sh", 30, 38),
        ("Summary", 38, 39),
    ):
        builder.step("fence", FENCE_WF, name, start, end)
    builder.dispatch("to-secondary", FAILOVER, 43)
    builder.run("to-secondary", FAILOVER, 44, 47, 101)
    for name, start, end in TO_SECONDARY_STEPS:
        builder.step("to-secondary", FAILOVER, name, start, end)
    for name in TO_SECONDARY_SKIPPED:
        builder.step("to-secondary", FAILOVER, name, None, None, "skipped")
    builder.dispatch("to-primary", FAILOVER, 123)
    builder.run("to-primary", FAILOVER, 124, 127, 160)
    for name, start, end in TO_PRIMARY_STEPS:
        builder.step("to-primary", FAILOVER, name, start, end)


def _add_phases(builder: TimelineBuilder) -> None:
    for name, start, end in (
        ("baseline", 0, 20),
        ("fence", 20, 42),
        ("to-secondary", 42, 102),
        ("steady-secondary", 102, 122),
        ("to-primary", 122, 161),
        ("steady-primary", 161, 181),
        ("consistency", 181, 182),
    ):
        builder.phase(name, start, end)


def _ledger(acked: list[Acknowledged]) -> LedgerReport:
    deposits = sum(1 for item in acked if item.kind == "deposit")
    delta = deposits - (len(acked) - deposits)
    stored = BASELINE.movement_count + len(acked) + 1
    final = LedgerReading(stored, BASELINE.balance_cents + delta + 1)
    consistency = ConsistencyReading(
        consistent=True,
        truncated=False,
        replayed_balance_cents=final.balance_cents,
        stored_balance_cents=final.balance_cents,
        duplicate_keys=0,
        chain_ok=True,
        balances_match=True,
        counts_by_region={PRIMARY: stored - 20, SECONDARY: 20},
    )
    return verify_ledger(BASELINE, final, consistency, acked, [], [], failover_deposits=1)


def build_sample(
    run_id: str = "2026-11-11T12-00-00Z", lag_factor: float = 1.0
) -> tuple[TimelineBuilder, LedgerReport, RunMeta]:
    """The synthetic timeline, its ledger report and the run metadata."""
    builder = TimelineBuilder(run_id)
    _add_phases(builder)
    acked, _ = _add_probes(builder)
    _add_workflows(builder)
    builder.dns(0, ["192.0.2.10"], first=True)
    builder.dns(92, ["198.51.100.7"])
    builder.dns(155, ["192.0.2.10"])
    for second, milliseconds in ((0, 850.0), (60, 1100.0), (120, 920.0), (180, 1300.0)):
        builder.lag(second, milliseconds * lag_factor)
    for phase, outcome, moment in (
        ("to-secondary", "deposited", 100.2),
        ("to-primary", "skipped", 160.2),
    ):
        data = {"phase": phase, "workflow": FAILOVER, "event": "probe_deposit", "outcome": outcome}
        builder.timeline.add("workflow_step", data, at(moment))
    ledger = _ledger(acked)
    snapshot = {
        "label": "ledger",
        "failover_deposit_outcomes": ["deposited", "skipped"],
        **ledger.to_data(),
    }
    builder.timeline.add("state_snapshot", snapshot, at(181.5))
    result = {"name": "RESULT", "verdict": VERDICT_PASS, "reason": None, "detail": None}
    builder.timeline.add("phase", result, at(182))
    meta = RunMeta(VERDICT_PASS, None, None, PRIMARY, SECONDARY)
    return builder, ledger, meta


def render_sample() -> tuple[str, str]:
    """(timeline JSON, report Markdown) of the synthetic drill."""
    builder, ledger, meta = build_sample()
    metrics = compute_metrics(builder.timeline, PRIMARY, SECONDARY)
    return builder.timeline.to_json() + "\n", render_run_report(
        builder.timeline, metrics, ledger, meta
    )


def formal_samples() -> dict[tuple[int, str], str]:
    """The formal report fixtures: (number of runs, language) to Markdown."""
    first = build_run(build_sample()[0].timeline)
    second = build_run(build_sample(SECOND_RUN_ID, 1.25)[0].timeline)
    samples: dict[tuple[int, str], str] = {}
    for count, runs in ((1, [first]), (2, [first, second])):
        documents = build_documents(runs, TECHNICAL_SAMPLE)
        samples[(count, "en")] = documents.english
        samples[(count, "es")] = documents.spanish
    return samples


if __name__ == "__main__":
    for key, text in formal_samples().items():
        FORMAL_FIXTURES[key].write_bytes(text.encode("utf-8"))
    timeline_text, report_text = render_sample()
    FIXTURES.mkdir(exist_ok=True)
    TIMELINE_FIXTURE.write_bytes(timeline_text.encode("utf-8"))
    REPORT_FIXTURE.write_bytes(report_text.encode("utf-8"))
