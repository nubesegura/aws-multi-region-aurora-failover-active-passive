import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dr_drill import observers
from dr_drill.observers import AwsObservers, build_observers, reduce_to_names
from dr_drill.redact import OutputGuard
from tests.fakes import make_settings
from tests.test_aws_ports import StubbedClients

START = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
END = datetime(2026, 11, 11, 12, 30, tzinfo=UTC)
READ_PREFIXES = ("describe_", "get_", "list_")
WRITE_VERBS = (
    "put",
    "update",
    "create",
    "delete",
    "invoke",
    "start",
    "stop",
    "modify",
    "failover",
    "switchover",
)


def make(clients: StubbedClients, zone: str | None = None) -> tuple[AwsObservers, OutputGuard]:
    guard = OutputGuard()
    return AwsObservers(clients, make_settings(), guard, zone), guard


def test_fence_state_reads_the_six_functions() -> None:
    clients = StubbedClients()
    stub = clients.stub("lambda", "us-east-2")
    for index in range(6):
        stub.add_response(
            "get_function_concurrency", {"ReservedConcurrentExecutions": 0} if index == 0 else {}
        )

    with clients.active():
        state = make(clients)[0].fence_state("us-east-2")

    assert len(state) == 6
    assert state["fnc-useast2-drdemo-create-transaction-prod"] == 0
    assert [value for name, value in state.items() if "create" not in name] == [None] * 5


def test_writer_region_is_read_from_the_global_cluster() -> None:
    clients = StubbedClients()
    members = [{"DBClusterArn": "arn:aws:rds:us-west-2:111:cluster:b", "IsWriter": True}]
    clients.stub("rds", "us-east-2").add_response(
        "describe_global_clusters",
        {"GlobalClusters": [{"GlobalClusterIdentifier": "g", "GlobalClusterMembers": members}]},
    )

    with clients.active():
        assert make(clients)[0].writer_region() == "us-west-2"


def _records(weights: dict[str, int], name: str = "api.example.invalid.") -> dict[str, object]:
    alias = {"HostedZoneId": "Z1", "DNSName": "x.example.invalid", "EvaluateTargetHealth": True}
    records = [
        {
            "Name": name,
            "Type": "A",
            "SetIdentifier": set_id,
            "Weight": weight,
            "AliasTarget": alias,
        }
        for set_id, weight in weights.items()
    ]
    records.append({"Name": name, "Type": "AAAA", "SetIdentifier": "v6", "Weight": 7})
    records.append({"Name": "other.example.invalid.", "Type": "A", "AliasTarget": alias})
    return {"ResourceRecordSets": records, "IsTruncated": False, "MaxItems": "10"}


def test_route53_weights_with_a_configured_zone() -> None:
    clients = StubbedClients()
    clients.stub("route53", "us-east-1").add_response(
        "list_resource_record_sets",
        _records({"use2": 255, "usw2": 0}),
        {
            "HostedZoneId": "ZCONFIGURED",
            "StartRecordName": "api.example.invalid",
            "StartRecordType": "A",
            "MaxItems": "10",
        },
    )

    with clients.active():
        weights = make(clients, "ZCONFIGURED")[0].route53_weights()

    assert weights == {"use2": 255, "usw2": 0}


def test_route53_weights_discovers_the_zone_by_walking_up_the_domain() -> None:
    clients = StubbedClients()
    stub = clients.stub("route53", "us-east-1")
    stub.add_response(
        "list_hosted_zones_by_name",
        {
            "HostedZones": [
                {"Id": "/hostedzone/Z9", "Name": "other.invalid.", "CallerReference": "r"}
            ],
            "DNSName": "api.example.invalid",
            "IsTruncated": False,
            "MaxItems": "1",
        },
        {"DNSName": "api.example.invalid", "MaxItems": "1"},
    )
    stub.add_response(
        "list_hosted_zones_by_name",
        {
            "HostedZones": [
                {"Id": "/hostedzone/ZFOUND", "Name": "example.invalid.", "CallerReference": "r"}
            ],
            "DNSName": "example.invalid",
            "IsTruncated": False,
            "MaxItems": "1",
        },
        {"DNSName": "example.invalid", "MaxItems": "1"},
    )
    stub.add_response("list_resource_record_sets", _records({"use2": 0, "usw2": 255}))
    stub.add_response("list_resource_record_sets", _records({"use2": 0, "usw2": 255}))
    watcher, _ = make(clients)

    with clients.active():
        assert watcher.route53_weights() == {"use2": 0, "usw2": 255}
        assert watcher.route53_weights() == {"use2": 0, "usw2": 255}


def test_route53_without_a_matching_zone_raises() -> None:
    clients = StubbedClients()
    stub = clients.stub("route53", "us-east-1")
    for name in ("api.example.invalid", "example.invalid"):
        stub.add_response(
            "list_hosted_zones_by_name",
            {"HostedZones": [], "DNSName": name, "IsTruncated": False, "MaxItems": "1"},
        )

    with clients.active(), pytest.raises(LookupError):
        make(clients)[0].route53_weights()


def test_replication_lag_reads_both_regions_and_both_metrics() -> None:
    clients = StubbedClients()
    for region, cluster, value in (
        ("us-east-2", "rdsc-useast2-drdemo-prod", 0.0),
        ("us-west-2", "rdsc-uswest2-drdemo-prod", 850.0),
    ):
        stub = clients.stub("cloudwatch", region)
        for metric, points in (
            (
                "AuroraGlobalDBReplicationLag",
                [{"Timestamp": END, "Maximum": value, "Unit": "Milliseconds"}],
            ),
            ("AuroraGlobalDBRPOLag", []),
        ):
            stub.add_response(
                "get_metric_statistics",
                {"Label": metric, "Datapoints": points},
                {
                    "Namespace": "AWS/RDS",
                    "MetricName": metric,
                    "Dimensions": [{"Name": "DBClusterIdentifier", "Value": cluster}],
                    "StartTime": START,
                    "EndTime": END,
                    "Period": 60,
                    "Statistics": ["Maximum"],
                },
            )

    with clients.active():
        samples = make(clients)[0].replication_lag(START, END)

    assert [(s.region, s.metric, s.milliseconds) for s in samples] == [
        ("us-east-2", "AuroraGlobalDBReplicationLag", 0.0),
        ("us-west-2", "AuroraGlobalDBReplicationLag", 850.0),
    ]
    assert samples[0].time_utc == END


def test_rds_events_are_reduced_to_names_and_paged() -> None:
    clients = StubbedClients()
    stub = clients.stub("rds", "us-west-2")
    message = (
        "Switchover of arn:aws:rds:us-west-2:123456789012:cluster:rdsc-uswest2-drdemo-prod done"
    )
    first = {
        "SourceIdentifier": "rdsc-uswest2-drdemo-prod",
        "SourceType": "db-cluster",
        "Message": message,
        "EventCategories": ["global-failover"],
        "Date": END,
        "SourceArn": "arn:aws:rds:us-west-2:123456789012:cluster:rdsc-uswest2-drdemo-prod",
    }
    second = {**first, "Message": "Started 123456789012", "Date": START}
    stub.add_response("describe_events", {"Events": [first], "Marker": "next"})
    stub.add_response(
        "describe_events",
        {"Events": [second]},
        {
            "SourceIdentifier": "rdsc-uswest2-drdemo-prod",
            "SourceType": "db-cluster",
            "StartTime": START,
            "EndTime": END,
            "Marker": "next",
        },
    )

    with clients.active():
        events = make(clients)[0].rds_events("us-west-2", START, END)

    assert [event.message for event in events] == [
        "Started <account>",
        "Switchover of rdsc-uswest2-drdemo-prod done",
    ]
    assert [event.time_utc for event in events] == [START, END]
    assert all(OutputGuard().reason(event.message) is None for event in events)


def _stack(parameters: list[dict[str, str]]) -> dict[str, object]:
    stack = {
        "StackName": "s",
        "CreationTime": START,
        "StackStatus": "UPDATE_COMPLETE",
        "Parameters": parameters,
    }
    return {"Stacks": [stack]}


def test_cognito_replica_state_reads_the_attachment_parameter() -> None:
    clients = StubbedClients()
    clients.stub("cloudformation", "us-west-2").add_response(
        "describe_stacks",
        _stack(
            [
                {"ParameterKey": "EnvType", "ParameterValue": "prod"},
                {"ParameterKey": "ReplicaAttachmentStatus", "ParameterValue": "ACTIVE"},
            ]
        ),
        {"StackName": "stk-uswest2-drdemo-cognito-prod"},
    )

    with clients.active():
        state = make(clients)[0].cognito_replica_state()

    assert (state.stack_status, state.attachment_status) == ("UPDATE_COMPLETE", "ACTIVE")


def test_cognito_replica_state_without_the_parameter_or_the_stack() -> None:
    clients = StubbedClients()
    stub = clients.stub("cloudformation", "us-west-2")
    stub.add_response("describe_stacks", _stack([]))
    stub.add_client_error("describe_stacks", "ValidationError", "does not exist")
    stub.add_client_error("describe_stacks", "AccessDenied", "no")
    watcher, _ = make(clients)

    with clients.active():
        assert watcher.cognito_replica_state().attachment_status is None
        assert watcher.cognito_replica_state().stack_status is None
        with pytest.raises(Exception, match="AccessDenied"):
            watcher.cognito_replica_state()


def test_pool_and_client_ids_are_read_once_and_registered_with_the_guard() -> None:
    clients = StubbedClients()
    stub = clients.stub("ssm", "us-west-2")
    stub.add_response(
        "get_parameter",
        {"Parameter": {"Value": "pool-value-for-test"}},
        {"Name": "/drdemo/prod/cognito/UserPoolId"},
    )
    stub.add_response(
        "get_parameter",
        {"Parameter": {"Value": "client-value-for-test"}},
        {"Name": "/drdemo/prod/cognito/BankClientId"},
    )
    watcher, guard = make(clients)

    with clients.active():
        ids = watcher.pool_and_client_ids("us-west-2")
        again = watcher.pool_and_client_ids("us-west-2")
        client = watcher.client_id("us-west-2")

    assert (ids.pool_id, ids.client_id) == ("pool-value-for-test", "client-value-for-test")
    assert again is ids
    assert client == "client-value-for-test"
    assert guard.reason("x pool-value-for-test x") is not None
    assert guard.reason("x client-value-for-test x") is not None


def test_reduce_to_names() -> None:
    text = "arn:aws:lambda:us-east-2:123456789012:function:fnc-a-prod and 111122223333 end"

    assert reduce_to_names(text) == "fnc-a-prod and <account> end"
    assert reduce_to_names("no secrets here") == "no secrets here"


def test_build_observers_reads_the_hosted_zone_from_the_environment() -> None:
    clients = StubbedClients()
    clients.stub("route53", "us-east-1").add_response(
        "list_resource_record_sets",
        _records({"use2": 1}),
        {
            "HostedZoneId": "ZENV",
            "StartRecordName": "api.example.invalid",
            "StartRecordType": "A",
            "MaxItems": "10",
        },
    )
    built = build_observers(
        make_settings(), OutputGuard(), {"HOSTED_ZONE_ID": "ZENV"}, clients=clients
    )

    with clients.active():
        assert built.route53_weights() == {"use2": 1}


def _client_call_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = ast.unparse(node.func.value)
            if "_client(" in receiver or "clients(" in receiver or receiver == "client":
                names.add(node.func.attr)
    return names


def test_every_aws_call_is_an_allow_listed_read_verb() -> None:
    names = _client_call_names(Path(observers.__file__).read_text(encoding="utf-8"))

    assert names == {
        "get_function_concurrency",
        "list_resource_record_sets",
        "list_hosted_zones_by_name",
        "get_metric_statistics",
        "describe_events",
        "describe_stacks",
        "get_parameter",
    }
    assert all(name.startswith(READ_PREFIXES) for name in names)
    assert not any(verb in name.split("_") for name in names for verb in WRITE_VERBS)


def test_the_allow_list_check_would_catch_write_verbs() -> None:
    source = "self._client('lambda', r).invoke(FunctionName=f)\nself._client('rds', r).failover_x()"

    names = _client_call_names(source)

    assert names == {"invoke", "failover_x"}
    assert not all(name.startswith(READ_PREFIXES) for name in names)
