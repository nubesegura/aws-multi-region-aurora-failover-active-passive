import socket
from datetime import UTC, timedelta

import pytest

from dr_drill import dns
from dr_drill.dns import DnsWatcher, socket_resolver
from dr_drill.redact import OutputGuard
from dr_drill.timeline import Timeline
from tests.probe_fakes import START, FakeTime

MAX_ROUNDS = 1000


def _timeline() -> Timeline:
    return Timeline("run-1", OutputGuard(), lambda: START)


class ScriptedResolver:
    """Answers per call from a script; an exception in the script is raised."""

    def __init__(self, script: list[list[str] | Exception]) -> None:
        self._script = script
        self.calls = 0

    def __call__(self, domain: str) -> list[str]:
        self.calls += 1
        assert self.calls < MAX_ROUNDS, "the fake clock loop did not stop"
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _run(script: list[list[str] | Exception], rounds: int) -> tuple[Timeline, DnsWatcher]:
    time = FakeTime(stop_after=rounds * 2.0 - 1.0)
    timeline = _timeline()
    watcher = DnsWatcher(
        "api.example.invalid", ScriptedResolver(script), time.clock(), timeline, time.stop
    )
    watcher.run()
    return timeline, watcher


def test_the_first_answer_is_recorded_sorted() -> None:
    timeline, _ = _run([["10.0.0.2", "10.0.0.1"]], rounds=3)

    events = timeline.events("dns")

    assert len(events) == 1
    assert events[0].data["answer"] == ["10.0.0.1", "10.0.0.2"]
    assert events[0].data["first"] is True
    assert events[0].data["previous"] is None
    assert events[0].time_utc == START


def test_a_change_is_recorded_once_with_the_time_of_the_first_different_answer() -> None:
    script: list[list[str] | Exception] = [["1.1.1.1"]] * 3 + [["2.2.2.2"]]
    timeline, _ = _run(script, rounds=8)

    events = timeline.events("dns")

    assert [event.data["answer"] for event in events] == [["1.1.1.1"], ["2.2.2.2"]]
    assert events[1].data["previous"] == ["1.1.1.1"]
    assert events[1].data["first"] is False
    assert events[1].time_utc == START + timedelta(seconds=6)


def test_the_same_set_in_another_order_is_not_a_change() -> None:
    script: list[list[str] | Exception] = [["1.1.1.1", "2.2.2.2"], ["2.2.2.2", "1.1.1.1"]]
    timeline, _ = _run(script, rounds=4)

    assert len(timeline.events("dns")) == 1


def test_a_failed_lookup_is_counted_and_changes_nothing() -> None:
    script: list[list[str] | Exception] = [["1.1.1.1"], OSError("down"), ["1.1.1.1"]]
    timeline, watcher = _run(script, rounds=4)

    assert watcher.failures == 1
    assert len(timeline.events("dns")) == 1


def test_a_resolver_encoding_error_is_counted_like_a_failed_lookup() -> None:
    script: list[list[str] | Exception] = [["1.1.1.1"], UnicodeError("bad label")]
    _, watcher = _run(script, rounds=3)

    assert watcher.failures >= 1


def test_it_polls_every_two_seconds() -> None:
    time = FakeTime(stop_after=9.0)
    resolver = ScriptedResolver([["1.1.1.1"]])

    DnsWatcher("a.example.invalid", resolver, time.clock(), _timeline(), time.stop).run()

    assert resolver.calls == 5
    assert set(time.sleeps) == {2.0}


def test_socket_resolver_sorts_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(
        host: str, port: int, type: int
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert (host, port, type) == ("api.example.invalid", 443, socket.SOCK_STREAM)
        family = socket.AF_INET
        return [
            (family, type, 6, "", ("9.9.9.9", 443)),
            (family, type, 6, "", ("1.1.1.1", 443)),
            (family, type, 6, "", ("9.9.9.9", 443)),
        ]

    monkeypatch.setattr(dns.socket, "getaddrinfo", fake_getaddrinfo)

    assert socket_resolver("api.example.invalid") == ("1.1.1.1", "9.9.9.9")


def test_the_event_time_is_timezone_aware() -> None:
    timeline, _ = _run([["1.1.1.1"]], rounds=1)

    assert timeline.events("dns")[0].time_utc.tzinfo is UTC
