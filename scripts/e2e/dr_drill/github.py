"""GitHub CLI adapter: dispatches the three drill workflows, finds their run and reads the timings.

Everything goes through `gh` with an argument list and no shell. Only the three dispatches of the
drill can be expressed: the workflow name and its inputs are checked against an allow-list, and
no other flag (no force option, no other ref) is representable. The wait is ONE blocking
`gh run watch`; there is no polling loop in Python for it. Reading the log of a step
(`step_log`) is a read-only `gh run view --log` whose text passes the output guard.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dr_drill.redact import OutputGuard
from dr_drill.validate_log import PROBE_HEADER

GhRunner = Callable[[Sequence[str], float], tuple[int, str]]

REF = "main"
DEFAULT_FENCE_REGION = "us-east-2"
CONFIRM_FAILOVER = "failover"
COMMAND_TIMEOUT_SECONDS = 60.0
WAIT_TIMEOUT_SECONDS = 3600.0
WATCH_INTERVAL_SECONDS = "30"
FIND_ATTEMPTS = 10
FIND_PAUSE_SECONDS = 3.0
CLOCK_SKEW = timedelta(seconds=5)
LIST_LIMIT = "20"
EXIT_TIMEOUT = 124
EXIT_NOT_RUNNABLE = 127

_RUN_URL = re.compile(r"/actions/runs/(\d+)")


class GithubError(Exception):
    """A `gh` call failed or answered something unexpected. Messages carry no command output."""


@dataclass(frozen=True)
class StepTiming:
    """One step of a job, with the times GitHub recorded."""

    name: str
    conclusion: str | None
    started: datetime | None
    completed: datetime | None


@dataclass(frozen=True)
class JobTiming:
    """One job of a run and its steps."""

    name: str
    conclusion: str | None
    started: datetime | None
    completed: datetime | None
    steps: tuple[StepTiming, ...]


@dataclass(frozen=True)
class RunDetails:
    """A finished (or running) workflow run: its conclusion, its times and its jobs."""

    run_id: int
    status: str | None
    conclusion: str | None
    created: datetime | None
    started: datetime | None
    updated: datetime | None
    jobs: tuple[JobTiming, ...]


def subprocess_gh_runner(command: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run `gh` without a shell; returns the exit code and standard output.

    A timeout answers 124 and a missing executable 127, so callers see a plain failure.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argument list, no shell, fixed executable
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return EXIT_TIMEOUT, ""
    except OSError:
        return EXIT_NOT_RUNNABLE, ""
    return completed.returncode, completed.stdout


def _allowed_inputs(fence_region: str) -> dict[str, tuple[dict[str, str], ...]]:
    return {
        "simulate-outage": ({"action": "fence", "region": fence_region, "confirm": fence_region},),
        "failover": (
            {"direction": "to-secondary", "confirm": CONFIRM_FAILOVER},
            {"direction": "to-primary", "confirm": CONFIRM_FAILOVER},
        ),
    }


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except ValueError:
        raise GithubError("gh answered something that is not JSON") from None
    if not isinstance(parsed, dict):
        raise GithubError("gh answered an unexpected JSON shape")
    return parsed


def _in_step(line: str, step_name: str) -> bool:
    """`gh run view --log` prefixes every line with `job<TAB>step<TAB>`."""
    parts = line.split("\t", 2)
    return len(parts) == 3 and parts[1] == step_name


def _from_header(lines: Iterable[str]) -> list[str]:
    """The step lines from the probe header on (the only ones judged); none without a header."""
    kept = list(lines)
    for index, line in enumerate(kept):
        if PROBE_HEADER in line:
            return kept[index:]
    return []


def _step(raw: Mapping[str, Any]) -> StepTiming:
    return StepTiming(
        name=str(raw.get("name", "")),
        conclusion=_text(raw.get("conclusion")),
        started=_time(raw.get("started_at")),
        completed=_time(raw.get("completed_at")),
    )


def _job(raw: Mapping[str, Any]) -> JobTiming:
    steps = raw.get("steps") or []
    return JobTiming(
        name=str(raw.get("name", "")),
        conclusion=_text(raw.get("conclusion")),
        started=_time(raw.get("started_at")),
        completed=_time(raw.get("completed_at")),
        steps=tuple(_step(step) for step in steps if isinstance(step, dict)),
    )


class Github:
    """`gh` over an injected runner: dispatch, find the run, wait once, read the details."""

    def __init__(
        self,
        repository: str,
        runner: GhRunner = subprocess_gh_runner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], object] = time.sleep,
        fence_region: str = DEFAULT_FENCE_REGION,
        guard: OutputGuard | None = None,
    ) -> None:
        self._guard = guard or OutputGuard()
        self._repository = repository
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._allowed = _allowed_inputs(fence_region)
        self._printed: dict[str, int] = {}

    def dispatch(self, workflow: str, inputs: Mapping[str, str]) -> datetime:
        """`gh workflow run` on main with allow-listed inputs; returns the dispatch UTC time."""
        self._validate(workflow, inputs)
        command = ["gh", "workflow", "run", f"{workflow}.yml", "--repo", self._repository]
        command += ["--ref", REF]
        for key, value in inputs.items():
            command += ["-f", f"{key}={value}"]
        dispatched = self._clock()
        code, output = self._runner(command, COMMAND_TIMEOUT_SECONDS)
        if code != 0:
            raise GithubError(f"gh workflow run {workflow} failed")
        printed = _RUN_URL.search(output)
        if printed:
            self._printed[workflow] = int(printed.group(1))
        else:
            self._printed.pop(workflow, None)
        return dispatched

    def find_run(self, workflow: str, after: datetime) -> int:
        """Run id of the dispatch: from the printed URL, else the first run created after it."""
        printed = self._printed.pop(workflow, None)
        if printed is not None:
            return printed
        for attempt in range(FIND_ATTEMPTS):
            if attempt:
                self._sleep(FIND_PAUSE_SECONDS)
            found = self._listed_after(workflow, after)
            if found is not None:
                return found
        raise GithubError(f"no run of {workflow} was found after the dispatch")

    def wait(self, run_id: int) -> bool:
        """One blocking `gh run watch --exit-status`; True when the run succeeded."""
        command = ["gh", "run", "watch", str(run_id), "--repo", self._repository]
        command += ["--exit-status", "--interval", WATCH_INTERVAL_SECONDS]
        code, _ = self._runner(command, WAIT_TIMEOUT_SECONDS)
        return code == 0

    def details(self, run_id: int) -> RunDetails:
        """Conclusion and times of the run and, from the jobs API, every job and step."""
        view = ["gh", "run", "view", str(run_id), "--repo", self._repository]
        view += ["--json", "status,conclusion,createdAt,startedAt,updatedAt"]
        run = _object(self._output(view, "gh run view"))
        jobs_path = f"repos/{self._repository}/actions/runs/{run_id}/jobs?per_page=100"
        jobs = _object(self._output(["gh", "api", jobs_path], "gh api jobs"))
        raw_jobs = jobs.get("jobs")
        return RunDetails(
            run_id=run_id,
            status=_text(run.get("status")),
            conclusion=_text(run.get("conclusion")),
            created=_time(run.get("createdAt")),
            started=_time(run.get("startedAt")),
            updated=_time(run.get("updatedAt")),
            jobs=tuple(_job(job) for job in raw_jobs if isinstance(job, dict))
            if isinstance(raw_jobs, list)
            else (),
        )

    def step_log(self, run_id: int, step_name: str) -> str:
        """Read-only `gh run view --log`: only the lines of the named step, checked by the guard.

        Raises GithubError when the log cannot be read or the guard refuses it; the text is not
        kept anywhere else.
        """
        command = ["gh", "run", "view", str(run_id), "--repo", self._repository, "--log"]
        output = self._output(command, "gh run view --log")
        lines = [line.rstrip("\r") for line in output.split("\n")]
        text = "\n".join(_from_header(line for line in lines if _in_step(line, step_name)))
        reason = self._guard.reason(text)
        if reason is not None:
            raise GithubError(f"the step log was refused by the output guard: {reason}")
        return text

    def _validate(self, workflow: str, inputs: Mapping[str, str]) -> None:
        allowed = self._allowed.get(workflow)
        if allowed is None or dict(inputs) not in allowed:
            raise GithubError("this workflow dispatch is not in the allow-list of the drill")

    def _output(self, command: Sequence[str], label: str) -> str:
        code, output = self._runner(command, COMMAND_TIMEOUT_SECONDS)
        if code != 0:
            raise GithubError(f"{label} failed")
        return output

    def _listed_after(self, workflow: str, after: datetime) -> int | None:
        command = ["gh", "run", "list", "--repo", self._repository, "--workflow"]
        command += [f"{workflow}.yml", "--event", "workflow_dispatch", "--limit", LIST_LIMIT]
        command += ["--json", "databaseId,createdAt,status"]
        try:
            runs = json.loads(self._output(command, "gh run list") or "[]")
        except ValueError:
            raise GithubError("gh run list answered something that is not JSON") from None
        if not isinstance(runs, list):
            raise GithubError("gh run list answered an unexpected JSON shape")
        candidates = []
        for run in runs:
            created = _time(run.get("createdAt")) if isinstance(run, dict) else None
            identifier = run.get("databaseId") if isinstance(run, dict) else None
            if created and isinstance(identifier, int) and created >= after - CLOCK_SKEW:
                candidates.append((created, identifier))
        return min(candidates)[1] if candidates else None
