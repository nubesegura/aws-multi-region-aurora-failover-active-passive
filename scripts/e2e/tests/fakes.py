"""Fakes shared by the tests: in-memory adapters, no network and no AWS call."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from dr_drill.config import Settings
from dr_drill.preconditions import PreconditionPorts

ACCOUNT = "123456789012"


def make_settings() -> Settings:
    return Settings(
        username="runner",
        password="Fake-Password-123!",
        api_domain_name="api.example.invalid",
        github_repository="example/repo",
        aws_profile="profile",
        region_primary="us-east-2",
        region_secondary="us-west-2",
        region_global="us-east-1",
        env_type="prod",
    )


class FakePorts:
    """Every read-only port in one object; `calls` records the method names used."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.account = ACCOUNT
        self.stack_statuses: dict[str, str | None] = {}
        self.stack_queries: list[tuple[str, str]] = []
        self.health = 200
        self.fenced: set[str] = set()
        self.writer: str | None = "us-east-2"
        self.active: list[str] = []
        self.logged_in = True
        self.sign_in_error: Exception | None = None
        self.fail_identity = False

    def caller_account(self) -> str:
        self.calls.append("caller_account")
        if self.fail_identity:
            raise RuntimeError("boom " + ACCOUNT)
        return self.account

    def stack_status(self, region: str, stack_name: str) -> str | None:
        self.calls.append("stack_status")
        self.stack_queries.append((region, stack_name))
        return self.stack_statuses.get(stack_name, "UPDATE_COMPLETE")

    def health_status(self) -> int:
        self.calls.append("health_status")
        return self.health

    def reserved_concurrency(self, region: str, function_name: str) -> int | None:
        self.calls.append("reserved_concurrency")
        return 0 if function_name in self.fenced else None

    def writer_region(self) -> str | None:
        self.calls.append("writer_region")
        return self.writer

    def active_runs(self, workflows: Sequence[str]) -> list[str]:
        self.calls.append("active_runs")
        return self.active

    def authenticated(self) -> bool:
        self.calls.append("authenticated")
        return self.logged_in

    def id_token(self) -> str:
        self.calls.append("id_token")
        if self.sign_in_error:
            raise self.sign_in_error
        return "fake-token"

    def ports(self) -> PreconditionPorts:
        return PreconditionPorts(self, self, self, self, self, self, self)


class FakeOpener:
    """Stands in for the no-redirect opener: `handler(request, timeout)` is what `open` returns."""

    def __init__(self, handler: Callable[[Any, float], Any]) -> None:
        self._handler = handler

    def open(self, request: Any, timeout: float) -> Any:
        return self._handler(request, timeout)
