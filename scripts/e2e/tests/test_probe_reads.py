from datetime import timedelta
from itertools import pairwise

from dr_drill.auth import AuthError, AuthProbeResult
from dr_drill.probe import AuthProbe, Observation, ReadProbe, format_utc, parse_utc

from .probe_fakes import START, FakeTime, ListSink, ScriptedApi

TOKEN = "fake-id-token-value"


class FakeSession:
    def __init__(self, time: FakeTime, results: list[bool] | None = None) -> None:
        self._time = time
        self._results = results or [True]
        self.calls = 0

    def authenticate_secondary(self) -> AuthProbeResult:
        self.calls += 1
        success = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        self._time.advance(0.2)
        return AuthProbeResult(success, 200.0, "us-west-2")


def test_reads_run_every_five_seconds_with_account_then_health() -> None:
    time = FakeTime(stop_after=12.0)
    api, sink = ScriptedApi(time, latency=0.0), ListSink()

    ReadProbe(api, lambda: TOKEN, time.clock(), sink, time.stop).run()

    assert [(obs.probe, obs.label) for obs in sink.items] == [
        ("account", "a0001"),
        ("health", "h0001"),
        ("account", "a0002"),
        ("health", "h0002"),
        ("account", "a0003"),
        ("health", "h0003"),
    ]
    starts = [obs.start_utc for obs in sink.items if obs.probe == "account"]
    assert starts == [START, START + timedelta(seconds=5), START + timedelta(seconds=10)]
    assert api.calls[1] == ("health", (True,))


def test_a_slow_round_does_not_cause_a_burst() -> None:
    time = FakeTime(stop_after=30.0)
    api, sink = ScriptedApi(time, latency=4.0), ListSink()

    ReadProbe(api, lambda: TOKEN, time.clock(), sink, time.stop).run()

    starts = [obs.start_utc for obs in sink.items if obs.probe == "account"]
    gaps = [(b - a).total_seconds() for a, b in pairwise(starts)]
    assert gaps
    assert min(gaps) >= 5.0


def test_read_failures_are_recorded_as_values() -> None:
    time = FakeTime(stop_after=6.0)
    api, sink = ScriptedApi(time, script=[503, 200], latency=0.0), ListSink()

    ReadProbe(api, lambda: TOKEN, time.clock(), sink, time.stop).run()

    account = [obs for obs in sink.items if obs.probe == "account"]
    assert [(obs.status, obs.ok) for obs in account] == [(503, False), (200, True)]
    assert account[1].balance_cents == 10_000
    assert account[1].served_by_region == "us-east-2"
    assert account[1].database_role == "writer"


def test_a_failed_sign_in_still_probes_the_health_endpoint() -> None:
    time = FakeTime(stop_after=1.0)
    api, sink = ScriptedApi(time, latency=0.0), ListSink()

    def token() -> str:
        raise AuthError(f"every region failed {TOKEN}")

    ReadProbe(api, token, time.clock(), sink, time.stop).run()

    assert [(obs.probe, obs.ok, obs.error) for obs in sink.items] == [
        ("account", False, "AuthError"),
        ("health", True, None),
    ]
    assert TOKEN not in repr(sink.items)


def test_no_token_appears_in_read_records() -> None:
    time = FakeTime(stop_after=6.0)
    api, sink = ScriptedApi(time, latency=0.0), ListSink()

    ReadProbe(api, lambda: TOKEN, time.clock(), sink, time.stop).run()

    assert TOKEN not in repr(sink.items)


def test_auth_probe_runs_every_ten_seconds() -> None:
    time = FakeTime(stop_after=25.0)
    session, sink = FakeSession(time), ListSink()

    AuthProbe(session, time.clock(), sink, time.stop).run()

    assert [obs.label for obs in sink.items] == ["u0001", "u0002", "u0003"]
    starts = [obs.start_utc for obs in sink.items]
    assert starts == [START, START + timedelta(seconds=10), START + timedelta(seconds=20)]


def test_auth_failures_are_recorded_as_values_without_a_token() -> None:
    time = FakeTime(stop_after=15.0)
    session, sink = FakeSession(time, [True, False]), ListSink()

    AuthProbe(session, time.clock(), sink, time.stop).run()

    first, second = sink.items
    assert (first.ok, first.status, first.error) == (True, 200, None)
    assert (second.ok, second.status, second.error) == (False, 0, "AuthFailed")
    assert (second.served_by_region, second.latency_ms, second.probe) == (
        "us-west-2",
        200.0,
        "auth",
    )


def test_observation_round_trips_through_data() -> None:
    observation = Observation(
        "write",
        "w0001",
        2,
        START,
        START + timedelta(milliseconds=42),
        True,
        201,
        42.0,
        key="key-1",
        kind="deposit",
        served_by_region="us-east-2",
        processed_region="us-east-2",
        database_role="writer",
        balance_cents=10_001,
    )

    assert Observation.from_data(observation.to_data()) == observation


def test_utc_text_has_milliseconds_and_parses_back() -> None:
    moment = START + timedelta(milliseconds=1234)

    assert format_utc(moment) == "2026-11-11T12:00:01.234Z"
    assert parse_utc(format_utc(moment)) == moment
