"""Fakes for the probe tests: a fake clock that also sleeps, and a scripted API."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from dr_drill.api import FieldValue, Sample
from dr_drill.probe import Observation, ProbeClock

START = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
MAX_FAKE_SLEEPS = 50_000


class FakeTime:
    """Monotonic seconds and UTC time driven by `sleep`; the stop event is set at `stop_after`."""

    def __init__(self, stop_after: float | None = None) -> None:
        self.now = 0.0
        self.stop = threading.Event()
        self.stop_after = stop_after
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def utc(self) -> datetime:
        return START + timedelta(seconds=self.now)

    def sleep(self, seconds: float) -> None:
        assert len(self.sleeps) < MAX_FAKE_SLEEPS, "a fake clock loop did not stop"
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)
        if self.stop_after is not None and self.now >= self.stop_after:
            self.stop.set()

    def clock(self) -> ProbeClock:
        return ProbeClock(self.monotonic, self.utc, self.sleep)


class ScriptedApi:
    """Answers from a script of statuses (the last one repeats); every call lasts `latency` s."""

    def __init__(
        self,
        time: FakeTime,
        script: list[int] | None = None,
        latency: float = 0.05,
        region: str = "us-east-2",
        role: str = "writer",
    ) -> None:
        self._time = time
        self._script = script or [201]
        self._latency = latency
        self._region = region
        self._role = role
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.starts: list[float] = []
        self.balance = 10_000

    def _sample(self, status: int, fields: dict[str, FieldValue] | None = None) -> Sample:
        started = self._time.utc()
        self._time.advance(self._latency)
        ok = 200 <= status < 300
        return Sample(
            start_utc=started,
            end_utc=self._time.utc(),
            status=status,
            latency_ms=self._latency * 1000.0,
            served_by_region=self._region if status else None,
            processed_region=self._region if ok else None,
            database_role=self._role if status else None,
            body_fields=fields or {},
        )

    def _next_status(self) -> int:
        return self._script.pop(0) if len(self._script) > 1 else self._script[0]

    def post_transaction(
        self, token: str, kind: str, amount: Decimal, idempotency_key: str
    ) -> Sample:
        self.calls.append(("post", (token, kind, amount, idempotency_key)))
        self.starts.append(self._time.now)
        status = self._next_status()
        if 200 <= status < 300:
            self.balance += 1 if kind == "deposit" else -1
            return self._sample(status, {"balance_cents": self.balance})
        return self._sample(status)

    def get_account(self, token: str) -> Sample:
        self.calls.append(("account", (token,)))
        status = self._next_status()
        return self._sample(status, {"balance_cents": 10_000, "movement_count": 3})

    def get_health(self, deep: bool = False) -> Sample:
        self.calls.append(("health", (deep,)))
        return self._sample(200)


def counter_keys() -> Callable[[], str]:
    state = {"n": 0}

    def new_key() -> str:
        state["n"] += 1
        return f"key-{state['n']}"

    return new_key


class ListSink:
    def __init__(self) -> None:
        self.items: list[Observation] = []

    def __call__(self, observation: Observation) -> None:
        self.items.append(observation)
