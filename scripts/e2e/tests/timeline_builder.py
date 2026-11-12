"""Builder of synthetic timelines for the metrics and report tests (no real data)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from dr_drill.probe import Observation, format_utc
from dr_drill.redact import OutputGuard
from dr_drill.timeline import Timeline

T0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
PRIMARY = "us-east-2"
SECONDARY = "us-west-2"


def at(seconds: float) -> datetime:
    """UTC time `seconds` after the synthetic start of the run."""
    return T0 + timedelta(seconds=seconds)


class TimelineBuilder:
    """Adds typed events at offsets (seconds) from `T0`."""

    def __init__(self, run_id: str = "2026-11-11T12-00-00Z") -> None:
        self.timeline = Timeline(run_id, OutputGuard(), lambda: T0)
        self._counter = 0

    def phase(self, name: str, start: float, end: float | None = None) -> None:
        self.timeline.add("phase", {"name": name, "state": "start"}, at(start))
        if end is not None:
            self.timeline.add("phase", {"name": name, "state": "end"}, at(end))

    def request(
        self,
        probe: str,
        start: float,
        ok: bool = True,
        region: str | None = PRIMARY,
        attempt: int = 1,
        latency_ms: float = 40.0,
        label: str | None = None,
        status: int | None = None,
    ) -> None:
        self._counter += 1
        status = status or (201 if ok and probe == "write" else 200 if ok else 503)
        observation = Observation(
            probe=probe,
            label=label or f"{probe[0]}{self._counter:04d}",
            attempt=attempt,
            start_utc=at(start),
            end_utc=at(start) + timedelta(milliseconds=latency_ms),
            ok=ok,
            status=status,
            latency_ms=latency_ms,
            served_by_region=region if ok else None,
            database_role="writer" if ok and probe == "write" else None,
        )
        self.timeline.add("request", observation.to_data(), observation.start_utc)

    def dispatch(self, phase: str, workflow: str, at_seconds: float) -> None:
        data = {"phase": phase, "workflow": workflow, "event": "dispatch", "inputs": {}}
        self.timeline.add("workflow_step", data, at(at_seconds))

    def run(
        self, phase: str, workflow: str, created: float, started: float, updated: float
    ) -> None:
        data = {
            "phase": phase,
            "workflow": workflow,
            "event": "run",
            "run_id": 1,
            "status": "completed",
            "conclusion": "success",
            "created": format_utc(at(created)),
            "started": format_utc(at(started)),
            "updated": format_utc(at(updated)),
        }
        self.timeline.add("workflow_step", data, at(created))

    def step(
        self,
        phase: str,
        workflow: str,
        name: str,
        start: float | None,
        end: float | None,
        conclusion: str = "success",
    ) -> None:
        data: dict[str, Any] = {
            "phase": phase,
            "workflow": workflow,
            "event": "step",
            "job": "job",
            "step": name,
            "conclusion": conclusion,
            "start": None if start is None else format_utc(at(start)),
            "end": None if end is None else format_utc(at(end)),
        }
        self.timeline.add("workflow_step", data, at(start if start is not None else 0))

    def dns(self, seconds: float, answer: list[str], first: bool = False) -> None:
        data = {
            "first": first,
            "answer": answer,
            "previous": None,
            "observed": format_utc(at(seconds)),
        }
        self.timeline.add("dns", data, at(seconds))

    def lag(
        self, seconds: float, milliseconds: float, metric: str = "AuroraGlobalDBReplicationLag"
    ) -> None:
        data = {"metric": metric, "region": SECONDARY, "milliseconds": milliseconds}
        self.timeline.add("metric_sample", data, at(seconds))
