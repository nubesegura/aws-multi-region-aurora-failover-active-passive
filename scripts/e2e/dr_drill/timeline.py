"""Timeline of a run: typed UTC millisecond events, and the orchestration of the probes."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from dr_drill.probe import Observation, Probe, format_utc, parse_utc
from dr_drill.redact import OutputGuard, RefusedOutput

SCHEMA_VERSION = 1
EVENT_KINDS = frozenset(
    {"request", "workflow_step", "phase", "dns", "metric_sample", "state_snapshot", "note"}
)
STOP_JOIN_SECONDS = 15.0


@dataclass(frozen=True)
class Event:
    """One timeline entry: a kind, a UTC time and a JSON-ready payload."""

    kind: str
    time_utc: datetime
    data: dict[str, Any]


class Timeline:
    """Thread-safe, append-only collection of events; every event passes the output guard."""

    def __init__(self, run_id: str, guard: OutputGuard, clock: Callable[[], datetime]) -> None:
        self._run_id = run_id
        self._guard = guard
        self._clock = clock
        self._lock = threading.Lock()
        self._events: list[Event] = []

    @property
    def run_id(self) -> str:
        """Identifier of the run (its folder name)."""
        return self._run_id

    def add(self, kind: str, data: dict[str, Any], time_utc: datetime | None = None) -> None:
        """Append an event. Raises ValueError for an unknown kind and RefusedOutput when unsafe."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        moment = time_utc or self._clock()
        self._guard.ensure(f"{kind} event", json.dumps(data, sort_keys=True))
        with self._lock:
            self._events.append(Event(kind, moment, data))

    def note(self, text: str) -> None:
        """Append a free-text note."""
        self.add("note", {"text": text})

    def record_observation(self, observation: Observation) -> None:
        """Sink of the probes: a refused record becomes a note that names no value."""
        data = observation.to_data()
        try:
            self.add("request", data, observation.start_utc)
        except RefusedOutput as refused:
            fields = ", ".join(self._refused_fields(data)) or "combined fields"
            self.note(
                f"request {observation.probe} {observation.label} refused: {refused.reason}"
                f" (field: {fields})"
            )

    def _refused_fields(self, data: dict[str, Any]) -> list[str]:
        """Names (never values) of the payload fields the guard refuses on their own."""
        return [
            name
            for name, value in data.items()
            if self._guard.reason(json.dumps(value)) is not None
        ]

    def events(self, kind: str | None = None) -> tuple[Event, ...]:
        """Events in time order (ties keep the order of arrival), optionally of one kind."""
        with self._lock:
            snapshot = list(self._events)
        ordered = sorted(snapshot, key=lambda event: event.time_utc)
        return tuple(event for event in ordered if kind is None or event.kind == kind)

    def observations(self) -> tuple[Observation, ...]:
        """The `request` events decoded as probe observations."""
        return tuple(Observation.from_data(event.data) for event in self.events("request"))

    def to_json(self) -> str:
        """Stable JSON document (schema version, run id, events); refused when it is unsafe."""
        document = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self._run_id,
            "events": [
                {"kind": event.kind, "time": format_utc(event.time_utc), "data": event.data}
                for event in self.events()
            ],
        }
        text = json.dumps(document, indent=2, sort_keys=True)
        self._guard.ensure("timeline", text)
        return text

    @classmethod
    def from_json(
        cls, text: str, guard: OutputGuard, clock: Callable[[], datetime] | None = None
    ) -> Timeline:
        """Load a document written by `to_json`; raises ValueError on another schema version."""
        document = json.loads(text)
        if document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported timeline schema version")
        timeline = cls(str(document["run_id"]), guard, clock or (lambda: datetime.now(UTC)))
        for item in document["events"]:
            timeline.add(item["kind"], item["data"], parse_utc(item["time"]))
        return timeline


class ProbeSet:
    """Starts and stops the probes together; a crashed probe is noted on the timeline."""

    def __init__(self, probes: Sequence[Probe], stop: threading.Event, timeline: Timeline) -> None:
        self._probes = tuple(probes)
        self._stop = stop
        self._timeline = timeline
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Start one thread per probe."""
        self._timeline.add("phase", {"name": "probes", "state": "started"})
        for probe in self._probes:
            thread = threading.Thread(
                target=self._run, args=(probe,), name=f"probe-{probe.name}", daemon=True
            )
            self._threads.append(thread)
            thread.start()

    def stop(self, join_seconds: float = STOP_JOIN_SECONDS) -> bool:
        """Set the stop event and wait for the threads; False when one did not end in time."""
        self._stop.set()
        for thread in self._threads:
            thread.join(join_seconds)
        stopped = not any(thread.is_alive() for thread in self._threads)
        self._timeline.add("phase", {"name": "probes", "state": "stopped" if stopped else "stuck"})
        return stopped

    def _run(self, probe: Probe) -> None:
        try:
            probe.run()
        except Exception as error:
            self._timeline.note(f"probe {probe.name} crashed: {type(error).__name__}")
