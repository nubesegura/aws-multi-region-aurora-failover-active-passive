import ast
import io
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from dr_drill import aws_ports, cli, http_safe
from dr_drill.auth import AuthError, AuthResult
from dr_drill.aws_ports import (
    ApiHealth,
    AuroraWriter,
    AwsIdentity,
    CloudFormationStacks,
    GithubRuns,
    LambdaFence,
    SsmClientIds,
    build_ports,
    urllib_health_transport,
)
from dr_drill.redact import OutputGuard
from tests.fakes import ACCOUNT, FakeOpener, make_settings

READ_PREFIXES = ("describe_", "get_")


class StubbedClients:
    """Client factory over real boto3 clients with fake credentials and a Stubber each."""

    def __init__(self) -> None:
        self.stubbers: dict[tuple[str, str], Stubber] = {}
        self.clients: dict[tuple[str, str], Any] = {}

    def __call__(self, service: str, region: str) -> Any:
        key = (service, region)
        if key not in self.clients:
            client = boto3.client(
                service,
                region_name=region,
                aws_access_key_id="fake",
                aws_secret_access_key="fake",
            )
            self.clients[key] = client
            self.stubbers[key] = Stubber(client)
        return self.clients[key]

    def stub(self, service: str, region: str) -> Stubber:
        self(service, region)
        return self.stubbers[(service, region)]

    @contextmanager
    def active(self) -> Iterator[None]:
        for stubber in self.stubbers.values():
            stubber.activate()
        try:
            yield
        finally:
            for stubber in self.stubbers.values():
                stubber.deactivate()


def test_identity_returns_the_account() -> None:
    clients = StubbedClients()
    clients.stub("sts", "us-east-2").add_response(
        "get_caller_identity",
        {"Account": ACCOUNT, "UserId": "u", "Arn": "arn:aws:iam::111:user/fake"},
    )

    with clients.active():
        assert AwsIdentity(clients, "us-east-2").caller_account() == ACCOUNT


def test_stack_status_reads_the_status() -> None:
    clients = StubbedClients()
    stack = {"StackName": "s", "CreationTime": "2026-01-01", "StackStatus": "UPDATE_COMPLETE"}
    clients.stub("cloudformation", "us-east-2").add_response(
        "describe_stacks", {"Stacks": [stack]}, {"StackName": "s"}
    )

    with clients.active():
        assert CloudFormationStacks(clients).stack_status("us-east-2", "s") == "UPDATE_COMPLETE"


def test_a_missing_stack_is_none() -> None:
    clients = StubbedClients()
    clients.stub("cloudformation", "us-east-2").add_client_error(
        "describe_stacks", "ValidationError", "Stack with id s does not exist"
    )

    with clients.active():
        assert CloudFormationStacks(clients).stack_status("us-east-2", "s") is None


def test_other_stack_errors_are_raised() -> None:
    clients = StubbedClients()
    clients.stub("cloudformation", "us-east-2").add_client_error(
        "describe_stacks", "AccessDenied", "no"
    )

    with clients.active(), pytest.raises(Exception, match="AccessDenied"):
        CloudFormationStacks(clients).stack_status("us-east-2", "s")


@pytest.mark.parametrize(
    ("answer", "expected"), [({"ReservedConcurrentExecutions": 0}, 0), ({}, None)]
)
def test_reserved_concurrency(answer: dict[str, int], expected: int | None) -> None:
    clients = StubbedClients()
    clients.stub("lambda", "us-east-2").add_response(
        "get_function_concurrency", answer, {"FunctionName": "f"}
    )

    with clients.active():
        assert LambdaFence(clients).reserved_concurrency("us-east-2", "f") == expected


def _cluster(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {"GlobalClusters": [{"GlobalClusterIdentifier": "g", "GlobalClusterMembers": members}]}


def test_writer_region_comes_from_the_writer_member() -> None:
    clients = StubbedClients()
    members = [
        {"DBClusterArn": "arn:aws:rds:us-west-2:111:cluster:b", "IsWriter": False},
        {"DBClusterArn": "arn:aws:rds:us-east-2:111:cluster:a", "IsWriter": True},
    ]
    clients.stub("rds", "us-east-2").add_response("describe_global_clusters", _cluster(members))

    with clients.active():
        assert AuroraWriter(clients, "us-east-2").writer_region() == "us-east-2"


def test_no_writer_is_none() -> None:
    clients = StubbedClients()
    clients.stub("rds", "us-east-2").add_response(
        "describe_global_clusters", {"GlobalClusters": []}
    )

    with clients.active():
        assert AuroraWriter(clients, "us-east-2").writer_region() is None


def test_a_writer_arn_without_a_region_is_none() -> None:
    clients = StubbedClients()
    members = [{"DBClusterArn": "bad", "IsWriter": True}]
    clients.stub("rds", "us-east-2").add_response("describe_global_clusters", _cluster(members))

    with clients.active():
        assert AuroraWriter(clients, "us-east-2").writer_region() is None


def test_client_id_is_read_from_ssm() -> None:
    clients = StubbedClients()
    clients.stub("ssm", "us-west-2").add_response(
        "get_parameter",
        {"Parameter": {"Value": "client-value"}},
        {"Name": "/drdemo/prod/cognito/BankClientId"},
    )

    with clients.active():
        assert SsmClientIds(clients, "prod").client_id("us-west-2") == "client-value"


def test_client_id_is_read_once_per_region() -> None:
    clients = StubbedClients()
    clients.stub("ssm", "us-west-2").add_response(
        "get_parameter", {"Parameter": {"Value": "client-value"}}
    )
    ids = SsmClientIds(clients, "prod")

    with clients.active():
        assert ids.client_id("us-west-2") == "client-value"
        assert ids.client_id("us-west-2") == "client-value"


def test_boto_client_factory_reuses_one_client_per_service_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, str]] = []

    class FakeSession:
        def __init__(self, profile_name: str) -> None:
            pass

        def client(self, service: str, **kwargs: Any) -> object:
            created.append((service, kwargs["region_name"]))
            return object()

    monkeypatch.setattr(aws_ports.boto3, "Session", FakeSession)
    make = aws_ports.boto_client_factory(make_settings())

    assert make("ssm", "us-east-2") is make("ssm", "us-east-2")
    assert make("ssm", "us-west-2") is not make("ssm", "us-east-2")
    assert created == [("ssm", "us-east-2"), ("ssm", "us-west-2")]


class FakeCommands:
    def __init__(self, outputs: dict[tuple[str, str], str] | None = None, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}
        self.code = code

    def __call__(self, command: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(command))
        if command[:3] == ["gh", "run", "list"]:
            key = (command[command.index("--workflow") + 1], command[command.index("--status") + 1])
            return self.code, self.outputs.get(key, "[]")
        return self.code, ""


def test_active_runs_lists_workflow_and_count_without_run_ids() -> None:
    runner = FakeCommands(
        {("failover.yml", "in_progress"): '[{"databaseId": 1}, {"databaseId": 2}]'}
    )

    found = GithubRuns("example/repo", runner).active_runs(("deploy", "failover"))

    assert found == ["failover has 2 in_progress"]
    assert len(runner.calls) == 4
    assert runner.calls[0][:5] == ["gh", "run", "list", "--repo", "example/repo"]


def test_no_active_runs_is_an_empty_list() -> None:
    assert GithubRuns("example/repo", FakeCommands()).active_runs(("deploy",)) == []


def test_a_failing_gh_run_list_raises() -> None:
    with pytest.raises(RuntimeError, match="gh run list failed"):
        GithubRuns("example/repo", FakeCommands(code=1)).active_runs(("deploy",))


def test_an_unexpected_gh_output_raises() -> None:
    runner = FakeCommands({("deploy.yml", "queued"): "{}"})

    with pytest.raises(TypeError):
        GithubRuns("example/repo", runner).active_runs(("deploy",))


def test_an_empty_gh_output_is_no_runs() -> None:
    runner = FakeCommands({("deploy.yml", "queued"): ""})

    assert GithubRuns("example/repo", runner).active_runs(("deploy",)) == []


@pytest.mark.parametrize(("code", "expected"), [(0, True), (1, False)])
def test_gh_auth_status(code: int, expected: bool) -> None:
    runner = FakeCommands(code=code)

    assert GithubRuns("example/repo", runner).authenticated() is expected
    assert runner.calls == [["gh", "auth", "status"]]


def test_health_uses_the_api_domain_over_https() -> None:
    seen: list[tuple[str, float]] = []

    def transport(url: str, timeout: float) -> int:
        seen.append((url, timeout))
        return 200

    assert ApiHealth("api.example.invalid", transport).health_status() == 200
    assert seen == [("https://api.example.invalid/health", aws_ports.HEALTH_TIMEOUT_SECONDS)]


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (FakeResponse(), 200),
        (urllib.error.HTTPError("https://x", 503, "down", {}, io.BytesIO(b"")), 503),  # type: ignore[arg-type]
        (OSError("unreachable"), 0),
    ],
)
def test_urllib_health_transport(
    monkeypatch: pytest.MonkeyPatch, outcome: object, expected: int
) -> None:
    def fake_urlopen(url: str, timeout: float) -> object:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http_safe, "_OPENER", FakeOpener(fake_urlopen))

    assert urllib_health_transport("https://api.example.invalid/health", 1.0) == expected


def test_urllib_health_transport_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="https"):
        urllib_health_transport("http://api.example.invalid/health", 1.0)


def test_subprocess_runner_runs_an_argument_list_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = "out"

    def fake_run(command: list[str], **kwargs: Any) -> Completed:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(aws_ports.subprocess, "run", fake_run)

    assert aws_ports.subprocess_runner(("gh", "auth", "status")) == (0, "out")
    assert seen["command"] == ["gh", "auth", "status"]
    assert "shell" not in seen["kwargs"]


def test_boto_client_factory_uses_the_profile_region_and_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "ca.pem"
    seen: dict[str, Any] = {}

    class FakeSession:
        def __init__(self, profile_name: str) -> None:
            seen["profile"] = profile_name

        def client(self, service: str, **kwargs: Any) -> str:
            seen["client"] = (service, kwargs)
            return "client"

    monkeypatch.setattr(aws_ports.boto3, "Session", FakeSession)
    make = aws_ports.boto_client_factory(replace(make_settings(), ca_bundle=bundle))

    assert make("sts", "us-east-2") == "client"
    assert seen["profile"] == "profile"
    assert seen["client"][1]["region_name"] == "us-east-2"
    assert seen["client"][1]["verify"] == str(bundle)


def test_boto_client_factory_without_a_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class FakeSession:
        def __init__(self, profile_name: str) -> None:
            pass

        def client(self, service: str, **kwargs: Any) -> str:
            seen.update(kwargs)
            return "client"

    monkeypatch.setattr(aws_ports.boto3, "Session", FakeSession)

    aws_ports.boto_client_factory(make_settings())("sts", "us-east-2")

    assert seen["verify"] is None


class FakeGateway:
    def initiate_auth(
        self, region: str, client_id: str, auth_flow: str, parameters: Any
    ) -> AuthResult:
        if region != "us-east-2":
            raise AuthError("unexpected region")
        return AuthResult("runtime-id-token", "runtime-refresh-token", 900)


def _all_clients() -> StubbedClients:
    clients = StubbedClients()
    clients.stub("sts", "us-east-2").add_response(
        "get_caller_identity",
        {"Account": ACCOUNT, "UserId": "u", "Arn": "arn:aws:iam::111:user/fake"},
    )
    stack = {"StackName": "s", "CreationTime": "2026-01-01", "StackStatus": "CREATE_COMPLETE"}
    for region, count in (("us-east-1", 1), ("us-east-2", 2), ("us-west-2", 1)):
        for _ in range(count):
            clients.stub("cloudformation", region).add_response(
                "describe_stacks", {"Stacks": [stack]}
            )
    for region in ("us-east-2", "us-west-2"):
        for _ in range(6):
            clients.stub("lambda", region).add_response("get_function_concurrency", {})
    members = [{"DBClusterArn": "arn:aws:rds:us-east-2:111:cluster:a", "IsWriter": True}]
    clients.stub("rds", "us-east-2").add_response("describe_global_clusters", _cluster(members))
    clients.stub("ssm", "us-east-2").add_response(
        "get_parameter", {"Parameter": {"Value": "client-value"}}
    )
    return clients


def test_check_runs_end_to_end_over_the_real_adapters_with_stubs(tmp_path: Path) -> None:
    clients = _all_clients()
    out, err = io.StringIO(), io.StringIO()
    guards: list[OutputGuard] = []

    def factory(settings: Any, guard: OutputGuard) -> Any:
        guards.append(guard)
        return build_ports(
            settings,
            guard,
            clients=clients,
            gateway=FakeGateway(),
            runner=FakeCommands(),
            health_transport=lambda url, timeout: 200,
        )

    deps = cli.Dependencies(
        load_settings=make_settings,
        ports_factory=factory,
        out=out,
        err=err,
        runs_dir=tmp_path / "runs",
    )

    with clients.active():
        code = cli.main(["check"], deps)

    assert code == 0, out.getvalue()
    assert len(out.getvalue().splitlines()) == 8
    assert err.getvalue() == ""
    assert guards[0].reason("runtime-id-token") is not None
    assert guards[0].reason("runtime-refresh-token") is not None


def _client_call_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = ast.unparse(node.func.value)
            if "clients(" in receiver or "_client" in receiver or receiver == "client":
                names.add(node.func.attr)
    return names


def test_every_aws_call_is_a_read_verb() -> None:
    source = Path(aws_ports.__file__).read_text(encoding="utf-8")

    names = _client_call_names(source)

    assert names == {
        "get_caller_identity",
        "describe_stacks",
        "get_function_concurrency",
        "describe_global_clusters",
        "get_parameter",
    }
    assert all(name.startswith(READ_PREFIXES) for name in names)


def test_the_allow_list_check_would_catch_a_write_verb() -> None:
    names = _client_call_names("self._clients('lambda', r).invoke(FunctionName=f)")

    assert not all(name.startswith(READ_PREFIXES) for name in names)


def test_the_gh_adapter_never_dispatches_or_cancels() -> None:
    source = Path(aws_ports.__file__).read_text(encoding="utf-8")

    for verb in ('"workflow"', '"cancel"', '"rerun"', '"delete"', "--force"):
        assert verb not in source
