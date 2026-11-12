"""Cognito session of the drill user: sign-in, refresh and fallback to the replica region."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

PASSWORD_FLOW = "USER_PASSWORD_AUTH"  # noqa: S105 - the name of the Cognito flow, not a secret
REFRESH_FLOW = "REFRESH_TOKEN_AUTH"
REFRESH_MARGIN_SECONDS = 120.0


class AuthError(Exception):
    """Authentication failed; messages carry error names and regions, never secrets."""


@dataclass(frozen=True)
class AuthResult:
    """Outcome of one InitiateAuth call; the tokens are hidden from `repr`."""

    id_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_in: int


@dataclass(frozen=True)
class AuthProbeResult:
    """Outcome of the authentication probe: no token is exposed."""

    success: bool
    latency_ms: float
    region: str


class CognitoGateway(Protocol):
    """Port to Cognito `InitiateAuth`."""

    def initiate_auth(
        self,
        region: str,
        client_id: str,
        auth_flow: str,
        parameters: Mapping[str, str],
    ) -> AuthResult:
        """Call InitiateAuth; raises AuthError on any failure."""
        ...


class ClientIdProvider(Protocol):
    """Port that gives the user pool client id of each region (read-only SSM, task 3.2)."""

    def client_id(self, region: str) -> str:
        """Client id of the app client in the region."""
        ...


class Session:
    """ID token of the drill user, kept fresh. Safe to share between probe threads.

    Two locks: `_state_lock` guards the fields and is never held during a network call; `_flight`
    makes the renewal single-flight. While a renewal runs, the other callers get the current token
    as long as it has not expired, so a slow or dead Cognito region never stalls the probes that
    still hold a valid token.
    """

    def __init__(
        self,
        gateway: CognitoGateway,
        client_ids: ClientIdProvider,
        username: str,
        password: str,
        regions: tuple[str, str],
        clock: Callable[[], float],
        register_secret: Callable[[str], None] | None = None,
    ) -> None:
        self._register_secret = register_secret
        self._gateway = gateway
        self._client_ids = client_ids
        self._username = username
        self._password = password
        self._regions = regions
        self._clock = clock
        self._state_lock = threading.Lock()
        self._flight = threading.Lock()
        self._id_token: str | None = None
        self._region = ""
        self._refresh_token: str | None = None
        self._expires_at = 0.0

    @property
    def region(self) -> str:
        """Region that issued the current token (empty before the first sign-in)."""
        with self._state_lock:
            return self._region

    def id_token(self) -> str:
        """A valid ID token: signs in, refreshes near expiry, re-authenticates if refresh fails."""
        token = self._fresh_token()
        if token is not None:
            return token
        if self._usable_token() is not None and self._flight.acquire(blocking=False):
            try:
                return self._renew()
            finally:
                self._flight.release()
        current = self._usable_token()
        if current is not None:
            return current
        with self._flight:
            return self._fresh_token() or self._renew()

    def authenticate_secondary(self) -> AuthProbeResult:
        """Sign in against the replica region only, to prove it works; the token is dropped."""
        region = self._regions[1]
        started = self._clock()
        try:
            self._password_sign_in(region)
            success = True
        except Exception:
            success = False
        return AuthProbeResult(success, (self._clock() - started) * 1000.0, region)

    def _fresh_token(self) -> str | None:
        """The current token when it still has more than the refresh margin left."""
        with self._state_lock:
            if self._id_token and self._expires_at - self._clock() >= REFRESH_MARGIN_SECONDS:
                return self._id_token
        return None

    def _usable_token(self) -> str | None:
        """The current token when it has not expired yet."""
        with self._state_lock:
            if self._id_token and self._expires_at > self._clock():
                return self._id_token
        return None

    def _renew(self) -> str:
        """Refresh, else sign in again; the caller holds `_flight`."""
        return self._refresh() or self._sign_in_or_keep_current()

    def _password_sign_in(self, region: str) -> AuthResult:
        parameters = {"USERNAME": self._username, "PASSWORD": self._password}
        return self._gateway.initiate_auth(
            region, self._client_ids.client_id(region), PASSWORD_FLOW, parameters
        )

    def _sign_in_or_keep_current(self) -> str:
        """Sign in again; if that fails, keep the current token while it has not expired."""
        try:
            return self._sign_in()
        except AuthError:
            current = self._usable_token()
            if current is not None:
                return current
            raise

    def _store(self, result: AuthResult, region: str, new_session: bool) -> str:
        if self._register_secret is not None:
            self._register_secret(result.id_token)
            if result.refresh_token:
                self._register_secret(result.refresh_token)
        with self._state_lock:
            self._id_token = result.id_token
            self._region = region
            if new_session:
                self._refresh_token = result.refresh_token
            else:
                self._refresh_token = result.refresh_token or self._refresh_token
            self._expires_at = self._clock() + result.expires_in
        return result.id_token

    def _sign_in_order(self) -> tuple[str, ...]:
        """Regions to try: the last one that worked first, so an outage does not stall re-auth."""
        with self._state_lock:
            region = self._region
        if region and region != self._regions[0]:
            return (region, self._regions[0])
        return self._regions

    def _sign_in(self) -> str:
        failures: list[str] = []
        for region in self._sign_in_order():
            try:
                result = self._password_sign_in(region)
            except Exception as error:
                failures.append(f"{region}: {type(error).__name__}")
                continue
            return self._store(result, region, new_session=True)
        raise AuthError("sign-in failed in every region (" + ", ".join(failures) + ")")

    def _refresh(self) -> str | None:
        """Refresh the token in the region that issued it; None when that is not possible."""
        with self._state_lock:
            refresh_token, region = self._refresh_token, self._region
        if not refresh_token:
            return None
        parameters = {"REFRESH_TOKEN": refresh_token}
        try:
            result = self._gateway.initiate_auth(
                region, self._client_ids.client_id(region), REFRESH_FLOW, parameters
            )
        except Exception:
            return None
        return self._store(result, region, new_session=False)
