import dataclasses
import io
import re
from pathlib import Path

import pytest

from dr_drill.dns import DNS_PERIOD_SECONDS
from dr_drill.formal_report import (
    EN,
    EN_DOCUMENT,
    ES,
    ES_DOCUMENT,
    OUTCOME_LABELS,
    TEXTS,
    Environment,
    FormalReportError,
    Row,
    RunData,
    _direction_events,
    _range_text,
    _reason,
    build_documents,
    build_run,
    formal_report_command,
    generate,
    load_run,
    metric_rows,
    read_environment,
    render_formal_report,
    run_findings,
)
from dr_drill.ledger import ConsistencyReading, LedgerReading, verify_ledger
from dr_drill.metrics import TRAFFIC_STEP, missing
from dr_drill.probe import (
    AUTH_PERIOD_SECONDS,
    DEFAULT_RATE_SECONDS,
    READ_PERIOD_SECONDS,
    Abandoned,
    Acknowledged,
)
from dr_drill.redact import OutputGuard
from dr_drill.timeline import Timeline

from .sample_run import (
    FORMAL_FIXTURES,
    SECOND_RUN_ID,
    TECHNICAL_SAMPLE,
    build_sample,
    formal_samples,
)
from .timeline_builder import PRIMARY, SECONDARY, at

ENVIRONMENT = Environment("db.x1.sample", "9.9")
ACCOUNT_LIKE = re.compile(r"(?<!\d)\d{12}(?!\d)|arn:aws|@|nube-?segura", re.IGNORECASE)


def sample_run(run_id: str = "2026-11-11T12-00-00Z", lag_factor: float = 1.0) -> RunData:
    return build_run(build_sample(run_id, lag_factor)[0].timeline)


def filtered(run: RunData, drop: str, keep: object = None) -> RunData:
    """The same run without the events of one kind (or without those failing `keep`)."""
    timeline = Timeline(run.run_id, OutputGuard(), lambda: at(0))
    for event in run.timeline.events():
        if event.kind != drop or (callable(keep) and keep(event)):
            timeline.add(event.kind, event.data, event.time_utc)
    return build_run(timeline)


def test_fixtures_are_what_the_helper_generates() -> None:
    for key, text in formal_samples().items():
        assert FORMAL_FIXTURES[key].read_bytes().decode("utf-8") == text


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("lang", [EN, ES])
def test_golden_documents_from_the_stored_fixtures(count: int, lang: str) -> None:
    runs = [sample_run()] + ([sample_run(SECOND_RUN_ID, 1.25)] if count == 2 else [])
    text = render_formal_report(lang, runs, read_environment(TECHNICAL_SAMPLE))
    assert text == FORMAL_FIXTURES[(count, lang)].read_text(encoding="utf-8")
    assert OutputGuard().reason(text) is None
    assert ACCOUNT_LIKE.search(text) is None
    assert "\r" not in text


def test_spanish_links_to_the_english_document_before_any_section() -> None:
    text = FORMAL_FIXTURES[(1, ES)].read_text(encoding="utf-8")
    assert text.index("../docs/dr-test-report.md") < text.index("## Propósito")
    english = FORMAL_FIXTURES[(1, EN)].read_text(encoding="utf-8")
    assert english.index("../docs-es/dr-test-report.md") < english.index("## Purpose")


def test_both_languages_have_the_same_texts_and_placeholders() -> None:
    assert TEXTS[EN].keys() == TEXTS[ES].keys()
    assert OUTCOME_LABELS[EN].keys() == OUTCOME_LABELS[ES].keys()
    for key, text in TEXTS[EN].items():
        assert set(re.findall(r"{(\w+)}", text)) == set(re.findall(r"{(\w+)}", TEXTS[ES][key])), key


def test_both_languages_have_the_same_tables() -> None:
    english = FORMAL_FIXTURES[(2, EN)].read_text(encoding="utf-8").splitlines()
    spanish = FORMAL_FIXTURES[(2, ES)].read_text(encoding="utf-8").splitlines()
    assert [line.count("|") for line in english if line.startswith("|")] == [
        line.count("|") for line in spanish if line.startswith("|")
    ]


def test_two_runs_show_both_values_and_the_range() -> None:
    text = FORMAL_FIXTURES[(2, EN)].read_text(encoding="utf-8")
    assert "### Range between the runs" in text
    assert (
        "| AuroraGlobalDBReplicationLag: maximum | 1300.0 ms | 1625.0 ms | 1300.0 ms to 1625.0 ms |"
        in text
    )
    assert "Run 2: 2026-11-12T12-00-00Z" in text
    assert "Run date (UTC): 2026-11-11, 2026-11-12" not in text  # same UTC date for both builders


def test_range_text_orders_the_values_and_marks_gaps() -> None:
    def row(number: float | None, unit: str = "s") -> Row:
        return Row("k", "name", "text", number, unit, "", "", "")

    assert _range_text(EN, row(9.0), row(3.0)) == "3.00 s to 9.00 s"
    assert _range_text(ES, row(40.0, "%"), row(60.0, "%")) == "de 40.0 % a 60.0 %"
    assert _range_text(EN, row(None), row(3.0)) == "not measured in every run"
    assert _range_text(EN, row(3.0), None) == "not measured in every run"


def test_negative_dns_convergence_is_reported_and_explained() -> None:
    run = sample_run()
    shifted = filtered(run, "dns", lambda event: event.data.get("first"))
    timeline = shifted.timeline
    timeline.add(
        "dns", {"first": False, "answer": ["x"], "previous": None, "observed": "t"}, at(89)
    )
    negative = build_run(timeline)
    findings = run_findings(EN, negative)
    assert any(
        "negative" in item and "before the traffic-switch step ended" in item for item in findings
    )
    assert any("negativa" in item for item in run_findings(ES, negative))


def test_a_missing_metric_prints_not_measured_with_the_reason() -> None:
    run = filtered(sample_run(), "dns")
    english = " ".join(run_findings(EN, run))
    assert "DNS convergence was not measured (no changed DNS answer was observed" in english
    spanish = " ".join(run_findings(ES, run))
    assert "no se midió (no se observó ninguna respuesta DNS distinta" in spanish
    rows = {row.key: row for row in metric_rows(EN, run)}
    assert rows["to-secondary:dns"].text.startswith("not measured: no changed DNS answer")
    assert rows["to-secondary:dns"].number is None


def test_missing_lag_samples_are_reported_not_invented() -> None:
    run = filtered(sample_run(), "metric_sample")
    assert any(
        "was not measured (no AuroraGlobalDBReplicationLag samples" in x
        for x in run_findings(EN, run)
    )
    assert any("no se registraron muestras" in x for x in run_findings(ES, run))


def test_the_lag_is_stated_as_a_bound_not_a_measurement() -> None:
    findings = " ".join(run_findings(EN, sample_run()))
    assert "is a bound on what an unplanned failover could lose" in findings
    assert "not a measurement of the RPO of an unplanned failover" in findings


def test_skipped_deposits_are_explained_by_rule() -> None:
    run = dataclasses.replace(
        sample_run(), deposits=(("to-secondary", "skipped"), ("to-primary", "skipped"))
    )
    findings = run_findings(EN, run)
    assert (
        "Failover probe deposits: to-secondary: SKIPPED (not posted); "
        "to-primary: SKIPPED (not posted)." in findings
    )
    assert any("Both failover probe deposits were SKIPPED" in item for item in findings)
    assert any("Los dos depósitos" in item for item in run_findings(ES, run))


def test_other_deposit_outcomes() -> None:
    mixed = dataclasses.replace(
        sample_run(), deposits=(("to-secondary", "skipped"), ("to-primary", "unknown"))
    )
    assert any("that were SKIPPED are not posted" in item for item in run_findings(EN, mixed))
    posted = dataclasses.replace(sample_run(), deposits=(("to-secondary", "deposited"),))
    findings = run_findings(EN, posted)
    assert not any("SKIPPED" in item for item in findings)
    none = dataclasses.replace(sample_run(), deposits=())
    assert "No failover probe deposit was classified." in run_findings(EN, none)


def _ledger(**overrides: object) -> object:
    consistency = ConsistencyReading(True, False, 10_001, 10_001, 0, True, True, {PRIMARY: 12})
    acked = [Acknowledged("w0001", "k1", "deposit", None, 201)]
    base = {
        "baseline": LedgerReading(10, 10_000),
        "final": LedgerReading(11, 10_001),
        "consistency": consistency,
        "acked": acked,
        "abandoned": [],
        "pending": [],
    }
    base.update(overrides)
    return verify_ledger(**base)  # type: ignore[arg-type]


def test_rpo_statements_follow_the_ledger_status() -> None:
    run = sample_run()
    cases = {
        "zero": (_ledger(), "Measured RPO: 0 acknowledged writes lost"),
        "loss": (_ledger(final=LedgerReading(10, 10_000)), "a definite loss of between 1 and 1"),
        "band": (
            _ledger(
                final=LedgerReading(11, 10_001), abandoned=[Abandoned("w2", "k2", "deposit", 3)]
            ),
            "not verifiable",
        ),
        "unverified": (_ledger(final=None), "not verified"),
    }
    for ledger, expected in cases.values():
        text = " ".join(run_findings(EN, dataclasses.replace(run, ledger=ledger)))  # type: ignore[arg-type]
        assert expected in text
    headline = render_formal_report(
        EN, [dataclasses.replace(run, ledger=cases["loss"][0])], ENVIRONMENT
    )  # type: ignore[arg-type]
    assert "loss: 1 to 1 acknowledged writes" in headline


def test_run_without_a_ledger_says_so() -> None:
    run = dataclasses.replace(sample_run(), ledger=None)
    assert "Measured RPO: not measured, the run has no ledger result." in run_findings(EN, run)
    text = render_formal_report(EN, [run], ENVIRONMENT)
    assert (
        "| 1 | not read | not read | not read | not read | not read | not read | not read |" in text
    )
    assert "| not verified |" in text


def test_ledger_problems_and_missing_consistency_are_shown() -> None:
    ledger = _ledger(final=LedgerReading(10, 10_000), consistency=None)
    run = dataclasses.replace(sample_run(), ledger=ledger)  # type: ignore[arg-type]
    assert any(item.startswith("Ledger problem reported") for item in run_findings(EN, run))
    assert "| 1 | 10 | 11 | 10 | 0 | no | not read | not read |" in render_formal_report(
        EN, [run], ENVIRONMENT
    )


def test_refused_probe_records_are_counted_from_the_notes() -> None:
    run = sample_run()
    run.timeline.note("request write w0013 refused: it contains a 12-digit number")
    run.timeline.note("an unrelated note")
    refused = build_run(run.timeline)
    assert refused.refused == 1
    assert any(
        item.startswith("1 probe records were refused") for item in run_findings(EN, refused)
    )
    assert not any("refused by the output guard" in item for item in run_findings(EN, run))


def test_dns_answers_rotating_are_reported_without_the_addresses() -> None:
    run = sample_run()
    for second in (90, 100, 110, 120):
        run.timeline.add(
            "dns",
            {"first": False, "answer": ["192.0.2.1"], "previous": None, "observed": "t"},
            at(second),
        )
    findings = run_findings(EN, build_run(run.timeline))
    assert any(
        "changed 6 times during the whole run for 2 traffic switches" in item for item in findings
    )
    assert not any("192.0.2" in item for item in findings)


def test_failed_recovery_and_missing_reads_have_their_own_sentences() -> None:
    run = sample_run()
    direction = run.metrics.directions[0]
    gone = dataclasses.replace(
        direction,
        write_rto=missing(
            "no failed or retried request was seen in the window (no outage observed)"
        ),
    )
    no_read = dataclasses.replace(
        run.metrics.directions[1], read_rto=missing("the run has no recorded times")
    )
    metrics = dataclasses.replace(run.metrics, directions=(gone, no_read))
    changed = dataclasses.replace(run, metrics=metrics)
    english = run_findings(EN, changed)
    assert (
        "`to-secondary`: the recovery was not measured (no failed or retried request was seen "
        "in the window (no outage observed))." in english
    )
    assert "`to-primary`: the read RTO was not measured (the run has no recorded times)." in english
    spanish = run_findings(ES, changed)
    assert any(
        "la recuperación no se midió (no se vio ninguna solicitud fallida" in item
        for item in spanish
    )
    assert any(
        "el RTO de lectura no se midió (la ejecución no tiene tiempos registrados)" in item
        for item in spanish
    )


def test_sign_in_without_attempts_is_not_measured() -> None:
    run = filtered(sample_run(), "request", lambda event: event.data.get("probe") != "auth")
    findings = run_findings(EN, run)
    assert any(
        "sign-in availability was not measured (no authentication probe answer" in x
        for x in findings
    )


def test_verdict_with_a_stop_reason() -> None:
    run = dataclasses.replace(sample_run(), verdict="FAIL", reason="WORKFLOW_FAILED")
    assert run_findings(EN, run)[0] == "Verdict of the runner: FAIL (stop reason: WORKFLOW_FAILED)."


def test_a_run_without_phases_prints_empty_sections() -> None:
    run = sample_run()
    bare = dataclasses.replace(run, metrics=dataclasses.replace(run.metrics, phases={}))
    text = render_formal_report(EN, [bare], ENVIRONMENT)
    assert "No phase started." in text
    assert "No event was recorded for this direction." in text


def test_reasons_are_translated_when_known_and_kept_otherwise() -> None:
    assert _reason(ES, "the failover was not dispatched") == "el failover no se lanzó"
    assert _reason(ES, "the fence phase did not start") == "la fase fence no empezó"
    assert _reason(ES, "something new") == "something new"
    assert _reason(EN, "the failover was not dispatched") == "the failover was not dispatched"
    assert _reason(EN, None) == ""


def test_environment_is_read_from_the_technical_document() -> None:
    assert read_environment(TECHNICAL_SAMPLE) == ENVIRONMENT
    assert read_environment("| Database | something else |") == Environment(None, None)
    text = render_formal_report(EN, [sample_run()], Environment(None, None))
    assert "instance class and engine version were not found" in text


def test_render_rejects_bad_run_sets() -> None:
    run = sample_run()
    with pytest.raises(FormalReportError, match="one or 2 runs"):
        render_formal_report(EN, [], ENVIRONMENT)
    with pytest.raises(FormalReportError, match="one or 2 runs"):
        render_formal_report(EN, [run, run, run], ENVIRONMENT)
    other = dataclasses.replace(run, primary="eu-west-1")
    with pytest.raises(FormalReportError, match="different regions"):
        render_formal_report(EN, [run, other], ENVIRONMENT)


def test_build_run_needs_the_regions_of_a_complete_drill() -> None:
    empty = Timeline("2026-11-11T12-00-00Z", OutputGuard(), lambda: at(0))
    with pytest.raises(FormalReportError, match="not a complete drill"):
        build_run(empty)


def test_build_run_reports_a_malformed_ledger_snapshot() -> None:
    timeline = build_sample()[0].timeline
    timeline.add("state_snapshot", {"label": "ledger", "rpo_status": "zero_loss"}, at(183))
    with pytest.raises(FormalReportError, match="lacks the field"):
        build_run(timeline)


def test_a_run_without_a_result_event_has_an_unknown_verdict() -> None:
    run = filtered(sample_run(), "phase", lambda event: event.data.get("name") != "RESULT")
    assert run.verdict == "UNKNOWN"
    assert run.reason is None


def test_load_run_reads_only_the_timeline(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "timeline.json").write_text(build_sample()[0].timeline.to_json(), encoding="utf-8")
    assert load_run(folder, OutputGuard()).primary == PRIMARY
    assert load_run(folder, OutputGuard()).secondary == SECONDARY
    with pytest.raises(FormalReportError, match=re.escape("cannot read timeline.json")):
        load_run(tmp_path / "missing", OutputGuard())
    (folder / "timeline.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(FormalReportError, match="not a valid timeline"):
        load_run(folder, OutputGuard())
    (folder / "timeline.json").write_text('{"schema_version": 9}', encoding="utf-8")
    with pytest.raises(FormalReportError, match="not a valid timeline"):
        load_run(folder, OutputGuard())


def make_repo(tmp_path: Path, runs: int = 1) -> tuple[Path, list[Path]]:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "technical.md").write_text(TECHNICAL_SAMPLE, encoding="utf-8")
    folders = []
    for index in range(runs):
        folder = tmp_path / f"run{index}"
        folder.mkdir()
        run_id = "2026-11-11T12-00-00Z" if index == 0 else SECOND_RUN_ID
        (folder / "timeline.json").write_text(
            build_sample(run_id, 1.0 + 0.25 * index)[0].timeline.to_json(), encoding="utf-8"
        )
        folders.append(folder)
    return tmp_path, folders


def test_generate_writes_both_documents_as_utf8_with_lf(tmp_path: Path) -> None:
    root, folders = make_repo(tmp_path, 2)
    english, spanish = generate(folders, root, OutputGuard())
    assert english == root / EN_DOCUMENT
    assert spanish == root / ES_DOCUMENT
    assert english.read_bytes().decode("utf-8") == FORMAL_FIXTURES[(2, EN)].read_text(
        encoding="utf-8"
    )
    assert b"\r" not in spanish.read_bytes()
    assert "Informe de la prueba de DR" in spanish.read_text(encoding="utf-8")


def test_generate_refuses_bad_inputs(tmp_path: Path) -> None:
    root, folders = make_repo(tmp_path, 2)
    with pytest.raises(FormalReportError, match="different run folders"):
        generate([folders[0], folders[0]], root, OutputGuard())
    with pytest.raises(FormalReportError, match="different run folders"):
        generate([*folders, folders[0]], root, OutputGuard())
    (root / "docs" / "technical.md").unlink()
    with pytest.raises(FormalReportError, match=re.escape("cannot read docs/technical.md")):
        generate(folders, root, OutputGuard())


def test_generate_writes_nothing_when_the_guard_refuses(tmp_path: Path) -> None:
    root, folders = make_repo(tmp_path)
    guard = OutputGuard()
    guard.register("Informe de la prueba de DR")
    with pytest.raises(
        FormalReportError, match=re.escape("dr-test-report.md was not written because")
    ):
        generate(folders, root, guard)
    assert not (root / EN_DOCUMENT).exists()
    assert not (root / ES_DOCUMENT).exists()


def test_the_command_reports_the_written_files_or_the_error(tmp_path: Path) -> None:
    root, folders = make_repo(tmp_path)
    runner = tmp_path / "runner"
    runner.mkdir()
    environ = {
        "E2E_USERNAME": "user-not-in-report",
        "E2E_PASSWORD": "password-not-in-report",
        "API_DOMAIN_NAME": "api.example.test",
        "GITHUB_REPOSITORY": "owner/repo",
        "AWS_PROFILE": "profile",
        "AWS_REGION_PRIMARY": "us-east-2",
        "AWS_REGION_SECONDARY": "us-west-2",
        "AWS_REGION_GLOBAL": "us-east-1",
        "ENV_TYPE": "prod",
    }
    out, err = io.StringIO(), io.StringIO()
    assert formal_report_command(folders, root, out, err, runner, environ) == 0
    assert out.getvalue() == "written: docs/dr-test-report.md\nwritten: docs-es/dr-test-report.md\n"
    assert err.getvalue() == ""
    out, err = io.StringIO(), io.StringIO()
    assert formal_report_command([tmp_path / "nowhere"], root, out, err, runner, environ) == 2
    assert err.getvalue().startswith("formal-report: cannot read timeline.json")
    assert out.getvalue() == ""


def test_the_command_defaults_to_the_console(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root, folders = make_repo(tmp_path)
    assert formal_report_command(folders, root) == 0
    assert "written: docs/dr-test-report.md" in capsys.readouterr().out
    assert formal_report_command([tmp_path / "nowhere"], root) == 2
    assert "formal-report:" in capsys.readouterr().err


def test_build_documents_returns_both_languages() -> None:
    documents = build_documents([sample_run()], TECHNICAL_SAMPLE)
    assert documents.english.startswith("# DR drill test report")
    assert documents.spanish.startswith("# Informe de la prueba de DR")


def test_measure_in_tables_never_shows_a_zero_for_missing_data() -> None:
    rows = metric_rows(EN, filtered(sample_run(), "metric_sample"))
    lag = [row for row in rows if row.key.startswith("AuroraGlobalDBReplicationLag")]
    assert lag
    assert all(row.number is None and row.text.startswith("not measured") for row in lag)


def test_sparse_runs_degrade_to_shorter_sections() -> None:
    run = sample_run()
    no_traffic = filtered(run, "workflow_step", lambda e: e.data.get("step") != TRAFFIC_STEP)
    assert not any(
        "changed DNS answer after" in x
        for _, x in _direction_events(EN, no_traffic, no_traffic.metrics.directions[0])
    )
    no_failures = filtered(run, "request", lambda e: e.data.get("ok") is not False)
    events = _direction_events(EN, no_failures, no_failures.metrics.directions[0])
    assert not any("First failed write" in text for _, text in events)
    no_steps = filtered(run, "workflow_step", lambda e: e.data.get("event") != "step")
    assert not any(
        "Step " in text
        for _, text in _direction_events(EN, no_steps, no_steps.metrics.directions[0])
    )
    assert all("largest share" not in item for item in run_findings(EN, no_steps))
    assert filtered(run, "state_snapshot").ledger is None


def test_unverified_and_unverifiable_ledgers_in_the_tables() -> None:
    run = sample_run()
    unverified = dataclasses.replace(run, ledger=_ledger(final=None))  # type: ignore[arg-type]
    assert "| 1 | 10 | 11 | not read | 0 | not read | yes | 0 |" in render_formal_report(
        EN, [unverified], ENVIRONMENT
    )
    band = _ledger(abandoned=[Abandoned("w2", "k2", "deposit", 3)], final=LedgerReading(11, 10_001))
    text = render_formal_report(EN, [dataclasses.replace(run, ledger=band)], ENVIRONMENT)  # type: ignore[arg-type]
    assert "not verifiable: 0 to 1 possible" in text


def test_missing_sign_in_data_in_the_metric_rows() -> None:
    run = filtered(sample_run(), "request", lambda event: event.data.get("probe") != "auth")
    rows = {row.key: row for row in metric_rows(EN, run)}
    assert rows["to-secondary:auth"].text.startswith("not measured: no authentication probe")
    assert rows["to-secondary:auth_run"].number is None


SKIPPED = (("to-secondary", "skipped"), ("to-primary", "skipped"))
WORKFLOW_WITHOUT_TOKEN = (
    '      - run: echo "${COGNITO_ID_TOKEN:-}"\n        # COGNITO_ID_TOKEN note\n'
)
WORKFLOW_WITH_TOKEN = "        env:\n          COGNITO_ID_TOKEN: ${{ steps.login.outputs.token }}\n"


def test_the_skipped_deposit_cause_is_stated_only_when_the_workflow_shows_no_token() -> None:
    run = dataclasses.replace(sample_run(), deposits=SKIPPED)
    assert read_environment(TECHNICAL_SAMPLE, WORKFLOW_WITHOUT_TOKEN).failover_without_token
    assert not read_environment(TECHNICAL_SAMPLE, WORKFLOW_WITH_TOKEN).failover_without_token
    assert not read_environment(TECHNICAL_SAMPLE, None).failover_without_token
    assert not any("Expected cause" in item for item in run_findings(EN, run))
    known = run_findings(EN, run, True)
    assert any(item.startswith("Expected cause: `failover.yml`") for item in known)
    assert any(item.startswith("Causa esperada:") for item in run_findings(ES, run, True))
    unclassified = dataclasses.replace(run, deposits=(("to-secondary", "deposited"),))
    assert not any("Expected cause" in item for item in run_findings(EN, unclassified, True))
    documents = build_documents([run], TECHNICAL_SAMPLE, WORKFLOW_WITHOUT_TOKEN)
    assert "Expected cause" in documents.english
    assert "Causa esperada" in documents.spanish
    assert "Expected cause" not in build_documents([run], TECHNICAL_SAMPLE).english


def test_cadences_in_the_text_come_from_the_probe_constants() -> None:
    for lang in (EN, ES):
        text = render_formal_report(lang, [sample_run()], ENVIRONMENT)
        assert "{" not in text
    english = render_formal_report(EN, [sample_run()], ENVIRONMENT)
    assert f"one attempt every {DEFAULT_RATE_SECONDS:g} s" in english
    assert f"`GET /account` every {READ_PERIOD_SECONDS:g} s" in english
    assert f"(every {AUTH_PERIOD_SECONDS:g} s)" in english
    assert f"every {DNS_PERIOD_SECONDS:g} s from the workstation" in english
    assert "withdrawals of 0.01 alternating" in english


def test_a_finding_without_unavailability_does_not_nest_the_missing_text() -> None:
    run = sample_run()
    direction = dataclasses.replace(
        run.metrics.directions[0], write_unavailability=missing("no failed write")
    )
    changed = dataclasses.replace(
        run,
        metrics=dataclasses.replace(
            run.metrics, directions=(direction, *run.metrics.directions[1:])
        ),
    )
    english = run_findings(EN, changed)
    assert any(
        "the write unavailability was not measured (no failed write)" in item for item in english
    )
    assert not any("unavailable for not measured" in item for item in english)
    assert any(
        "la indisponibilidad de escritura no se midió" in x for x in run_findings(ES, changed)
    )


def test_the_command_guard_uses_the_settings_secrets_or_warns(tmp_path: Path) -> None:
    root, folders = make_repo(tmp_path)
    runner = tmp_path / "runner"
    runner.mkdir()
    out, err = io.StringIO(), io.StringIO()
    assert formal_report_command(folders, root, out, err, runner, {}) == 0
    assert "only the shape checks of the output guard apply" in err.getvalue()
    environ = {
        "E2E_USERNAME": "Informe",
        "E2E_PASSWORD": "secret-value-1",
        "API_DOMAIN_NAME": "api.example.test",
        "GITHUB_REPOSITORY": "owner/repo",
        "AWS_PROFILE": "profile",
        "AWS_REGION_PRIMARY": "us-east-2",
        "AWS_REGION_SECONDARY": "us-west-2",
        "AWS_REGION_GLOBAL": "us-east-1",
        "ENV_TYPE": "prod",
    }
    out, err = io.StringIO(), io.StringIO()
    assert formal_report_command(folders, root, out, err, runner, environ) == 2
    assert "was not written because it contains a known secret value" in err.getvalue()
    assert "Informe" not in out.getvalue()
