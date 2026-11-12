"""Real read-only adapters of the preconditions: boto3 reads, `gh` queries and `GET /health`.

Every AWS call here is a read verb (`describe_*` or `get_*`); a static test enforces it.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
from collections.abc import Callable, Sequence
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from dr_drill.auth import CognitoGateway, Session
from dr_drill.cognito_http import HttpCognitoGateway
from dr_drill.config import Settings
from dr_drill.http_safe import open_url
from dr_drill.preconditions import PreconditionPorts
from dr_drill.redact import OutputGuard

ClientFactory = Callable[[str, str], Any]
CommandRunner = Callable[[Sequence[str]], tuple[int, str]]
HealthTransport = Callable[[str, float], int]

HEALTH_TIMEOUT_SECONDS = 10.0
COMMAND_TIMEOUT_SECONDS = 60
ACTIVE_RUN_STATUSES = ("queued", "in_progress")
RUN_LIST_LIMIT = "20"
STACK_MISSING_CODE = "ValidationError"
CLIENT_ID_PARAMETER = "/drdemo/{env_type}/cognito/BankClientId"
_BOTO_CONFIG = Config(
    connect_timeout=5, read_timeout=15, retries={"max_attempts": 2, "mode": "standard"}
)


def boto_client_factory(settings: Settings) -> ClientFactory:
    """Clients of the profile in `settings`, honouring the CA bundle behind TLS inspection.

    One client per (service, region), created under a lock: a boto3 `Session` is not thread-safe
    for client creation and the probe threads share this factory. Clients themselves are reused.
    """
    session = boto3.Session(profile_name=settings.aws_profile)
    verify = str(settings.ca_bundle) if settings.ca_bundle else None
    cache: dict[tuple[str, str], Any] = {}
    lock = threading.Lock()

    def make(service: str, region: str) -> Any:
        with lock:
            if (service, region) not in cache:
                cache[(service, region)] = session.client(
                    service, region_name=region, config=_BOTO_CONFIG, verify=verify
                )
            return cache[(service, region)]

    return make


def subprocess_runner(command: Sequence[str]) -> tuple[int, str]:
    """Run an argument list without a shell; returns the exit code and standard output."""
    completed = subprocess.run(  # noqa: S603 - argument list, no shell, fixed executable names
        list(command),
        capture_output=True,
        text=True,
        check=False,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return completed.returncode, completed.stdout


def urllib_health_transport(url: str, timeout: float) -> int:
    """HTTP status of a GET; 0 when the endpoint did not answer."""
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    try:
        with open_url(url, timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


class AwsIdentity:
    """`sts get-caller-identity`."""

    def __init__(self, clients: ClientFactory, region: str) -> None:
        self._clients = clients
        self._region = region

    def caller_account(self) -> str:
        """Account id of the caller; the caller masks it."""
        return str(self._clients("sts", self._region).get_caller_identity()["Account"])


class CloudFormationStacks:
    """Stack status through `describe_stacks`."""

    def __init__(self, clients: ClientFactory) -> None:
        self._clients = clients

    def stack_status(self, region: str, stack_name: str) -> str | None:
        """Status of the stack, None when it does not exist; other errors are raised."""
        try:
            stacks = self._clients("cloudformation", region).describe_stacks(StackName=stack_name)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == STACK_MISSING_CODE:
                return None
            raise
        return str(stacks["Stacks"][0]["StackStatus"])


class LambdaFence:
    """Reserved concurrency through `get_function_concurrency`."""

    def __init__(self, clients: ClientFactory) -> None:
        self._clients = clients

    def reserved_concurrency(self, region: str, function_name: str) -> int | None:
        """Reserved concurrency of the function, None when it has none."""
        answer = self._clients("lambda", region).get_function_concurrency(
            FunctionName=function_name
        )
        value = answer.get("ReservedConcurrentExecutions")
        return None if value is None else int(value)


class AuroraWriter:
    """Region of the global cluster writer through `describe_global_clusters`."""

    def __init__(self, clients: ClientFactory, region: str) -> None:
        self._clients = clients
        self._region = region

    def writer_region(self) -> str | None:
        """Region of the writer member of the first global cluster that has one."""
        clusters = self._clients("rds", self._region).describe_global_clusters()
        for cluster in clusters.get("GlobalClusters", []):
            for member in cluster.get("GlobalClusterMembers", []):
                if member.get("IsWriter"):
                    return _region_of_arn(str(member["DBClusterArn"]))
        return None


def _region_of_arn(arn: str) -> str | None:
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 and parts[3] else None


class GithubRuns:
    """`gh run list` and `gh auth status` through an injected command runner."""

    def __init__(self, repository: str, runner: CommandRunner = subprocess_runner) -> None:
        self._repository = repository
        self._runner = runner

    def active_runs(self, workflows: Sequence[str]) -> list[str]:
        """Queued or in-progress runs of the workflows; the run id is left out on purpose."""
        found: list[str] = []
        for workflow in workflows:
            for status in ACTIVE_RUN_STATUSES:
                count = len(self._list(workflow, status))
                if count:
                    found.append(f"{workflow} has {count} {status}")
        return found

    def authenticated(self) -> bool:
        """Whether `gh auth status` succeeds."""
        code, _ = self._runner(["gh", "auth", "status"])
        return code == 0

    def _list(self, workflow: str, status: str) -> list[object]:
        command = [
            "gh",
            "run",
            "list",
            "--repo",
            self._repository,
            "--workflow",
            f"{workflow}.yml",
            "--status",
            status,
            "--json",
            "databaseId",
            "--limit",
            RUN_LIST_LIMIT,
        ]
        code, output = self._runner(command)
        if code != 0:
            raise RuntimeError("gh run list failed")
        runs = json.loads(output or "[]")
        if not isinstance(runs, list):
            raise TypeError("unexpected gh run list output")
        return runs


class ApiHealth:
    """`GET /health` on the API domain."""

    def __init__(self, domain: str, transport: HealthTransport = urllib_health_transport) -> None:
        self._url = f"https://{domain}/health"
        self._transport = transport

    def health_status(self) -> int:
        """HTTP status of `GET /health` (0 when it did not answer)."""
        return self._transport(self._url, HEALTH_TIMEOUT_SECONDS)


class SsmClientIds:
    """App client id of each region from SSM (`get_parameter`, read only)."""

    def __init__(self, clients: ClientFactory, env_type: str) -> None:
        self._clients = clients
        self._name = CLIENT_ID_PARAMETER.format(env_type=env_type)
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def client_id(self, region: str) -> str:
        """Client id of the bank app client in the region; read once per region, then cached."""
        with self._lock:
            cached = self._cache.get(region)
        if cached is not None:
            return cached
        answer = self._clients("ssm", region).get_parameter(Name=self._name)
        value = str(answer["Parameter"]["Value"])
        with self._lock:
            self._cache[region] = value
        return value


def build_ports(
    settings: Settings,
    guard: OutputGuard,
    clients: ClientFactory | None = None,
    gateway: CognitoGateway | None = None,
    runner: CommandRunner = subprocess_runner,
    health_transport: HealthTransport = urllib_health_transport,
) -> PreconditionPorts:
    """The real read-only ports; tokens obtained by the session are registered with the guard."""
    make = clients or boto_client_factory(settings)
    session = Session(
        gateway or HttpCognitoGateway(),
        SsmClientIds(make, settings.env_type),
        settings.username,
        settings.password,
        (settings.region_primary, settings.region_secondary),
        time.time,
        guard.register,
    )
    return PreconditionPorts(
        identity=AwsIdentity(make, settings.region_primary),
        stacks=CloudFormationStacks(make),
        health=ApiHealth(settings.api_domain_name, health_transport),
        fence=LambdaFence(make),
        writer=AuroraWriter(make, settings.region_primary),
        runs=GithubRuns(settings.github_repository, runner),
        sign_in=session,
    )
