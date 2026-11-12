import pytest

from dr_drill.validate_log import ProbeDeposit, classify_probe_deposit

HEADER = "2026-11-11T12:08:00Z step 3/3: probe movement (deposit of 0.01)"
SKIPPED_LINE = (
    "2026-11-11T12:08:00Z   skipped (no COGNITO_ID_TOKEN provided for authenticated probe)"
)
DEPOSITED_LINE = (
    "2026-11-11T12:08:02Z   HTTP 201 processed_region=us-west-2 "
    "id=7d9f1c52-aaaa-4bbb-8ccc-0123456789ab balance=12.34"
)
PASSED = "2026-11-11T12:08:02Z validation passed"
EARLIER = (
    "2026-11-11T12:07:50Z step 2/3: deep health\n"
    "2026-11-11T12:07:51Z   HTTP 200 status=ok region(served_by_region)=us-west-2 "
    "database_role=writer\n"
)


def log(*lines: str) -> str:
    return EARLIER + "\n".join(lines) + "\n"


def test_a_missing_token_is_skipped() -> None:
    assert classify_probe_deposit(log(HEADER, SKIPPED_LINE, PASSED)) is ProbeDeposit.SKIPPED


def test_a_created_deposit_is_deposited() -> None:
    assert classify_probe_deposit(log(HEADER, DEPOSITED_LINE, PASSED)) is ProbeDeposit.DEPOSITED


def test_the_probe_flag_missing_is_skipped() -> None:
    line = "2026-11-11T12:08:00Z   skipped (add --probe to post one deposit of 0.01)"

    assert classify_probe_deposit(log(HEADER, line, PASSED)) is ProbeDeposit.SKIPPED


@pytest.mark.parametrize(
    "line",
    [
        "2026-11-11T12:08:02Z WARN  probe returned HTTP 400",
        "2026-11-11T12:08:02Z WARN  probe returned HTTP 409",
        "2026-11-11T12:08:02Z WARN  HTTP 503 db_unavailable: the serving region cannot write",
    ],
)
def test_an_http_error_is_rejected_not_deposited(line: str) -> None:
    assert classify_probe_deposit(log(HEADER, line)) is ProbeDeposit.REJECTED


@pytest.mark.parametrize("code", ["500", "502", "504", "000"])
def test_a_gateway_error_or_no_answer_may_hide_a_deposit_so_it_is_unknown(code: str) -> None:
    line = f"2026-11-11T12:08:02Z WARN  probe returned HTTP {code}"

    assert classify_probe_deposit(log(HEADER, line)) is ProbeDeposit.UNKNOWN


def test_github_prefixes_and_ansi_colours_are_ignored() -> None:
    prefix = "prod\tGroup 3 - validate the service on the custom domain\t"
    ansi_header = f"{prefix}\x1b[36;1m{HEADER}\x1b[0m"
    ansi_skipped = f"{prefix}﻿\x1b[33;1m{SKIPPED_LINE}\x1b[0m"

    assert classify_probe_deposit(log(ansi_header, ansi_skipped)) is ProbeDeposit.SKIPPED


def test_the_outcome_of_an_earlier_step_does_not_count() -> None:
    text = "  HTTP 201 processed_region=us-west-2\n" + log(HEADER, SKIPPED_LINE)

    assert classify_probe_deposit(text) is ProbeDeposit.SKIPPED


@pytest.mark.parametrize("text", [None, "", "\n\n", "no marker here\n"])
def test_a_missing_or_empty_log_is_unknown(text: str | None) -> None:
    assert classify_probe_deposit(text) is ProbeDeposit.UNKNOWN


def test_a_truncated_log_is_unknown() -> None:
    assert classify_probe_deposit(log(HEADER)) is ProbeDeposit.UNKNOWN


def test_a_header_followed_by_unrecognised_lines_is_unknown() -> None:
    assert classify_probe_deposit(log(HEADER, "2026-11-11T12:08:00Z   something else")) is (
        ProbeDeposit.UNKNOWN
    )
