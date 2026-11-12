"""DNS watcher: resolves the API domain every 2 seconds and records every change of the answer.

This is the DNS convergence seen by this workstation only (its resolver and cache), not by other
resolvers. The watcher is a probe: `ProbeSet` runs it in its own thread for the whole drill.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Sequence

from dr_drill.probe import ProbeClock, format_utc, run_periodic
from dr_drill.timeline import Timeline

DNS_PERIOD_SECONDS = 2.0
HTTPS_PORT = 443

Resolver = Callable[[str], Sequence[str]]


def socket_resolver(domain: str) -> tuple[str, ...]:
    """Addresses of the domain from the system resolver (`getaddrinfo`), sorted and unique."""
    answers = socket.getaddrinfo(domain, HTTPS_PORT, type=socket.SOCK_STREAM)
    return tuple(sorted({str(answer[4][0]) for answer in answers}))


class DnsWatcher:
    """Records the first answer and the time of every change as `dns` timeline events."""

    name = "dns"

    def __init__(
        self,
        domain: str,
        resolver: Resolver,
        clock: ProbeClock,
        timeline: Timeline,
        stop: threading.Event,
        period_seconds: float = DNS_PERIOD_SECONDS,
    ) -> None:
        self._domain = domain
        self._resolver = resolver
        self._clock = clock
        self._timeline = timeline
        self._stop = stop
        self._period = period_seconds
        self._last: tuple[str, ...] | None = None
        self._failures = 0

    @property
    def failures(self) -> int:
        """Lookups that failed; they change nothing and are not recorded as an answer."""
        return self._failures

    def run(self) -> None:
        """Resolve until the stop event is set."""
        run_periodic(self._poll, self._period, self._clock, self._stop)

    def _poll(self) -> None:
        try:
            answer = tuple(sorted(set(self._resolver(self._domain))))
        except (OSError, UnicodeError):
            self._failures += 1
            return
        observed = self._clock.utc_now()
        if answer == self._last:
            return
        self._timeline.add(
            "dns",
            {
                "first": self._last is None,
                "answer": list(answer),
                "previous": None if self._last is None else list(self._last),
                "observed": format_utc(observed),
            },
            observed,
        )
        self._last = answer
