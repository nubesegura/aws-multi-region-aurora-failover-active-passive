import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dr_drill import github
from dr_drill.github import Github, GithubError

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
FENCE = {"action": "fence", "region": "us-east-2", "confirm": "us-east-2"}
TO_SECONDARY = {"direction": "to-secondary", "confirm": "failover"}
TO_PRIMARY = {"direction": "to-primary", "confirm": "failover"}


class FakeGh:
    """Scripted `gh`: answers keyed by the sub-command; every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.answers: dict[str, list[tuple[int, str]]] = {}

    def add(self, key: str, code: int, output: str) -> None:
        self.answers.setdefault(key, []).append((code, output))

    def __call__(self, command: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append((list(command), timeout))
        key = " ".join(command[1:3])
        queue = self.answers.get(key, [(0, "")])
        return queue.pop(0) if len(queue) > 1 else queue[0]


def make(gh: FakeGh, sleeps: list[float] | None = None) -> Github:
    pauses = sleeps if sleeps is not None else []
    return Github("example/repo", gh, clock=lambda: NOW, sleep=pauses.append)


def test_dispatch_builds_the_argument_list_and_returns_the_time() -> None:
    gh = FakeGh()

    assert make(gh).dispatch("simulate-outage", FENCE) == NOW

    assert gh.calls[0][0] == [
        "gh",
        "workflow",
        "run",
        "simulate-outage.yml",
        "--repo",
        "example/repo",
        "--ref",
        "main",
        "-f",
        "action=fence",
        "-f",
        "region=us-east-2",
        "-f",
        "confirm=us-east-2",
    ]


@pytest.mark.parametrize("inputs", [TO_SECONDARY, TO_PRIMARY])
def test_failover_inputs_are_allowed(inputs: dict[str, str]) -> None:
    gh = FakeGh()

    make(gh).dispatch("failover", inputs)

    assert gh.calls[0][0][-4:] == [
        "-f",
        f"direction={inputs['direction']}",
        "-f",
        "confirm=failover",
    ]


@pytest.mark.parametrize(
    ("workflow", "inputs"),
    [
        ("deploy", {}),
        ("destroy", {"confirm": "destroy"}),
        ("failover", {"direction": "to-secondary", "confirm": "failover", "force": "true"}),
        ("failover", {"direction": "to-secondary", "confirm": "no"}),
        ("failover", {"direction": "sideways", "confirm": "failover"}),
        ("simulate-outage", {"action": "unfence", "region": "us-east-2", "confirm": "us-east-2"}),
        ("simulate-outage", {"action": "fence", "region": "us-west-2", "confirm": "us-west-2"}),
    ],
)
def test_non_allow_listed_dispatches_raise_and_run_nothing(
    workflow: str, inputs: dict[str, str]
) -> None:
    gh = FakeGh()

    with pytest.raises(GithubError, match="allow-list"):
        make(gh).dispatch(workflow, inputs)

    assert gh.calls == []


def test_a_failing_dispatch_raises_without_the_output() -> None:
    gh = FakeGh()
    gh.add("workflow run", 1, "secret-looking-output")

    with pytest.raises(GithubError) as raised:
        make(gh).dispatch("failover", TO_SECONDARY)

    assert "secret-looking-output" not in str(raised.value)


def test_the_run_id_comes_from_the_printed_url() -> None:
    gh = FakeGh()
    gh.add("workflow run", 0, "https://github.com/example/repo/actions/runs/987654321\n")
    client = make(gh)
    dispatched = client.dispatch("failover", TO_SECONDARY)

    assert client.find_run("failover", dispatched) == 987654321
    assert len(gh.calls) == 1


def test_without_a_printed_url_the_first_run_created_after_the_dispatch_is_used() -> None:
    gh = FakeGh()
    old = (NOW - timedelta(minutes=30)).isoformat()
    first = (NOW + timedelta(seconds=2)).isoformat()
    second = (NOW + timedelta(seconds=9)).isoformat()
    runs = [
        {"databaseId": 1, "createdAt": old, "status": "completed"},
        {"databaseId": 3, "createdAt": second, "status": "queued"},
        {"databaseId": 2, "createdAt": first, "status": "queued"},
    ]
    gh.add("run list", 0, json.dumps(runs))
    client = make(gh)
    dispatched = client.dispatch("failover", TO_SECONDARY)

    assert client.find_run("failover", dispatched) == 2
    command = gh.calls[-1][0]
    assert command[:3] == ["gh", "run", "list"]
    assert "--event" in command
    assert command[command.index("--json") + 1] == "databaseId,createdAt,status"


def test_find_run_retries_a_few_times_with_a_pause_then_gives_up() -> None:
    gh = FakeGh()
    gh.add("run list", 0, "[]")
    pauses: list[float] = []

    with pytest.raises(GithubError, match="no run of failover"):
        make(gh, pauses).find_run("failover", NOW)

    assert len(pauses) == github.FIND_ATTEMPTS - 1


def test_find_run_finds_a_run_that_appears_late() -> None:
    gh = FakeGh()
    late = [{"databaseId": 7, "createdAt": (NOW + timedelta(seconds=4)).isoformat()}]
    gh.add("run list", 0, "[]")
    gh.add("run list", 0, json.dumps(late))
    pauses: list[float] = []

    assert make(gh, pauses).find_run("failover", NOW) == 7
    assert len(pauses) == 1


@pytest.mark.parametrize(("code", "output"), [(1, ""), (0, "not json"), (0, "{}")])
def test_find_run_rejects_bad_gh_answers(code: int, output: str) -> None:
    gh = FakeGh()
    gh.add("run list", code, output)

    with pytest.raises(GithubError):
        make(gh).find_run("failover", NOW)


def test_wait_is_one_blocking_watch_with_exit_status() -> None:
    gh = FakeGh()
    gh.add("run watch", 0, "")

    assert make(gh).wait(55) is True

    assert len(gh.calls) == 1
    command, timeout = gh.calls[0]
    assert command == [
        "gh",
        "run",
        "watch",
        "55",
        "--repo",
        "example/repo",
        "--exit-status",
        "--interval",
        "30",
    ]
    assert timeout == github.WAIT_TIMEOUT_SECONDS


def test_wait_reports_a_failed_run() -> None:
    gh = FakeGh()
    gh.add("run watch", 1, "")

    assert make(gh).wait(55) is False


def _jobs_payload() -> dict[str, Any]:
    return {
        "jobs": [
            {
                "name": "failover prod",
                "conclusion": "success",
                "started_at": "2026-11-11T12:00:10Z",
                "completed_at": "2026-11-11T12:08:00Z",
                "steps": [
                    {
                        "name": "Group 2 - planned switchover to the secondary region",
                        "conclusion": "success",
                        "started_at": "2026-11-11T12:02:00Z",
                        "completed_at": "2026-11-11T12:05:30Z",
                    },
                    {"name": "Skipped step", "conclusion": "skipped", "started_at": None},
                ],
            }
        ]
    }


def test_details_parses_the_run_the_jobs_and_the_steps() -> None:
    gh = FakeGh()
    run = {
        "status": "completed",
        "conclusion": "success",
        "createdAt": "2026-11-11T12:00:01Z",
        "startedAt": "2026-11-11T12:00:08Z",
        "updatedAt": "2026-11-11T12:08:01Z",
    }
    gh.add("run view", 0, json.dumps(run))
    gh.add(
        "api repos/example/repo/actions/runs/55/jobs?per_page=100", 0, json.dumps(_jobs_payload())
    )

    details = make(gh).details(55)

    assert details.conclusion == "success"
    assert details.created == datetime(2026, 11, 11, 12, 0, 1, tzinfo=UTC)
    assert details.started == datetime(2026, 11, 11, 12, 0, 8, tzinfo=UTC)
    assert details.updated == datetime(2026, 11, 11, 12, 8, 1, tzinfo=UTC)
    job = details.jobs[0]
    assert job.name == "failover prod"
    assert job.steps[0].name.startswith("Group 2")
    assert job.steps[0].started == datetime(2026, 11, 11, 12, 2, 0, tzinfo=UTC)
    assert job.steps[0].completed == datetime(2026, 11, 11, 12, 5, 30, tzinfo=UTC)
    assert job.steps[1].started is None
    assert gh.calls[1][0] == ["gh", "api", "repos/example/repo/actions/runs/55/jobs?per_page=100"]


def test_details_tolerates_missing_jobs_and_odd_times() -> None:
    gh = FakeGh()
    gh.add("run view", 0, json.dumps({"conclusion": None, "createdAt": "garbage"}))
    gh.add("api repos/example/repo/actions/runs/1/jobs?per_page=100", 0, "{}")

    details = make(gh).details(1)

    assert details.conclusion is None
    assert details.created is None
    assert details.jobs == ()


@pytest.mark.parametrize(("code", "output"), [(1, "{}"), (0, "nope"), (0, "[]")])
def test_details_rejects_bad_gh_answers(code: int, output: str) -> None:
    gh = FakeGh()
    gh.add("run view", code, output)

    with pytest.raises(GithubError):
        make(gh).details(1)


def test_a_naive_time_is_taken_as_utc() -> None:
    assert github._time("2026-11-11T12:00:00") == NOW


def test_subprocess_gh_runner_uses_an_argument_list_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = "out"

    def fake_run(command: list[str], **kwargs: Any) -> Completed:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert github.subprocess_gh_runner(("gh", "auth", "status"), 5.0) == (0, "out")
    assert seen["command"] == ["gh", "auth", "status"]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["timeout"] == 5.0


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (subprocess.TimeoutExpired("gh", 1), github.EXIT_TIMEOUT),
        (FileNotFoundError("gh"), github.EXIT_NOT_RUNNABLE),
    ],
)
def test_subprocess_gh_runner_turns_timeouts_and_missing_gh_into_exit_codes(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: int
) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert github.subprocess_gh_runner(("gh",), 1.0) == (code, "")


def test_the_adapter_has_no_force_flag_and_no_other_verb() -> None:
    source = Path(github.__file__).read_text(encoding="utf-8")

    for forbidden in ("--force", '"cancel"', '"rerun"', '"delete"', '"disable"'):
        assert forbidden not in source


def test_only_the_dispatch_can_write_and_only_with_the_three_allowed_combinations() -> None:
    source = Path(github.__file__).read_text(encoding="utf-8")

    assert source.count('"workflow"') == 1
    assert source.count('"-f"') == 1
    assert source.count("--method") == 0
    allowed = github._allowed_inputs("us-east-2")
    combinations = [(name, inputs) for name, options in allowed.items() for inputs in options]
    assert len(combinations) == 3


STEP = "Group 3 - validate the service on the custom domain"


def _log_line(job: str, step: str, text: str) -> str:
    return f"{job}\t{step}\t2026-11-11T12:08:00.1234567Z {text}"


def test_step_log_is_a_read_only_run_view_and_keeps_only_the_named_step() -> None:
    gh = FakeGh()
    output = "\n".join(
        [
            _log_line("prod", "Group 2 - switchover", "other step line"),
            _log_line("prod", STEP, "step 3/3: probe movement (deposit of 0.01)"),
            _log_line("prod", STEP, "  skipped (no COGNITO_ID_TOKEN provided)"),
            "a line without tabs",
        ]
    )
    gh.add("run view", 0, output)

    text = make(gh).step_log(55, STEP)

    assert gh.calls[0][0] == ["gh", "run", "view", "55", "--repo", "example/repo", "--log"]
    assert text.count("\n") == 1
    assert "probe movement" in text
    assert "other step line" not in text


def test_step_log_handles_crlf_and_bom_and_judges_only_the_lines_from_the_header() -> None:
    gh = FakeGh()
    header = "step 3/3: probe movement (deposit of 0.01)"
    lines = [
        _log_line("prod", STEP, "step 2/3: arn:aws:iam::123456789012:role/x"),
        _log_line("prod", STEP, header),
        _log_line("prod", STEP, "﻿  skipped (no COGNITO_ID_TOKEN provided)"),
    ]
    gh.add("run view", 0, "\r\n".join(lines) + "\r\n")

    text = make(gh).step_log(55, STEP)

    assert "\r" not in text
    assert text.splitlines()[0].endswith(header)
    assert "skipped (no COGNITO_ID_TOKEN" in text
    assert "123456789012" not in text


def test_step_log_without_the_probe_header_is_empty() -> None:
    gh = FakeGh()
    gh.add("run view", 0, _log_line("prod", STEP, "step 2/3: deep health"))

    assert make(gh).step_log(55, STEP) == ""


def test_step_log_of_an_unknown_step_is_empty() -> None:
    gh = FakeGh()
    gh.add("run view", 0, _log_line("prod", "Other", "text"))

    assert make(gh).step_log(55, STEP) == ""


def test_step_log_failure_raises_without_the_output() -> None:
    gh = FakeGh()
    gh.add("run view", 1, "secret-looking-output")

    with pytest.raises(GithubError) as raised:
        make(gh).step_log(55, STEP)

    assert "secret-looking-output" not in str(raised.value)


def test_step_log_is_refused_by_the_output_guard_before_it_is_returned() -> None:
    gh = FakeGh()
    gh.add(
        "run view",
        0,
        "\n".join(
            [
                _log_line("prod", STEP, "step 3/3: probe movement (deposit of 0.01)"),
                _log_line("prod", STEP, "arn:aws:iam::123456789012:role/x"),
            ]
        ),
    )

    with pytest.raises(GithubError, match="output guard") as raised:
        make(gh).step_log(55, STEP)

    assert "123456789012" not in str(raised.value)
