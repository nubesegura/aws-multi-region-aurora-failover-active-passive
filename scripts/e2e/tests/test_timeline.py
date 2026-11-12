import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from dr_drill.probe import Observation, WriteProbe
from dr_drill.redact import OutputGuard, RefusedOutput
from dr_drill.timeline import SCHEMA_VERSION, ProbeSet, Timeline

from .probe_fakes import FakeTime, ScriptedApi, counter_keys

START = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmYWtlLXVzZXIifQ.c2lnbmF0dXJlLWZha2U"


def make_timeline() -> Timeline:
    return Timeline("2026-11-11T12-00-00Z", OutputGuard(["Fake-Password-123!"]), lambda: START)


def observation(label: str = "w0001", offset_ms: int = 0) -> Observation:
    start = START + timedelta(milliseconds=offset_ms)
    return Observation(
        "write",
        label,
        1,
        start,
        start + timedelta(milliseconds=40),
        True,
        201,
        40.0,
        key="3f2b8c1e-9d4a-4c7e-8b6a-1a2b3c4d5e6f",
        kind="deposit",
        served_by_region="us-east-2",
        processed_region="us-east-2",
        database_role="writer",
        balance_cents=10_001,
    )


def test_json_round_trip_keeps_every_event() -> None:
    timeline = make_timeline()
    timeline.record_observation(observation())
    timeline.add("phase", {"name": "baseline"}, START + timedelta(seconds=1))
    timeline.add("dns", {"answers": ["192.0.2.1"]}, START + timedelta(seconds=2))
    timeline.add("workflow_step", {"name": "fence", "seconds": 12.5}, START + timedelta(seconds=3))
    timeline.add("metric_sample", {"lag_ms": 120.0}, START + timedelta(seconds=4))
    timeline.add("state_snapshot", {"writer": "us-east-2"}, START + timedelta(seconds=5))
    timeline.add("note", {"text": "hello"}, START + timedelta(seconds=6))

    loaded = Timeline.from_json(timeline.to_json(), OutputGuard())

    assert loaded.run_id == timeline.run_id
    assert loaded.events() == timeline.events()
    assert loaded.observations() == (observation(),)


def test_document_has_a_schema_version_and_the_run_id() -> None:
    document = json.loads(make_timeline().to_json())

    assert document == {
        "schema_version": SCHEMA_VERSION,
        "run_id": "2026-11-11T12-00-00Z",
        "events": [],
    }


def test_events_are_sorted_by_time_and_filterable() -> None:
    timeline = make_timeline()
    timeline.add("note", {"text": "late"}, START + timedelta(seconds=9))
    timeline.add("phase", {"name": "early"}, START)

    assert [event.kind for event in timeline.events()] == ["phase", "note"]
    assert [event.data["text"] for event in timeline.events("note")] == ["late"]


def test_the_clock_stamps_events_without_a_time() -> None:
    timeline = make_timeline()
    timeline.note("now")

    assert timeline.events()[0].time_utc == START


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown event kind"):
        make_timeline().add("mystery", {})


def test_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="schema"):
        Timeline.from_json('{"schema_version": 99, "run_id": "r", "events": []}', OutputGuard())


def test_from_json_uses_the_real_clock_by_default() -> None:
    loaded = Timeline.from_json(make_timeline().to_json(), OutputGuard())

    loaded.note("later")

    assert loaded.events()[0].time_utc.tzinfo is not None


def test_the_guard_refuses_an_event_carrying_a_token_shaped_string() -> None:
    timeline = make_timeline()

    with pytest.raises(RefusedOutput) as raised:
        timeline.add("note", {"text": f"Bearer {FAKE_JWT}"})

    assert FAKE_JWT not in str(raised.value)
    assert timeline.events() == ()


def test_the_guard_refuses_a_known_secret_in_an_event() -> None:
    with pytest.raises(RefusedOutput):
        make_timeline().add("note", {"text": "pwd Fake-Password-123!"})


def test_a_refused_observation_becomes_a_note_without_the_value() -> None:
    timeline = make_timeline()
    bad = observation()
    bad = Observation(**{**bad.__dict__, "error": FAKE_JWT})

    timeline.record_observation(bad)

    [event] = timeline.events()
    assert event.kind == "note"
    assert FAKE_JWT not in json.dumps(event.data)
    assert "w0001" in event.data["text"]


def test_a_long_latency_float_is_stored_rounded_and_not_refused() -> None:
    timeline = make_timeline()
    long_latency = replace(observation(), latency_ms=523.123456789012)

    timeline.record_observation(long_latency)

    [event] = timeline.events()
    assert event.kind == "request"
    assert event.data["latency_ms"] == 523.123


def test_a_refused_observation_note_names_the_field_and_not_the_value() -> None:
    timeline = make_timeline()
    account = "123456789012"
    bad = replace(observation(), error=f"denied for {account}")

    timeline.record_observation(bad)

    [event] = timeline.events()
    assert event.kind == "note"
    assert "(field: error)" in event.data["text"]
    assert "12-digit" in event.data["text"]
    assert account not in event.data["text"]


def test_concurrent_appends_lose_nothing() -> None:
    timeline = make_timeline()

    def worker(number: int) -> None:
        for index in range(100):
            timeline.record_observation(observation(f"w{number}-{index}"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(timeline.observations()) == 800
    assert len({obs.label for obs in timeline.observations()}) == 800


class LoopProbe:
    """Runs until stopped, sending one observation first."""

    def __init__(self, name: str, stop: threading.Event, timeline: Timeline) -> None:
        self.name = name
        self._stop = stop
        self._timeline = timeline
        self.started = threading.Event()

    def run(self) -> None:
        self._timeline.record_observation(observation(self.name))
        self.started.set()
        self._stop.wait(5.0)


class CrashingProbe:
    name = "crash"

    def run(self) -> None:
        raise RuntimeError("https://secret.example.invalid/token=abc")


class StuckProbe:
    name = "stuck"

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def run(self) -> None:
        self._release.wait(5.0)


def test_probe_set_starts_and_stops_all_probes_together() -> None:
    timeline, stop = make_timeline(), threading.Event()
    probes = [LoopProbe(name, stop, timeline) for name in ("write", "read", "auth")]
    probe_set = ProbeSet(probes, stop, timeline)

    probe_set.start()
    for probe in probes:
        assert probe.started.wait(2.0)
    stopped = probe_set.stop()

    assert stopped
    assert {obs.label for obs in timeline.observations()} == {"write", "read", "auth"}
    states = [event.data["state"] for event in timeline.events("phase")]
    assert states == ["started", "stopped"]


def test_a_crashing_probe_is_noted_by_class_name_and_does_not_stop_the_others() -> None:
    timeline, stop = make_timeline(), threading.Event()
    loop = LoopProbe("write", stop, timeline)
    probe_set = ProbeSet([CrashingProbe(), loop], stop, timeline)

    probe_set.start()
    assert loop.started.wait(2.0)
    probe_set.stop()

    notes = [event.data["text"] for event in timeline.events("note")]
    assert notes == ["probe crash crashed: RuntimeError"]


def test_stop_reports_a_probe_that_does_not_end() -> None:
    timeline, stop, release = make_timeline(), threading.Event(), threading.Event()
    probe_set = ProbeSet([StuckProbe(release)], stop, timeline)
    probe_set.start()

    stopped = probe_set.stop(join_seconds=0.05)
    release.set()

    assert not stopped
    assert timeline.events("phase")[-1].data["state"] == "stuck"


def test_a_write_probe_keeps_running_when_the_timeline_refuses_its_observations() -> None:
    time = FakeTime(stop_after=2.5)
    timeline = make_timeline()
    probe = WriteProbe(
        ScriptedApi(time, region=FAKE_JWT),
        lambda: "fake-id-token-value",
        time.clock(),
        timeline.record_observation,
        time.stop,
        new_key=counter_keys(),
    )

    probe.run()

    assert [ack.label for ack in probe.acked] == ["w0001", "w0002", "w0003"]
    assert [event.kind for event in timeline.events()] == ["note"] * 3
    assert FAKE_JWT not in json.dumps([event.data for event in timeline.events()])
