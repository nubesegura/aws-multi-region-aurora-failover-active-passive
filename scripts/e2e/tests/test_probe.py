from decimal import Decimal
from itertools import pairwise

import pytest

from dr_drill.auth import AuthError
from dr_drill.probe import (
    ABANDON_MAX_OUTAGE,
    ABANDON_PERMANENT,
    MIN_ATTEMPT_INTERVAL_SECONDS,
    Observation,
    WriteProbe,
    is_permanent_error,
)

from .probe_fakes import FakeTime, ListSink, ScriptedApi, counter_keys

TOKEN = "fake-id-token-value"


def make_probe(
    time: FakeTime,
    api: ScriptedApi,
    sink: ListSink,
    rate: float = 1.0,
    max_outage: float = 600.0,
    token_source: object = None,
) -> WriteProbe:
    return WriteProbe(
        api,
        token_source or (lambda: TOKEN),  # type: ignore[arg-type]
        time.clock(),
        sink,
        time.stop,
        rate_seconds=rate,
        max_outage_seconds=max_outage,
        new_key=counter_keys(),
    )


def posts(api: ScriptedApi) -> list[tuple[object, ...]]:
    return [args for name, args in api.calls if name == "post"]


def test_movements_alternate_deposit_first_with_net_zero_amounts() -> None:
    time = FakeTime(stop_after=4.5)
    api, sink = ScriptedApi(time), ListSink()

    make_probe(time, api, sink).run()

    assert [(args[1], args[2]) for args in posts(api)] == [
        ("deposit", Decimal("0.01")),
        ("withdrawal", Decimal("0.01")),
        ("deposit", Decimal("0.01")),
        ("withdrawal", Decimal("0.01")),
        ("deposit", Decimal("0.01")),
    ]
    assert [obs.label for obs in sink.items] == ["w0001", "w0002", "w0003", "w0004", "w0005"]


def test_each_movement_has_a_fresh_key_and_the_token_is_passed() -> None:
    time = FakeTime(stop_after=2.5)
    api, sink = ScriptedApi(time), ListSink()

    make_probe(time, api, sink).run()

    assert [args[3] for args in posts(api)] == ["key-1", "key-2", "key-3"]
    assert {args[0] for args in posts(api)} == {TOKEN}


def test_new_movements_start_every_rate_seconds() -> None:
    time = FakeTime(stop_after=3.5)
    api, sink = ScriptedApi(time), ListSink()

    make_probe(time, api, sink, rate=1.0).run()

    assert api.starts == [0.0, 1.0, 2.0, 3.0]


def test_a_retry_uses_the_same_key_and_no_new_movement_starts_while_pending() -> None:
    time = FakeTime(stop_after=6.5)
    api, sink = ScriptedApi(time, script=[503, 503, 503, 201]), ListSink()

    make_probe(time, api, sink).run()

    keys = [args[3] for args in posts(api)]
    assert keys[:4] == ["key-1"] * 4
    assert keys[4] == "key-2"
    assert [args[1] for args in posts(api)][:5] == [
        "deposit",
        "deposit",
        "deposit",
        "deposit",
        "withdrawal",
    ]


def test_every_attempt_is_recorded_with_its_attempt_number() -> None:
    time = FakeTime(stop_after=3.5)
    api, sink = ScriptedApi(time, script=[503, 0, 201]), ListSink()

    make_probe(time, api, sink).run()

    first = sink.items[:3]
    assert [(obs.label, obs.attempt, obs.status, obs.ok) for obs in first] == [
        ("w0001", 1, 503, False),
        ("w0001", 2, 0, False),
        ("w0001", 3, 201, True),
    ]
    assert all(obs.probe == "write" and obs.kind == "deposit" for obs in first)
    assert first[0].key == "key-1"
    assert first[2].balance_cents == 10_001
    assert first[2].processed_region == "us-east-2"
    assert first[2].database_role == "writer"
    assert first[2].latency_ms == 50.0


def test_acknowledged_movements_keep_the_key_and_the_result_balance() -> None:
    time = FakeTime(stop_after=1.5)
    api, sink = ScriptedApi(time), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    assert [(ack.label, ack.key, ack.kind, ack.balance_cents) for ack in probe.acked] == [
        ("w0001", "key-1", "deposit", 10_001),
        ("w0002", "key-2", "withdrawal", 10_000),
    ]


def test_a_replay_answer_acknowledges_the_movement_once() -> None:
    time = FakeTime(stop_after=2.5)
    api, sink = ScriptedApi(time, script=[0, 200, 201]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    assert [ack.status for ack in probe.acked] == [200, 201]
    assert [ack.key for ack in probe.acked] == ["key-1", "key-2"]


def test_the_request_rate_never_exceeds_two_per_second_even_in_an_outage() -> None:
    time = FakeTime(stop_after=30.0)
    api, sink = ScriptedApi(time, script=[503], latency=0.0), ListSink()

    make_probe(time, api, sink, rate=0.1, max_outage=600.0).run()

    gaps = [b - a for a, b in pairwise(api.starts)]
    assert gaps
    assert min(gaps) >= MIN_ATTEMPT_INTERVAL_SECONDS
    assert max(sum(1 for s in api.starts if t <= s < t + 1.0) for t in api.starts) <= 2


def test_a_movement_is_abandoned_after_the_maximum_outage() -> None:
    time = FakeTime(stop_after=12.5)
    api, sink = ScriptedApi(time, script=[503] * 8 + [201]), ListSink()
    probe = make_probe(time, api, sink, max_outage=5.0)

    probe.run()

    assert [(item.label, item.key) for item in probe.abandoned] == [("w0001", "key-1")]
    assert (probe.abandoned[0].status, probe.abandoned[0].reason) == (503, ABANDON_MAX_OUTAGE)
    assert probe.probe_errors == 0
    assert not probe.pending
    assert probe.acked[0].label == "w0002"
    assert probe.acked[0].kind == "withdrawal"


def test_delayed_movements_are_counted() -> None:
    time = FakeTime(stop_after=8.0)
    api, sink = ScriptedApi(time, script=[503, 503, 503, 201]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    assert probe.delayed_movements == 1


def test_a_failed_sign_in_is_an_attempt_and_is_retried() -> None:
    time = FakeTime(stop_after=3.5)
    api, sink = ScriptedApi(time), ListSink()
    answers: list[object] = [AuthError("every region failed"), TOKEN]

    def token_source() -> str:
        value = answers.pop(0) if answers else TOKEN
        if isinstance(value, Exception):
            raise value
        return str(value)

    make_probe(time, api, sink, token_source=token_source).run()

    assert (sink.items[0].status, sink.items[0].ok, sink.items[0].error) == (0, False, "AuthError")
    assert (sink.items[1].label, sink.items[1].attempt, sink.items[1].ok) == ("w0001", 2, True)
    assert "every region" not in repr(sink.items)


def test_no_token_appears_in_any_record() -> None:
    time = FakeTime(stop_after=3.5)
    api, sink = ScriptedApi(time, script=[503, 201]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    assert TOKEN not in repr(sink.items) + repr(probe.acked) + repr(probe.abandoned)


def test_stop_during_an_outage_ends_the_loop_without_abandoning() -> None:
    time = FakeTime(stop_after=4.0)
    api, sink = ScriptedApi(time, script=[503]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    assert not probe.acked
    assert not probe.abandoned
    assert len(posts(api)) == 4
    assert [(i.label, i.key, i.kind, i.attempts) for i in probe.pending] == [
        ("w0001", "key-1", "deposit", 4)
    ]


def test_a_stop_set_before_the_start_sends_nothing() -> None:
    time = FakeTime()
    time.stop.set()
    api, sink = ScriptedApi(time), ListSink()

    make_probe(time, api, sink).run()

    assert api.calls == []


def test_the_probe_has_a_name_for_the_orchestrator() -> None:
    time = FakeTime()
    assert make_probe(time, ScriptedApi(time), ListSink()).name == "write"


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
def test_a_permanent_client_error_abandons_at_once_and_the_next_movement_starts(
    status: int,
) -> None:
    time = FakeTime(stop_after=1.5)
    api, sink = ScriptedApi(time, script=[status, 201]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    first = probe.abandoned[0]
    assert (first.label, first.key, first.attempts) == ("w0001", "key-1", 1)
    assert (first.status, first.reason) == (status, ABANDON_PERMANENT)
    assert probe.probe_errors == 1
    assert [ack.key for ack in probe.acked] == ["key-2"]
    assert not probe.pending


def test_a_422_does_not_cause_a_post_storm() -> None:
    time = FakeTime(stop_after=10.5)
    api, sink = ScriptedApi(time, script=[422]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    keys = [args[3] for args in posts(api)]
    assert len(keys) == 11
    assert len(set(keys)) == 11
    assert len(probe.abandoned) == 11
    assert probe.probe_errors == 11


def test_a_first_401_is_retried_with_the_same_key_to_allow_a_token_refresh() -> None:
    time = FakeTime(stop_after=2.5)
    api, sink = ScriptedApi(time, script=[401, 201]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    assert [args[3] for args in posts(api)][:2] == ["key-1", "key-1"]
    assert (probe.acked[0].label, probe.acked[0].status) == ("w0001", 201)
    assert not probe.abandoned


def test_a_second_401_abandons_the_movement() -> None:
    time = FakeTime(stop_after=2.5)
    api, sink = ScriptedApi(time, script=[401, 401, 201]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    first = probe.abandoned[0]
    assert (first.label, first.attempts, first.status, first.reason) == (
        "w0001",
        2,
        401,
        ABANDON_PERMANENT,
    )
    assert probe.acked[0].label == "w0002"


@pytest.mark.parametrize("status", [0, 408, 429, 500, 502, 503, 504])
def test_transient_statuses_stay_retried_with_the_same_key(status: int) -> None:
    time = FakeTime(stop_after=3.5)
    api, sink = ScriptedApi(time, script=[status, status, 201]), ListSink()
    probe = make_probe(time, api, sink)

    probe.run()

    assert [args[3] for args in posts(api)][:3] == ["key-1"] * 3
    assert probe.acked[0].label == "w0001"
    assert not probe.abandoned


@pytest.mark.parametrize(
    ("status", "unauthorized", "expected"),
    [
        (400, 0, True),
        (401, 1, False),
        (401, 2, True),
        (403, 0, True),
        (404, 0, True),
        (409, 0, True),
        (422, 0, True),
        (301, 0, True),
        (405, 0, True),
        (0, 0, False),
        (408, 0, False),
        (429, 0, False),
        (500, 0, False),
        (503, 0, False),
        (200, 0, False),
    ],
)
def test_error_classification(status: int, unauthorized: int, expected: bool) -> None:
    assert is_permanent_error(status, unauthorized) is expected


def test_a_failing_sink_neither_kills_the_probe_nor_loses_the_sent_movement() -> None:
    time = FakeTime(stop_after=2.5)
    api = ScriptedApi(time)

    def broken_sink(observation: Observation) -> None:
        raise RuntimeError("disk full")

    probe = WriteProbe(
        api, lambda: TOKEN, time.clock(), broken_sink, time.stop, new_key=counter_keys()
    )

    probe.run()

    assert [ack.key for ack in probe.acked] == ["key-1", "key-2", "key-3"]
    assert probe.sink_failures == 3
    assert len(posts(api)) == 3


def test_a_stop_with_no_outage_leaves_nothing_pending() -> None:
    time = FakeTime(stop_after=2.5)
    probe = make_probe(time, ScriptedApi(time), ListSink())

    probe.run()

    assert probe.pending == ()
