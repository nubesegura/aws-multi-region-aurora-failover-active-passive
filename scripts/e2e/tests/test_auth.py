import threading
from collections.abc import Callable, Mapping

import pytest

from dr_drill.auth import (
    PASSWORD_FLOW,
    REFRESH_FLOW,
    AuthError,
    AuthResult,
    Session,
)

PRIMARY = "us-east-2"
SECONDARY = "us-west-2"
PASSWORD = "Fake-Password-123!"
ID_TOKEN = "fake-id-token-value"
REFRESH_TOKEN = "fake-refresh-token-value"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeClientIds:
    def client_id(self, region: str) -> str:
        return f"client-{region}"


class FakeGateway:
    """Scripted gateway: `failing` regions raise, every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failing: set[tuple[str, str]] = set()
        self.counter = 0

    def initiate_auth(
        self, region: str, client_id: str, auth_flow: str, parameters: Mapping[str, str]
    ) -> AuthResult:
        self.calls.append((region, auth_flow))
        if (region, auth_flow) in self.failing:
            raise AuthError(f"InitiateAuth in {region} failed: NotAuthorizedException (HTTP 400)")
        self.counter += 1
        refresh = REFRESH_TOKEN if auth_flow == PASSWORD_FLOW else None
        return AuthResult(f"{ID_TOKEN}-{self.counter}", refresh, 900)


def make_session(
    gateway: FakeGateway,
    clock: FakeClock,
    register_secret: Callable[[str], None] | None = None,
) -> Session:
    return Session(
        gateway, FakeClientIds(), "runner", PASSWORD, (PRIMARY, SECONDARY), clock, register_secret
    )


def test_first_sign_in_uses_the_primary_region_and_caches_the_token() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)

    first = session.id_token()
    clock.now += 600
    second = session.id_token()

    assert first == second == f"{ID_TOKEN}-1"
    assert gateway.calls == [(PRIMARY, PASSWORD_FLOW)]
    assert session.region == PRIMARY


def test_token_is_refreshed_when_less_than_two_minutes_remain() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)
    session.id_token()
    clock.now += 900 - 119

    token = session.id_token()

    assert token == f"{ID_TOKEN}-2"
    assert gateway.calls == [(PRIMARY, PASSWORD_FLOW), (PRIMARY, REFRESH_FLOW)]


def test_refresh_does_not_happen_before_the_margin() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)
    session.id_token()
    clock.now += 900 - 121

    session.id_token()

    assert gateway.calls == [(PRIMARY, PASSWORD_FLOW)]


def test_refresh_keeps_the_refresh_token_for_the_next_round() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)
    session.id_token()
    for _ in range(2):
        clock.now += 800
        session.id_token()

    assert [flow for _, flow in gateway.calls] == [PASSWORD_FLOW, REFRESH_FLOW, REFRESH_FLOW]


def test_primary_failure_falls_back_to_the_replica() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    gateway.failing.add((PRIMARY, PASSWORD_FLOW))
    session = make_session(gateway, clock)

    session.id_token()

    assert gateway.calls == [(PRIMARY, PASSWORD_FLOW), (SECONDARY, PASSWORD_FLOW)]
    assert session.region == SECONDARY


def test_failed_refresh_reauthenticates_with_the_password() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)
    session.id_token()
    gateway.failing.add((PRIMARY, REFRESH_FLOW))
    clock.now += 850

    token = session.id_token()

    assert token == f"{ID_TOKEN}-2"
    assert gateway.calls[1:] == [(PRIMARY, REFRESH_FLOW), (PRIMARY, PASSWORD_FLOW)]


def test_refresh_is_skipped_without_a_refresh_token() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)
    session.id_token()
    session._refresh_token = None
    clock.now += 850

    session.id_token()

    assert gateway.calls[1:] == [(PRIMARY, PASSWORD_FLOW)]


def test_every_region_failing_raises_without_secrets() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    gateway.failing.update({(PRIMARY, PASSWORD_FLOW), (SECONDARY, PASSWORD_FLOW)})
    session = make_session(gateway, clock)

    with pytest.raises(AuthError) as raised:
        session.id_token()

    message = str(raised.value)
    assert PRIMARY in message
    assert SECONDARY in message
    assert PASSWORD not in message
    assert ID_TOKEN not in message


def test_authenticate_secondary_reports_success_latency_and_region_only() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)

    result = session.authenticate_secondary()

    assert result.success
    assert result.region == SECONDARY
    assert result.latency_ms == 0.0
    assert gateway.calls == [(SECONDARY, PASSWORD_FLOW)]
    assert ID_TOKEN not in repr(result)
    assert session.region == ""


def test_authenticate_secondary_failure_is_a_value() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    gateway.failing.add((SECONDARY, PASSWORD_FLOW))

    result = make_session(gateway, clock).authenticate_secondary()

    assert not result.success


def test_auth_result_repr_hides_the_tokens() -> None:
    assert ID_TOKEN not in repr(AuthResult(ID_TOKEN, REFRESH_TOKEN, 900))
    assert REFRESH_TOKEN not in repr(AuthResult(ID_TOKEN, REFRESH_TOKEN, 900))


def test_tokens_are_registered_with_the_guard_as_they_arrive() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    registered: list[str] = []
    session = make_session(gateway, clock, registered.append)

    session.id_token()
    clock.now += 900 - 119
    session.id_token()

    assert registered == [f"{ID_TOKEN}-1", REFRESH_TOKEN, f"{ID_TOKEN}-2"]


def test_a_still_valid_token_is_kept_when_refresh_and_sign_in_fail() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)
    first = session.id_token()
    clock.now += 900 - 60
    gateway.failing |= {
        (PRIMARY, REFRESH_FLOW),
        (PRIMARY, PASSWORD_FLOW),
        (SECONDARY, PASSWORD_FLOW),
    }

    assert session.id_token() == first


def test_an_expired_token_is_not_kept_when_every_path_fails() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    session = make_session(gateway, clock)
    session.id_token()
    clock.now += 900
    gateway.failing |= {
        (PRIMARY, REFRESH_FLOW),
        (PRIMARY, PASSWORD_FLOW),
        (SECONDARY, PASSWORD_FLOW),
    }

    with pytest.raises(AuthError):
        session.id_token()


def test_authenticate_secondary_reports_unexpected_errors_as_values() -> None:
    class BrokenClientIds:
        def client_id(self, region: str) -> str:
            raise RuntimeError("ssm down")

    session = Session(
        FakeGateway(), BrokenClientIds(), "runner", PASSWORD, (PRIMARY, SECONDARY), FakeClock()
    )

    result = session.authenticate_secondary()

    assert result.success is False
    assert result.region == SECONDARY


def test_sign_in_starts_with_the_last_region_that_worked() -> None:
    gateway, clock = FakeGateway(), FakeClock()
    gateway.failing.add((PRIMARY, PASSWORD_FLOW))
    session = make_session(gateway, clock)
    session.id_token()
    gateway.failing.add((SECONDARY, REFRESH_FLOW))
    clock.now += 850
    gateway.calls.clear()

    session.id_token()

    assert gateway.calls == [(SECONDARY, REFRESH_FLOW), (SECONDARY, PASSWORD_FLOW)]


class BlockingGateway(FakeGateway):
    """Blocks inside the given flow until released, to hold a renewal in flight."""

    def __init__(self, blocked_flow: str) -> None:
        super().__init__()
        self.blocked_flow = blocked_flow
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocking = False

    def initiate_auth(
        self, region: str, client_id: str, auth_flow: str, parameters: Mapping[str, str]
    ) -> AuthResult:
        if self.blocking and auth_flow == self.blocked_flow:
            self.entered.set()
            assert self.release.wait(timeout=5)
        return super().initiate_auth(region, client_id, auth_flow, parameters)


def start_blocked_refresh(
    gateway: BlockingGateway, clock: FakeClock
) -> tuple[Session, str, threading.Thread, list[str]]:
    session = make_session(gateway, clock)
    first = session.id_token()
    clock.now += 900 - 60
    gateway.blocking = True
    results: list[str] = []
    thread = threading.Thread(target=lambda: results.append(session.id_token()))
    thread.start()
    assert gateway.entered.wait(timeout=5)
    return session, first, thread, results


def test_a_renewal_in_flight_does_not_block_the_others_while_the_token_is_valid() -> None:
    gateway, clock = BlockingGateway(REFRESH_FLOW), FakeClock()
    session, first, thread, results = start_blocked_refresh(gateway, clock)

    assert session.id_token() == first
    assert session.region == PRIMARY

    gateway.release.set()
    thread.join(timeout=5)
    assert results == [f"{ID_TOKEN}-2"]
    assert [flow for _, flow in gateway.calls].count(REFRESH_FLOW) == 1


def test_the_authentication_probe_does_not_wait_for_a_renewal_in_flight() -> None:
    gateway, clock = BlockingGateway(REFRESH_FLOW), FakeClock()
    session, _, thread, _ = start_blocked_refresh(gateway, clock)

    assert session.authenticate_secondary().success

    gateway.release.set()
    thread.join(timeout=5)


def test_concurrent_first_sign_ins_share_one_network_call() -> None:
    gateway, clock = BlockingGateway(PASSWORD_FLOW), FakeClock()
    gateway.blocking = True
    session = make_session(gateway, clock)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(session.id_token())) for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    assert gateway.entered.wait(timeout=5)
    gateway.release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert results == [f"{ID_TOKEN}-1"] * 3
    assert gateway.calls == [(PRIMARY, PASSWORD_FLOW)]
