from dr_drill.preconditions import (
    PreconditionReport,
    api_stack_name,
    function_name,
    mask_account,
    run_preconditions,
)
from tests.fakes import ACCOUNT, FakePorts, make_settings

SETTINGS = make_settings()


def verdict(fake: FakePorts) -> PreconditionReport:
    return run_preconditions(fake.ports(), SETTINGS)


def failed_names(report: PreconditionReport) -> list[str]:
    return [check.name for check in report.failed]


def test_everything_ready_passes_and_masks_the_account() -> None:
    report = verdict(FakePorts())

    assert report.ok
    assert len(report.checks) == 8
    identity = report.checks[0]
    assert identity.detail == "account ********9012"
    assert ACCOUNT not in repr(report)


def test_names_follow_the_naming_convention() -> None:
    assert api_stack_name("us-west-2", "prod") == "stk-uswest2-drdemo-api-prod"
    assert function_name("us-east-2", "get-stats", "prod") == "fnc-useast2-drdemo-get-stats-prod"
    assert mask_account(ACCOUNT) == "********9012"


def test_stack_not_complete_is_named() -> None:
    fake = FakePorts()
    fake.stack_statuses["stk-uswest2-drdemo-api-prod"] = "UPDATE_ROLLBACK_COMPLETE"
    fake.stack_statuses["stk-useast1-drdemo-dns-prod"] = None

    report = verdict(fake)

    assert failed_names(report) == ["stacks"]
    detail = report.failed[0].detail
    assert "stk-uswest2-drdemo-api-prod in us-west-2 is UPDATE_ROLLBACK_COMPLETE" in detail
    assert "stk-useast1-drdemo-dns-prod in us-east-1 is missing" in detail


def test_each_stack_is_looked_up_in_the_region_that_hosts_it() -> None:
    fake = FakePorts()

    verdict(fake)

    assert fake.stack_queries == [
        ("us-east-2", "stk-useast2-drdemo-edge-prod"),
        ("us-east-1", "stk-useast1-drdemo-dns-prod"),
        ("us-east-2", "stk-useast2-drdemo-api-prod"),
        ("us-west-2", "stk-uswest2-drdemo-api-prod"),
    ]


def test_health_503_fails() -> None:
    fake = FakePorts()
    fake.health = 503

    report = verdict(fake)

    assert failed_names(report) == ["api health"]
    assert "503" in report.failed[0].detail


def test_fence_present_names_the_function() -> None:
    fake = FakePorts()
    fake.fenced.add("fnc-useast2-drdemo-get-stats-prod")

    report = verdict(fake)

    assert failed_names(report) == ["no fence"]
    assert "fnc-useast2-drdemo-get-stats-prod" in report.failed[0].detail


def test_all_twelve_functions_are_inspected() -> None:
    fake = FakePorts()

    verdict(fake)

    assert fake.calls.count("reserved_concurrency") == 12


def test_writer_not_in_primary_fails() -> None:
    fake = FakePorts()
    fake.writer = "us-west-2"

    report = verdict(fake)

    assert failed_names(report) == ["writer in primary"]
    assert "us-west-2" in report.failed[0].detail


def test_unknown_writer_fails() -> None:
    fake = FakePorts()
    fake.writer = None

    assert "unknown" in verdict(fake).failed[0].detail


def test_run_in_progress_fails() -> None:
    fake = FakePorts()
    fake.active = ["failover (in_progress)"]

    report = verdict(fake)

    assert failed_names(report) == ["no workflow in progress"]
    assert "failover (in_progress)" in report.failed[0].detail


def test_gh_not_logged_in_fails() -> None:
    fake = FakePorts()
    fake.logged_in = False

    assert failed_names(verdict(fake)) == ["gh login"]


def test_sign_in_failure_fails_without_the_message() -> None:
    fake = FakePorts()
    fake.sign_in_error = RuntimeError("secret detail " + ACCOUNT)

    report = verdict(fake)

    assert failed_names(report) == ["e2e user sign-in"]
    assert "RuntimeError" in report.failed[0].detail
    assert ACCOUNT not in report.failed[0].detail


def test_adapter_exception_is_a_failed_check_not_a_crash() -> None:
    fake = FakePorts()
    fake.fail_identity = True

    report = verdict(fake)

    assert failed_names(report) == ["aws identity"]
    assert ACCOUNT not in repr(report)
    assert len(report.checks) == 8


def test_a_failed_report_stops_the_run_before_any_write() -> None:
    fake = FakePorts()
    fake.writer = "us-west-2"
    writes: list[str] = []

    report = verdict(fake)
    if report.ok:
        writes.append("dispatch")

    assert failed_names(report) == ["writer in primary"]
    assert writes == []
    assert set(fake.calls) <= {
        "caller_account",
        "stack_status",
        "health_status",
        "reserved_concurrency",
        "writer_region",
        "active_runs",
        "authenticated",
        "id_token",
    }
