import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from dr_drill.redact import OutputGuard
from dr_drill.runlog import RefusedOutput, RunLog

MOMENT = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=UTC)


def make_log(tmp_path: Path, moment: datetime = MOMENT) -> RunLog:
    return RunLog(tmp_path / "run" / "run.log", OutputGuard(["Fake-Password-123!"]), lambda: moment)


def test_line_format_and_millisecond_timestamp(tmp_path: Path) -> None:
    log = make_log(tmp_path)

    line = log.append("baseline", "deposit 0.01", "ok", "us-east-2")

    assert line == "2026-11-11T12:00:00.123Z | baseline | deposit 0.01 | ok | us-east-2"
    assert log.path.read_bytes() == (line + "\n").encode()


def test_empty_fields_are_dashes(tmp_path: Path) -> None:
    assert make_log(tmp_path).append("fence", "dispatch", "").endswith("| dispatch | - | -")


def test_separator_and_newlines_are_replaced(tmp_path: Path) -> None:
    line = make_log(tmp_path).append("a|b", "two\nlines", "x  y")

    assert line.split(" | ")[1:4] == ["a/b", "two lines", "x y"]


def test_time_is_converted_to_utc(tmp_path: Path) -> None:
    local = datetime(2026, 11, 11, 9, 0, 0, tzinfo=timezone(timedelta(hours=-3)))

    assert make_log(tmp_path, local).append("s", "a", "r").startswith("2026-11-11T12:00:00.000Z")


def test_lines_are_appended_with_lf(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append("s", "a", "1")
    log.append("s", "a", "2")

    data = log.path.read_bytes()
    assert data.count(b"\n") == 2
    assert b"\r" not in data


@pytest.mark.parametrize(
    "value",
    [
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmYWtlLXVzZXIifQ.c2lnbmF0dXJlLWZha2U",
        "123456789012",
        "arn:aws:iam::role/fake",
        "someone@example.com",
        "Fake-Password-123!",
    ],
)
def test_refused_line_is_not_written(tmp_path: Path, value: str) -> None:
    log = make_log(tmp_path)

    with pytest.raises(RefusedOutput) as raised:
        log.append("s", "login", value, "us-east-2")

    assert raised.value.field == "result"
    assert value not in str(raised.value)
    assert not log.path.exists()


def test_ordinary_lines_pass(tmp_path: Path) -> None:
    log = make_log(tmp_path)

    log.append("1.1", "key 3f2b8c1e-9d4a-4c7e-8b6a-1a2b3c4d5e6f", "balance 1234.56", "us-west-2")

    assert log.path.exists()


def test_a_naive_clock_value_is_rejected(tmp_path: Path) -> None:
    log = make_log(tmp_path, datetime(2026, 11, 11, 12, 0, 0))

    with pytest.raises(ValueError, match="timezone"):
        log.append("s", "a", "r")


def test_concurrent_appends_write_whole_lines(tmp_path: Path) -> None:
    log = make_log(tmp_path)

    def worker(number: int) -> None:
        for _ in range(50):
            log.append("probe", f"thread {number}", "ok")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 300
    assert all(line.count(" | ") == 4 for line in lines)
