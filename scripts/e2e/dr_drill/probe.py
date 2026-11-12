"""Continuous probes: write (net-zero movements), read and health, and secondary authentication.

The probes hold no I/O of their own: the API, the token source, the clock, the sleep and the sink
are injected, so the tests run on a fake clock. Records never carry a token.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from dr_drill.api import Sample
from dr_drill.auth import AuthError, AuthProbeResult

DEFAULT_RATE_SECONDS = 1.0
DEFAULT_MAX_OUTAGE_SECONDS = 600.0
READ_PERIOD_SECONDS = 5.0
AUTH_PERIOD_SECONDS = 10.0
MIN_ATTEMPT_INTERVAL_SECONDS = 0.5
MOVEMENT_AMOUNT = Decimal("0.01")
MOVEMENT_KINDS = ("deposit", "withdrawal")

STATUS_TRANSPORT = 0
HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300
HTTP_UNAUTHORIZED = 401
HTTP_REQUEST_TIMEOUT = 408
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500
MAX_UNAUTHORIZED_ANSWERS = 2
ABANDON_PERMANENT = "permanent"
ABANDON_MAX_OUTAGE = "max_outage"

PROBE_WRITE = "write"
PROBE_ACCOUNT = "account"
PROBE_HEALTH = "health"
PROBE_AUTH = "auth"

LATENCY_DECIMALS = 3
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def round_ms(value: float) -> float:
    """A millisecond figure rounded for serialization, so no float carries a long fraction."""
    return round(value, LATENCY_DECIMALS)


def format_utc(moment: datetime) -> str:
    """UTC ISO time with milliseconds, e.g. 2026-11-11T12:00:00.123Z."""
    utc = moment.astimezone(UTC)
    return utc.strftime(TIME_FORMAT) + f".{utc.microsecond // 1000:03d}Z"


def parse_utc(text: str) -> datetime:
    """Inverse of `format_utc`."""
    return datetime.fromisoformat(text).astimezone(UTC)


@dataclass(frozen=True)
class Observation:
    """One attempt of one probe. Carries no token and no header."""

    probe: str
    label: str
    attempt: int
    start_utc: datetime
    end_utc: datetime
    ok: bool
    status: int
    latency_ms: float
    key: str | None = None
    kind: str | None = None
    served_by_region: str | None = None
    processed_region: str | None = None
    database_role: str | None = None
    balance_cents: int | None = None
    error: str | None = None

    @classmethod
    def from_sample(
        cls,
        probe: str,
        label: str,
        attempt: int,
        sample: Sample,
        key: str | None = None,
        kind: str | None = None,
    ) -> Observation:
        """Record of an HTTP answer."""
        balance = sample.body_fields.get("balance_cents")
        error = sample.body_fields.get("error")
        return cls(
            probe=probe,
            label=label,
            attempt=attempt,
            start_utc=sample.start_utc,
            end_utc=sample.end_utc,
            ok=sample.ok,
            status=sample.status,
            latency_ms=sample.latency_ms,
            key=key,
            kind=kind,
            served_by_region=sample.served_by_region,
            processed_region=sample.processed_region,
            database_role=sample.database_role,
            balance_cents=balance if isinstance(balance, int) else None,
            error=error if isinstance(error, str) else None,
        )

    def to_data(self) -> dict[str, Any]:
        """JSON-ready dictionary (times with milliseconds)."""
        return {
            "probe": self.probe,
            "label": self.label,
            "attempt": self.attempt,
            "start": format_utc(self.start_utc),
            "end": format_utc(self.end_utc),
            "ok": self.ok,
            "status": self.status,
            "latency_ms": round_ms(self.latency_ms),
            "key": self.key,
            "kind": self.kind,
            "served_by_region": self.served_by_region,
            "processed_region": self.processed_region,
            "database_role": self.database_role,
            "balance_cents": self.balance_cents,
            "error": self.error,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Observation:
        """Inverse of `to_data`."""
        return cls(
            probe=data["probe"],
            label=data["label"],
            attempt=data["attempt"],
            start_utc=parse_utc(data["start"]),
            end_utc=parse_utc(data["end"]),
            ok=data["ok"],
            status=data["status"],
            latency_ms=data["latency_ms"],
            key=data.get("key"),
            kind=data.get("kind"),
            served_by_region=data.get("served_by_region"),
            processed_region=data.get("processed_region"),
            database_role=data.get("database_role"),
            balance_cents=data.get("balance_cents"),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class Acknowledged:
    """A movement that got a 2xx answer (a `200` replay counts once)."""

    label: str
    key: str
    kind: str
    balance_cents: int | None
    status: int


@dataclass(frozen=True)
class Abandoned:
    """A movement given up: a permanent client error or the maximum outage.

    The ledger may or may not hold it (a timeout or a 5xx can hide a committed write), so
    task 3.5 treats it as part of the uncertainty band, never as an acknowledged write.
    """

    label: str
    key: str
    kind: str
    attempts: int
    status: int = 0
    reason: str = ABANDON_MAX_OUTAGE


@dataclass(frozen=True)
class Pending:
    """The movement that was in flight (not acknowledged, not abandoned) when the probe stopped."""

    label: str
    key: str
    kind: str
    attempts: int


@dataclass(frozen=True)
class ProbeClock:
    """Injected time: monotonic seconds, UTC now and a sleep (`stop.wait` in real runs)."""

    monotonic: Callable[[], float]
    utc_now: Callable[[], datetime]
    sleep: Callable[[float], object]


Sink = Callable[[Observation], None]
TokenSource = Callable[[], str]


class Probe(Protocol):
    """What `ProbeSet` runs: a named loop that ends when its stop event is set."""

    @property
    def name(self) -> str:
        """Short name used in notes."""
        ...

    def run(self) -> None:
        """Probe until the stop event is set."""
        ...


class WriteApi(Protocol):
    """Writes the probe needs."""

    def post_transaction(
        self, token: str, kind: str, amount: Decimal, idempotency_key: str
    ) -> Sample:
        """POST /transactions."""
        ...


class ReadApi(Protocol):
    """Reads the probe needs."""

    def get_account(self, token: str) -> Sample:
        """GET /account."""
        ...

    def get_health(self, deep: bool = False) -> Sample:
        """GET /health."""
        ...


def is_permanent_error(status: int, unauthorized_answers: int) -> bool:
    """True when retrying the same movement cannot help.

    Retried: status 0 (transport), 408, 429, 5xx, a 2xx without a usable body, and a first 401
    (the token may need a refresh). Everything else (400, 403, 404, 409, 422, a second 401, any
    other 3xx or 4xx) is deterministic and not an outage.
    """
    if status == HTTP_UNAUTHORIZED:
        return unauthorized_answers >= MAX_UNAUTHORIZED_ANSWERS
    transient = (
        status in (STATUS_TRANSPORT, HTTP_REQUEST_TIMEOUT, HTTP_TOO_MANY_REQUESTS)
        or status >= HTTP_SERVER_ERROR
        or HTTP_OK <= status < HTTP_MULTIPLE_CHOICES
    )
    return not transient


def _failed_sign_in(
    probe: str, label: str, attempt: int, clock: ProbeClock, key: str | None, kind: str | None
) -> Observation:
    moment = clock.utc_now()
    return Observation(
        probe, label, attempt, moment, moment, False, 0, 0.0, key=key, kind=kind, error="AuthError"
    )


class WriteProbe:
    """Net-zero movements: deposit 0.01, withdrawal 0.01, ... one at a time, retried by key."""

    name = PROBE_WRITE

    def __init__(
        self,
        api: WriteApi,
        token: TokenSource,
        clock: ProbeClock,
        sink: Sink,
        stop: threading.Event,
        rate_seconds: float = DEFAULT_RATE_SECONDS,
        max_outage_seconds: float = DEFAULT_MAX_OUTAGE_SECONDS,
        new_key: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._api = api
        self._token = token
        self._clock = clock
        self._sink = sink
        self._stop = stop
        self._interval = max(rate_seconds, MIN_ATTEMPT_INTERVAL_SECONDS)
        self._max_outage = max_outage_seconds
        self._new_key = new_key
        self._lock = threading.Lock()
        self._acked: list[Acknowledged] = []
        self._abandoned: list[Abandoned] = []
        self._pending: Pending | None = None
        self._delayed = 0
        self._movements = 0
        self._sink_failures = 0
        self._last_attempt_start: float | None = None

    @property
    def acked(self) -> tuple[Acknowledged, ...]:
        """Movements acknowledged so far, in order."""
        with self._lock:
            return tuple(self._acked)

    @property
    def abandoned(self) -> tuple[Abandoned, ...]:
        """Movements given up (permanent error or maximum outage), in order."""
        with self._lock:
            return tuple(self._abandoned)

    @property
    def pending(self) -> tuple[Pending, ...]:
        """The movement in flight when the probe stopped (empty when none)."""
        with self._lock:
            return (self._pending,) if self._pending else ()

    @property
    def probe_errors(self) -> int:
        """Movements abandoned because of a permanent client error (not an outage)."""
        with self._lock:
            return sum(1 for item in self._abandoned if item.reason == ABANDON_PERMANENT)

    @property
    def delayed_movements(self) -> int:
        """Movements that were still pending when the next one was due."""
        with self._lock:
            return self._delayed

    @property
    def sink_failures(self) -> int:
        """Observations the sink failed to take; the probe kept running and kept its accounting."""
        with self._lock:
            return self._sink_failures

    def run(self) -> None:
        """Send movements until the stop event is set."""
        scheduled = self._clock.monotonic()
        while not self._stop.is_set():
            self._wait_for_slot(scheduled)
            if self._stop.is_set():
                break
            first_start = self._clock.monotonic()
            self._deliver()
            scheduled = first_start + self._interval
            now = self._clock.monotonic()
            if now > scheduled:
                with self._lock:
                    self._delayed += 1
                scheduled = now

    def _wait_for_slot(self, earliest: float) -> None:
        """Sleep until `earliest` and at least one interval after the last attempt began."""
        with self._lock:
            last = self._last_attempt_start
        if last is not None:
            earliest = max(earliest, last + self._interval)
        pause = earliest - self._clock.monotonic()
        if pause > 0:
            self._clock.sleep(pause)

    def _deliver(self) -> None:
        with self._lock:
            self._movements += 1
            number = self._movements
        label = f"w{number:04d}"
        kind = MOVEMENT_KINDS[(number - 1) % len(MOVEMENT_KINDS)]
        key = self._new_key()
        first_start = self._clock.monotonic()
        attempt = 0
        unauthorized = 0
        while not self._stop.is_set():
            attempt += 1
            if attempt > 1:
                self._wait_for_slot(self._clock.monotonic())
                if self._stop.is_set():
                    return
            with self._lock:
                self._pending = Pending(label, key, kind, attempt)
            observation = self._attempt(label, attempt, kind, key)
            unauthorized += observation.status == HTTP_UNAUTHORIZED
            if self._settle(observation, label, key, kind, attempt, unauthorized, first_start):
                return

    def _settle(
        self,
        observation: Observation,
        label: str,
        key: str,
        kind: str,
        attempt: int,
        unauthorized: int,
        first_start: float,
    ) -> bool:
        """Account for the attempt, then hand it to the sink; True when the movement is closed."""
        status = observation.status
        closed = True
        if observation.ok:
            self._close(Acknowledged(label, key, kind, observation.balance_cents, status))
        elif is_permanent_error(status, unauthorized):
            self._close(Abandoned(label, key, kind, attempt, status, ABANDON_PERMANENT))
        elif self._clock.monotonic() - first_start >= self._max_outage:
            self._close(Abandoned(label, key, kind, attempt, status, ABANDON_MAX_OUTAGE))
        else:
            closed = False
        self._record(observation)
        return closed

    def _close(self, outcome: Acknowledged | Abandoned) -> None:
        with self._lock:
            if isinstance(outcome, Acknowledged):
                self._acked.append(outcome)
            else:
                self._abandoned.append(outcome)
            self._pending = None

    def _record(self, observation: Observation) -> None:
        """Send the observation to the sink; a sink failure must not stop the probe."""
        try:
            self._sink(observation)
        except Exception:
            with self._lock:
                self._sink_failures += 1

    def _attempt(self, label: str, attempt: int, kind: str, key: str) -> Observation:
        with self._lock:
            self._last_attempt_start = self._clock.monotonic()
        try:
            token = self._token()
        except AuthError:
            return _failed_sign_in(PROBE_WRITE, label, attempt, self._clock, key, kind)
        sample = self._api.post_transaction(token, kind, MOVEMENT_AMOUNT, key)
        return Observation.from_sample(PROBE_WRITE, label, attempt, sample, key=key, kind=kind)


def run_periodic(
    action: Callable[[], None], period: float, clock: ProbeClock, stop: threading.Event
) -> None:
    """Call `action` every `period` seconds until stopped; a late round does not cause a burst."""
    next_at = clock.monotonic()
    while not stop.is_set():
        action()
        next_at += period
        now = clock.monotonic()
        if next_at < now:
            next_at = now
        if next_at > now:
            clock.sleep(next_at - now)


class ReadProbe:
    """Every 5 seconds: `GET /account` (token) and `GET /health?deep=true` (no token)."""

    name = "read"

    def __init__(
        self,
        api: ReadApi,
        token: TokenSource,
        clock: ProbeClock,
        sink: Sink,
        stop: threading.Event,
        period_seconds: float = READ_PERIOD_SECONDS,
    ) -> None:
        self._api = api
        self._token = token
        self._clock = clock
        self._sink = sink
        self._stop = stop
        self._period = period_seconds
        self._round = 0

    def run(self) -> None:
        """Read until the stop event is set."""
        run_periodic(self._round_once, self._period, self._clock, self._stop)

    def _round_once(self) -> None:
        self._round += 1
        number = self._round
        try:
            token = self._token()
        except AuthError:
            self._sink(_failed_sign_in(PROBE_ACCOUNT, f"a{number:04d}", 1, self._clock, None, None))
        else:
            account = self._api.get_account(token)
            self._sink(Observation.from_sample(PROBE_ACCOUNT, f"a{number:04d}", 1, account))
        health = self._api.get_health(deep=True)
        self._sink(Observation.from_sample(PROBE_HEALTH, f"h{number:04d}", 1, health))


class SecondaryAuth(Protocol):
    """Port of the authentication probe (`Session.authenticate_secondary`)."""

    def authenticate_secondary(self) -> AuthProbeResult:
        """Sign in against the replica region; the token is dropped."""
        ...


class AuthProbe:
    """Every 10 seconds: a sign-in against the replica region (success, latency, region)."""

    name = PROBE_AUTH

    def __init__(
        self,
        session: SecondaryAuth,
        clock: ProbeClock,
        sink: Sink,
        stop: threading.Event,
        period_seconds: float = AUTH_PERIOD_SECONDS,
    ) -> None:
        self._session = session
        self._clock = clock
        self._sink = sink
        self._stop = stop
        self._period = period_seconds
        self._round = 0

    def run(self) -> None:
        """Probe until the stop event is set."""
        run_periodic(self._round_once, self._period, self._clock, self._stop)

    def _round_once(self) -> None:
        self._round += 1
        started = self._clock.utc_now()
        result = self._session.authenticate_secondary()
        self._sink(
            Observation(
                probe=PROBE_AUTH,
                label=f"u{self._round:04d}",
                attempt=1,
                start_utc=started,
                end_utc=self._clock.utc_now(),
                ok=result.success,
                status=200 if result.success else 0,
                latency_ms=result.latency_ms,
                served_by_region=result.region,
                error=None if result.success else "AuthFailed",
            )
        )
