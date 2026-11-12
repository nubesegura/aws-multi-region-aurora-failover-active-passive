"""Read-only preconditions of the drill: nothing is written to prod unless every check passes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from dr_drill.config import Settings

READY_STACK_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})
FUNCTION_DESCRIPTORS = (
    "create-transaction",
    "list-transactions",
    "get-stats",
    "get-health",
    "get-account",
    "get-consistency",
)
GUARDED_WORKFLOWS = ("deploy", "destroy", "failover", "simulate-outage")
HEALTH_OK = 200


class IdentityPort(Protocol):
    """Who the AWS profile is."""

    def caller_account(self) -> str:
        """Account id of the caller (`aws sts get-caller-identity`); callers mask it."""
        ...


class StackPort(Protocol):
    """CloudFormation stack status."""

    def stack_status(self, region: str, stack_name: str) -> str | None:
        """Status of the stack, or None when it does not exist."""
        ...


class HealthPort(Protocol):
    """Public health endpoint of the API."""

    def health_status(self) -> int:
        """HTTP status of `GET /health` on the API domain (0 when it did not answer)."""
        ...


class FencePort(Protocol):
    """Reserved concurrency of the API functions (a fence sets it to 0)."""

    def reserved_concurrency(self, region: str, function_name: str) -> int | None:
        """Reserved concurrency of the function, None when it has none."""
        ...


class WriterPort(Protocol):
    """Aurora global cluster writer."""

    def writer_region(self) -> str | None:
        """Region of the writer cluster of the global cluster."""
        ...


class WorkflowRunsPort(Protocol):
    """GitHub Actions runs and CLI login."""

    def active_runs(self, workflows: Sequence[str]) -> list[str]:
        """Queued or in-progress runs of the workflows, one short description each."""
        ...

    def authenticated(self) -> bool:
        """Whether `gh auth status` succeeds."""
        ...


class SignInPort(Protocol):
    """Anything that can produce an ID token (the Session)."""

    def id_token(self) -> str:
        """ID token of the drill user; raises when sign-in fails."""
        ...


@dataclass(frozen=True)
class PreconditionPorts:
    """Read-only adapters used by the checks."""

    identity: IdentityPort
    stacks: StackPort
    health: HealthPort
    fence: FencePort
    writer: WriterPort
    runs: WorkflowRunsPort
    sign_in: SignInPort


@dataclass(frozen=True)
class Check:
    """Result of one precondition."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreconditionReport:
    """All checks and their combined verdict."""

    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        """True when every check passed."""
        return all(check.ok for check in self.checks)

    @property
    def failed(self) -> tuple[Check, ...]:
        """The checks that did not pass."""
        return tuple(check for check in self.checks if not check.ok)


def mask_account(account: str) -> str:
    """Account id reduced to its last 4 digits."""
    return "*" * 8 + account[-4:]


def api_stack_name(region: str, env_type: str) -> str:
    """Name of the API stack of a data region."""
    return stack_name(region, "api", env_type)


def stack_name(region: str, kind: str, env_type: str) -> str:
    """Name of a stack of the given kind in a region."""
    return f"stk-{region.replace('-', '')}-drdemo-{kind}-{env_type}"


def function_name(region: str, descriptor: str, env_type: str) -> str:
    """Name of an API function."""
    return f"fnc-{region.replace('-', '')}-drdemo-{descriptor}-{env_type}"


def _guarded(name: str, check: Callable[[], Check]) -> Check:
    """Run a check; an adapter failure is a failed check naming only the error class."""
    try:
        return check()
    except Exception as error:
        return Check(name, False, f"could not be checked ({type(error).__name__})")


def _identity(ports: PreconditionPorts) -> Check:
    return Check("aws identity", True, f"account {mask_account(ports.identity.caller_account())}")


def _stacks(ports: PreconditionPorts, settings: Settings) -> Check:
    expected = [
        (settings.region_primary, stack_name(settings.region_primary, "edge", settings.env_type)),
        (settings.region_global, stack_name(settings.region_global, "dns", settings.env_type)),
        *(
            (region, api_stack_name(region, settings.env_type))
            for region in (settings.region_primary, settings.region_secondary)
        ),
    ]
    problems = []
    for region, name in expected:
        status = ports.stacks.stack_status(region, name)
        if status not in READY_STACK_STATUSES:
            problems.append(f"{name} in {region} is {status or 'missing'}")
    if problems:
        return Check("stacks", False, "; ".join(problems))
    return Check("stacks", True, f"{len(expected)} stacks complete")


def _health(ports: PreconditionPorts) -> Check:
    status = ports.health.health_status()
    return Check("api health", status == HEALTH_OK, f"GET /health answered {status}")


def _fence(ports: PreconditionPorts, settings: Settings) -> Check:
    names = [
        (region, function_name(region, descriptor, settings.env_type))
        for region in (settings.region_primary, settings.region_secondary)
        for descriptor in FUNCTION_DESCRIPTORS
    ]
    fenced = [
        name for region, name in names if ports.fence.reserved_concurrency(region, name) is not None
    ]
    if fenced:
        return Check("no fence", False, "reserved concurrency set on " + ", ".join(fenced))
    return Check("no fence", True, "no reserved concurrency on the 12 functions")


def _writer(ports: PreconditionPorts, settings: Settings) -> Check:
    region = ports.writer.writer_region()
    return Check(
        "writer in primary",
        region == settings.region_primary,
        f"writer is in {region or 'unknown'}, expected {settings.region_primary}",
    )


def _runs(ports: PreconditionPorts) -> Check:
    active = ports.runs.active_runs(GUARDED_WORKFLOWS)
    if active:
        return Check("no workflow in progress", False, ", ".join(active))
    return Check("no workflow in progress", True, "none queued or running")


def _github_login(ports: PreconditionPorts) -> Check:
    logged_in = ports.runs.authenticated()
    return Check("gh login", logged_in, "logged in" if logged_in else "gh auth status failed")


def _sign_in(ports: PreconditionPorts) -> Check:
    ports.sign_in.id_token()
    return Check("e2e user sign-in", True, "signed in")


def run_preconditions(ports: PreconditionPorts, settings: Settings) -> PreconditionReport:
    """Run every read-only check; a failing adapter is a failed check, never an exception."""
    steps: list[tuple[str, Callable[[], Check]]] = [
        ("aws identity", lambda: _identity(ports)),
        ("stacks", lambda: _stacks(ports, settings)),
        ("api health", lambda: _health(ports)),
        ("no fence", lambda: _fence(ports, settings)),
        ("writer in primary", lambda: _writer(ports, settings)),
        ("no workflow in progress", lambda: _runs(ports)),
        ("gh login", lambda: _github_login(ports)),
        ("e2e user sign-in", lambda: _sign_in(ports)),
    ]
    return PreconditionReport(tuple(_guarded(name, check) for name, check in steps))
