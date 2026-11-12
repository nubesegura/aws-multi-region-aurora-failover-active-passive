"""Read-only AWS observers: fence state, writer region, Route 53 weights, lag, events and Cognito.

Every boto3 call in this module is a read (`describe_*`, `get_*`, `list_*`); a static test enforces
it. Account ids and ARNs are reduced to names before anything leaves the adapter. Any value read
from SSM is passed to `OutputGuard.register` before it is used: the guard's heuristic is only the
second line of defence.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from dr_drill.aws_ports import AuroraWriter, ClientFactory, boto_client_factory
from dr_drill.config import Settings
from dr_drill.preconditions import FUNCTION_DESCRIPTORS, function_name
from dr_drill.redact import OutputGuard

LAG_PERIOD_SECONDS = 60
LAG_METRICS = ("AuroraGlobalDBReplicationLag", "AuroraGlobalDBRPOLag")
USER_POOL_PARAMETER = "/drdemo/{env_type}/cognito/UserPoolId"
CLIENT_PARAMETER = "/drdemo/{env_type}/cognito/BankClientId"
MAX_EVENT_PAGES = 20
STACK_MISSING_CODE = "ValidationError"
ACCOUNT_PLACEHOLDER = "<account>"

_ARN = re.compile(
    r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d*:(?:[A-Za-z0-9._-]+[:/])*([A-Za-z0-9._-]+)"
)
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


@dataclass(frozen=True)
class LagSample:
    """One CloudWatch datapoint of an Aurora global database lag metric (maximum, milliseconds)."""

    region: str
    metric: str
    time_utc: datetime
    milliseconds: float


@dataclass(frozen=True)
class RdsEvent:
    """One RDS cluster event: when, where and what (names only, no ARN, no account id)."""

    region: str
    time_utc: datetime
    message: str


@dataclass(frozen=True)
class CognitoReplicaState:
    """State of the replica regional attachment: the stack status and `ReplicaAttachmentStatus`."""

    stack_status: str | None
    attachment_status: str | None


@dataclass(frozen=True)
class CognitoIds:
    """Pool and app client ids of a region, read from SSM."""

    pool_id: str
    client_id: str


def reduce_to_names(text: str) -> str:
    """Replace every ARN by its resource name and every 12-digit number by a placeholder."""
    return _ACCOUNT_ID.sub(ACCOUNT_PLACEHOLDER, _ARN.sub(r"\1", text))


def _stack_name(region: str, kind: str, env_type: str) -> str:
    return f"stk-{region.replace('-', '')}-drdemo-{kind}-{env_type}"


def _cluster_name(region: str, env_type: str) -> str:
    return f"rdsc-{region.replace('-', '')}-drdemo-{env_type}"


def _aware(moment: datetime) -> datetime:
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


class AwsObservers:
    """The drill's read-only view of AWS; the profile and CA bundle come from the client factory."""

    def __init__(
        self,
        clients: ClientFactory,
        settings: Settings,
        guard: OutputGuard,
        hosted_zone_id: str | None = None,
    ) -> None:
        self._clients = clients
        self._settings = settings
        self._guard = guard
        self._zone_id = hosted_zone_id or None
        self._ids: dict[str, CognitoIds] = {}
        self._lock = threading.Lock()

    def _client(self, service: str, region: str) -> Any:
        return self._clients(service, region)

    @property
    def _regions(self) -> tuple[str, str]:
        return (self._settings.region_primary, self._settings.region_secondary)

    def fence_state(self, region: str) -> dict[str, int | None]:
        """Reserved concurrency of the six API functions of the region (None = not fenced)."""
        state: dict[str, int | None] = {}
        for descriptor in FUNCTION_DESCRIPTORS:
            name = function_name(region, descriptor, self._settings.env_type)
            answer = self._client("lambda", region).get_function_concurrency(FunctionName=name)
            value = answer.get("ReservedConcurrentExecutions")
            state[name] = None if value is None else int(value)
        return state

    def writer_region(self) -> str | None:
        """Region of the Aurora global cluster writer."""
        return AuroraWriter(self._clients, self._settings.region_primary).writer_region()

    def route53_weights(self) -> dict[str, int]:
        """Weight of each weighted `A` record of the API domain, by set identifier."""
        domain = self._settings.api_domain_name.rstrip(".")
        answer = self._client("route53", self._settings.region_global).list_resource_record_sets(
            HostedZoneId=self._hosted_zone_id(domain),
            StartRecordName=domain,
            StartRecordType="A",
            MaxItems="10",
        )
        weights: dict[str, int] = {}
        for record in answer.get("ResourceRecordSets", []):
            named = str(record.get("Name", "")).rstrip(".") == domain
            if named and record.get("Type") == "A" and "Weight" in record:
                weights[str(record.get("SetIdentifier", ""))] = int(record["Weight"])
        return weights

    def replication_lag(self, start: datetime, end: datetime) -> list[LagSample]:
        """Lag datapoints (60-second period, maximum) of the cluster in both regions."""
        samples: list[LagSample] = []
        for region in self._regions:
            cluster = _cluster_name(region, self._settings.env_type)
            for metric in LAG_METRICS:
                answer = self._client("cloudwatch", region).get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName=metric,
                    Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster}],
                    StartTime=start,
                    EndTime=end,
                    Period=LAG_PERIOD_SECONDS,
                    Statistics=["Maximum"],
                )
                samples.extend(
                    LagSample(region, metric, _aware(point["Timestamp"]), float(point["Maximum"]))
                    for point in answer.get("Datapoints", [])
                    if "Maximum" in point
                )
        return sorted(samples, key=lambda sample: (sample.time_utc, sample.region, sample.metric))

    def rds_events(self, region: str, start: datetime, end: datetime) -> list[RdsEvent]:
        """Events of the cluster in the region (switchover and failover included)."""
        cluster = _cluster_name(region, self._settings.env_type)
        events: list[RdsEvent] = []
        marker: str | None = None
        for _ in range(MAX_EVENT_PAGES):
            arguments: dict[str, Any] = {
                "SourceIdentifier": cluster,
                "SourceType": "db-cluster",
                "StartTime": start,
                "EndTime": end,
            }
            if marker:
                arguments["Marker"] = marker
            answer = self._client("rds", region).describe_events(**arguments)
            events.extend(
                RdsEvent(
                    region, _aware(item["Date"]), reduce_to_names(str(item.get("Message", "")))
                )
                for item in answer.get("Events", [])
            )
            marker = answer.get("Marker")
            if not marker:
                break
        return sorted(events, key=lambda event: event.time_utc)

    def cognito_replica_state(self) -> CognitoReplicaState:
        """Attachment status of the replica regional configuration (secondary region stack)."""
        region = self._settings.region_secondary
        stack = _stack_name(region, "cognito", self._settings.env_type)
        try:
            answer = self._client("cloudformation", region).describe_stacks(StackName=stack)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == STACK_MISSING_CODE:
                return CognitoReplicaState(None, None)
            raise
        described = answer["Stacks"][0]
        parameters = {
            item.get("ParameterKey"): item.get("ParameterValue")
            for item in described.get("Parameters", [])
        }
        attachment = parameters.get("ReplicaAttachmentStatus")
        status = described.get("StackStatus")
        return CognitoReplicaState(
            None if status is None else str(status),
            None if attachment is None else str(attachment),
        )

    def pool_and_client_ids(self, region: str) -> CognitoIds:
        """User pool and bank client ids of the region from SSM; registered with the guard."""
        with self._lock:
            known = self._ids.get(region)
        if known is not None:
            return known
        env_type = self._settings.env_type
        pool = self._parameter(region, USER_POOL_PARAMETER.format(env_type=env_type))
        client = self._parameter(region, CLIENT_PARAMETER.format(env_type=env_type))
        ids = CognitoIds(pool, client)
        with self._lock:
            self._ids[region] = ids
        return ids

    def client_id(self, region: str) -> str:
        """Bank app client id of the region (the client id provider of the Cognito session)."""
        return self.pool_and_client_ids(region).client_id

    def _parameter(self, region: str, name: str) -> str:
        answer = self._client("ssm", region).get_parameter(Name=name)
        value = str(answer["Parameter"]["Value"])
        self._guard.register(value)
        return value

    def _hosted_zone_id(self, domain: str) -> str:
        with self._lock:
            if self._zone_id:
                return self._zone_id
        labels = domain.split(".")
        for index in range(len(labels) - 1):
            candidate = ".".join(labels[index:])
            found = self._client("route53", self._settings.region_global).list_hosted_zones_by_name(
                DNSName=candidate, MaxItems="1"
            )
            for zone in found.get("HostedZones", []):
                if str(zone.get("Name", "")).rstrip(".") == candidate:
                    zone_id = str(zone["Id"]).rsplit("/", 1)[-1]
                    with self._lock:
                        self._zone_id = zone_id
                    return zone_id
        raise LookupError("no hosted zone matches the API domain")


def build_observers(
    settings: Settings,
    guard: OutputGuard,
    environ: Mapping[str, str],
    clients: ClientFactory | None = None,
) -> AwsObservers:
    """Observers over real read-only clients; `HOSTED_ZONE_ID` is taken from the environment."""
    make = clients or boto_client_factory(settings)
    return AwsObservers(make, settings, guard, environ.get("HOSTED_ZONE_ID"))
