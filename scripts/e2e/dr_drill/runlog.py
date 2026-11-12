"""Run log: one line per action (`UTC time | step | action | result | region`) through the guard."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from dr_drill.redact import OutputGuard, RefusedOutput

__all__ = ["RefusedOutput", "RunLog"]

SEPARATOR = " | "
EMPTY_FIELD = "-"


def _clean(value: str) -> str:
    """One-line field: no newlines, no separator, "-" when empty."""
    text = " ".join(value.split()).replace("|", "/")
    return text or EMPTY_FIELD


def _timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("the clock must return a timezone-aware datetime")
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class RunLog:
    """Append-only log file. A refused line is not written."""

    def __init__(self, path: Path, guard: OutputGuard, clock: Callable[[], datetime]) -> None:
        self._path = path
        self._guard = guard
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """File the lines are appended to."""
        return self._path

    def append(self, step: str, action: str, result: str, region: str = "") -> str:
        """Write one line and return it (without the line feed) for the console.

        Raises RefusedOutput, naming the field and the reason, when a field fails the output guard.
        """
        fields = {"step": step, "action": action, "result": result, "region": region}
        for name, value in fields.items():
            self._guard.ensure(name, value)
        with self._lock:
            stamp = _timestamp(self._clock())
            line = SEPARATOR.join([stamp, *(_clean(value) for value in fields.values())])
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("ab") as handle:
                handle.write((line + "\n").encode("utf-8"))
        return line
