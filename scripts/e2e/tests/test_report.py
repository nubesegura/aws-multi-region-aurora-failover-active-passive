import json
from datetime import UTC, datetime
from pathlib import Path

from dr_drill.drill import DrillResult
from dr_drill.ledger import ConsistencyReading, LedgerReading, LedgerReport, verify_ledger
from dr_drill.metrics import compute_metrics
from dr_drill.phases import (
    VERDICT_ABORTED,
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
)
from dr_drill.probe import Abandoned, Acknowledged
from dr_drill.redact import OutputGuard
from dr_drill.report import (
    RunMeta,
    render_run_report,
    run_folder,
    run_meta_from_result,
    write_run_outputs,
)
from dr_drill.timeline import Timeline

from .sample_run import (
    REPORT_FIXTURE,
    TIMELINE_FIXTURE,
    build_sample,
    render_sample,
)
from .timeline_builder import PRIMARY, SECONDARY, TimelineBuilder, at

FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmYWtlLXVzZXIifQ.c2lnbmF0dXJlLWZha2U"


def render(builder: TimelineBuilder, ledger: LedgerReport | None, meta: RunMeta) -> str:
    metrics = compute_metrics(builder.timeline, PRIMARY, SECONDARY)
    return render_run_report(builder.timeline, metrics, ledger, meta)


def test_fixture_is_what_the_helper_generates() -> None:
    timeline_text, report_text = render_sample()
    assert TIMELINE_FIXTURE.read_bytes().decode("utf-8") == timeline_text
    assert REPORT_FIXTURE.read_bytes().decode("utf-8") == report_text


def test_golden_report_from_the_stored_timeline() -> None:
    timeline = Timeline.from_json(TIMELINE_FIXTURE.read_text(encoding="utf-8"), OutputGuard())
    _, ledger, meta = build_sample()
    metrics = compute_metrics(timeline, PRIMARY, SECONDARY)
    report = render_run_report(timeline, metrics, ledger, meta)
    assert report == REPORT_FIXTURE.read_text(encoding="utf-8")
    assert OutputGuard().reason(report) is None
    assert "\r" not in report


def test_report_has_every_required_section() -> None:
    report = REPORT_FIXTURE.read_text(encoding="utf-8")
    for heading in (
        "**Verdict: PASS**",
        "## Phases",
        "| Metric | Value | Definition | Source | Resolution |",
        "## Breakdown per direction",
        "Measured RPO:",
        "Replication lag bound:",
        "### Band and failover probe deposits",
        "## Errors, retries and latency",
        "## What was not measured",
        "## Limitations",
    ):
        assert heading in report


def test_failed_run_names_the_stop_reason() -> None:
    builder, ledger, _ = build_sample()
    meta = RunMeta(
        VERDICT_FAIL,
        "WORKFLOW_FAILED",
        "failover ended with conclusion failure",
        PRIMARY,
        SECONDARY,
        ("a warning",),
    )
    report = render(builder, ledger, meta)
    assert "**Verdict: FAIL - stop reason: WORKFLOW_FAILED**" in report
    assert "failover ended with conclusion failure" in report
    assert "- a warning" in report
    assert "do not unfence by hand" in report


def test_aborted_run_without_ledger_and_without_phases() -> None:
    builder = TimelineBuilder()
    meta = RunMeta(VERDICT_ABORTED, None, None, PRIMARY, SECONDARY)
    report = render(builder, None, meta)
    assert "**Verdict: ABORTED" in report
    assert "No phase started." in report
    assert "The ledger was not checked" in report
    assert "No probe attempt was recorded." in report
    assert "No failover probe deposit was classified" in report
    assert "not measured" in report
    assert "maximum | not measured: no AuroraGlobalDBReplicationLag samples" in report


def test_inconclusive_run_shows_the_band_and_the_uncertain_deposits() -> None:
    builder = TimelineBuilder()
    for phase, outcome in (("to-secondary", "unknown"), ("to-primary", "rejected")):
        data = {
            "phase": phase,
            "workflow": "failover",
            "event": "probe_deposit",
            "outcome": outcome,
        }
        builder.timeline.add("workflow_step", data, at(1))
    acked = [Acknowledged("w0001", "k1", "deposit", None, 201)]
    abandoned = [Abandoned("w0002", "k2", "withdrawal", 3)]
    consistency = ConsistencyReading(True, False, 10_001, 10_001, 0, True, True, {PRIMARY: 12})
    ledger = verify_ledger(
        LedgerReading(10, 10_000),
        LedgerReading(11, 10_001),
        consistency,
        acked,
        abandoned,
        [],
        0,
        1,
    )
    meta = RunMeta(
        VERDICT_INCONCLUSIVE, "RPO_NOT_VERIFIED", "band hides a loss", PRIMARY, SECONDARY
    )
    report = render(builder, ledger, meta)
    assert "**Verdict: INCONCLUSIVE - RPO_NOT_VERIFIED**" in report
    assert "Not verifiable: between 0 and 2" in report
    assert "| to-secondary | unknown | log missing, cut or not recognised" in report
    assert "| to-primary | rejected | HTTP error" in report
    assert "Uncertainty band: 2 = 1 abandoned or pending probe movements + 1 uncertain" in report


def test_definite_loss_and_unverified_ledger_statements() -> None:
    consistency = ConsistencyReading(True, False, 10_001, 10_001, 0, True, True, {})
    acked = [Acknowledged(f"w{n}", f"k{n}", "deposit", None, 201) for n in range(3)]
    lost = verify_ledger(
        LedgerReading(10, 10_000), LedgerReading(11, 10_003), consistency, acked, [], []
    )
    unverified = verify_ledger(LedgerReading(10, 10_000), None, None, acked, [], [])
    meta = RunMeta(VERDICT_FAIL, "LEDGER_MISMATCH", None, PRIMARY, SECONDARY)
    builder = TimelineBuilder()
    assert "Definite loss: between 2 and 2" in render(builder, lost, meta)
    text = render(builder, unverified, meta)
    assert "Not verified: the movement count could not be checked." in text
    assert "Consistency: not read." in text
    assert "- Note: the movement count could not be verified" in text


def test_run_meta_from_a_drill_result() -> None:
    result = DrillResult(VERDICT_FAIL, 1, "BALANCE_CHAIN", "drift", None, (), ("w",))
    meta = run_meta_from_result(result, PRIMARY, SECONDARY)
    assert (meta.verdict, meta.reason, meta.detail, meta.warnings) == (
        VERDICT_FAIL,
        "BALANCE_CHAIN",
        "drift",
        ("w",),
    )


def test_run_folder_is_named_by_the_utc_time(tmp_path: Path) -> None:
    now = datetime(2026, 11, 11, 12, 30, 5, tzinfo=UTC)
    assert run_folder(tmp_path, now) == tmp_path / "2026-11-11T12-30-05Z"


def test_outputs_are_written_into_a_new_folder(tmp_path: Path) -> None:
    builder, ledger, meta = build_sample()
    report = render(builder, ledger, meta)
    folder = tmp_path / "reports" / "e2e" / "2026-11-11T12-00-00Z"
    outputs = write_run_outputs(
        folder, builder.timeline, report, OutputGuard(), "line one\nline two\n"
    )
    assert outputs.refused == ()
    assert sorted(path.name for path in outputs.written) == [
        "report.md",
        "run.log",
        "timeline.json",
    ]
    assert (folder / "run.log").read_bytes() == b"line one\nline two\n"
    assert (folder / "report.md").read_text(encoding="utf-8") == report
    assert (
        json.loads((folder / "timeline.json").read_text(encoding="utf-8"))["run_id"]
        == builder.timeline.run_id
    )
    assert b"\r" not in (folder / "timeline.json").read_bytes()


def test_an_aborted_run_still_writes_the_three_files(tmp_path: Path) -> None:
    builder = TimelineBuilder()
    builder.phase("baseline", 0)
    builder.timeline.add("phase", {"name": "INTERRUPTED"}, at(3))
    meta = RunMeta(VERDICT_ABORTED, None, None, PRIMARY, SECONDARY)
    outputs = write_run_outputs(
        tmp_path / "run", builder.timeline, render(builder, None, meta), OutputGuard(), "aborted\n"
    )
    assert outputs.refused == ()
    assert {path.name for path in (tmp_path / "run").iterdir()} == {
        "report.md",
        "run.log",
        "timeline.json",
    }
    assert "ABORTED" in (tmp_path / "run" / "report.md").read_text(encoding="utf-8")


def test_an_existing_run_log_is_kept_when_no_text_is_given(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "run.log").write_bytes(b"appended by the run log\n")
    outputs = write_run_outputs(folder, TimelineBuilder().timeline, "report", OutputGuard())
    assert (folder / "run.log").read_bytes() == b"appended by the run log\n"
    assert sorted(path.name for path in outputs.written) == ["report.md", "timeline.json"]


def test_the_guard_refuses_unsafe_text_without_writing_it(tmp_path: Path) -> None:
    builder = TimelineBuilder()
    outputs = write_run_outputs(
        tmp_path / "run", builder.timeline, f"token {FAKE_JWT}", OutputGuard(), "ok line\n"
    )
    assert [path.name for path in outputs.written] == ["run.log", "timeline.json"]
    assert len(outputs.refused) == 1
    assert "report.md was not written" in outputs.refused[0]
    assert FAKE_JWT not in outputs.refused[0]
    assert not (tmp_path / "run" / "report.md").exists()


def test_a_known_secret_in_the_log_or_the_timeline_is_refused(tmp_path: Path) -> None:
    guard = OutputGuard(["Fake-Password-123!"])
    builder = TimelineBuilder()
    builder.timeline.note("harmless")
    outputs = write_run_outputs(
        tmp_path / "a", builder.timeline, "report", guard, "Fake-Password-123! leaked\n"
    )
    assert [path.name for path in outputs.written] == ["report.md", "timeline.json"]
    leaky = Timeline("run", OutputGuard(), lambda: at(0))
    leaky.note("Fake-Password-123!")
    outputs = write_run_outputs(tmp_path / "b", leaky, "report", guard)
    assert [path.name for path in outputs.written] == ["report.md"]
    assert "timeline" in outputs.refused[0]
    assert "Fake-Password-123!" not in outputs.refused[0]
