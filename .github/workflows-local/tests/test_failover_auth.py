"""Automated mocked failover test suite for multi-region Cognito authentication continuity."""

from __future__ import annotations

import base64
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure `src/backend` is importable during tests.
BACKEND_DIR = Path(__file__).resolve().parents[3] / "src" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from dr_demo.adapters.inbound.api_gateway import ApiGatewayController
from dr_demo.application.use_cases.create_transaction import CreateTransaction
from dr_demo.application.use_cases.get_account import GetAccount
from dr_demo.application.use_cases.get_consistency import GetConsistency
from dr_demo.application.use_cases.get_health import GetHealth
from dr_demo.application.use_cases.get_stats import GetStats
from dr_demo.application.use_cases.list_transactions import ListTransactions
from dr_demo.config.logging import JsonFormatter
from tests.fakes.in_memory import (
    FixedClock,
    FixedRegionProvider,
    InMemoryTransactionRepository,
    SequentialIdGenerator,
)

USER_POOL_ID = "us-east-2_DrDemoPool1"
BANK_CLIENT_ID = "cgnc-useast1-drdemo-bank-prod"
DASHBOARD_CLIENT_ID = "cgnc-useast1-drdemo-dash-prod"
ALLOWED_CLIENT_IDS = (BANK_CLIENT_ID, DASHBOARD_CLIENT_ID)
BANK_ORIGIN = "https://bank.example.invalid"
DASHBOARD_ORIGIN = "https://dashboard.example.invalid"
TOKEN_SWITCH_BUDGET_SECONDS = 5.0


def _encode_jwt(claims: dict[str, Any]) -> str:
    header_b64 = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"{header_b64}.{payload_b64}.sig"


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        msg = "Malformed JWT"
        raise ValueError(msg)
    padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    raw: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    return raw


@dataclass
class FakeContext:
    aws_request_id: str = "ctx-failover-1"


@dataclass
class FakeStopwatch:
    """Deterministic fake clock measuring client failover re-authentication latency."""

    current_time: datetime = field(
        default_factory=lambda: datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
    )
    elapsed_seconds: float = 0.0

    def advance(self, seconds: float) -> None:
        self.elapsed_seconds += seconds
        self.current_time = self.current_time + timedelta(seconds=seconds)


class FakeCognitoMultiRegionCluster:
    """Simulates primary (us-east-2) and MRR replica (us-west-2) Cognito User Pools."""

    def __init__(self, stopwatch: FakeStopwatch) -> None:
        self.stopwatch = stopwatch
        self.primary_available = True
        self.replica_status = "INACTIVE"
        self.users: dict[str, dict[str, str]] = {
            "bank-operator": {
                "sub": "11111111-2222-3333-4444-555555555555",
                "username": "bank-operator",
                "email": "bank-operator@example.internal",
                "password": "Bank#Operator123",
            },
            "dashboard-viewer": {
                "sub": "66666666-7777-8888-9999-000000000000",
                "username": "dashboard-viewer",
                "email": "dashboard-viewer@example.internal",
                "password": "Dash#Viewer1234",
            },
        }
        self.auth_calls: list[tuple[str, str, str]] = []

    def set_replica_status(self, status: str) -> None:
        self.replica_status = status

    def initiate_auth(
        self,
        *,
        region: str,
        client_id: str,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        self.stopwatch.advance(0.18)
        self.auth_calls.append((region, client_id, username))
        if region == "us-east-2" and not self.primary_available:
            msg = "Primary Cognito endpoint unreachable in us-east-2"
            raise ConnectionError(msg)
        if region == "us-west-2" and self.replica_status != "ACTIVE":
            msg = "UserPoolReplica in us-west-2 is INACTIVE"
            raise PermissionError(msg)
        if client_id not in ALLOWED_CLIENT_IDS:
            msg = "Invalid ClientId"
            raise PermissionError(msg)
        user = self.users.get(username)
        if user is None or user["password"] != password:
            msg = "NotAuthorizedException: Incorrect username or password."
            raise PermissionError(msg)

        exp = int((self.stopwatch.current_time + timedelta(minutes=15)).timestamp())
        claims: dict[str, Any] = {
            "sub": user["sub"],
            "cognito:username": user["username"],
            "email": user["email"],
            "token_use": "id",
            "aud": client_id,
            "iss": f"https://cognito-idp.{region}.amazonaws.com/{USER_POOL_ID}",
            "exp": exp,
        }
        id_token = _encode_jwt(claims)
        return {
            "AuthenticationResult": {
                "IdToken": id_token,
                "AccessToken": f"access-{region}-{username}",
                "RefreshToken": f"refresh-{region}-{username}",
                "ExpiresIn": 900,
            }
        }


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, Any]] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


class RegionalApiGatewaySimulator:
    """Simulates regional API Gateway Cognito Authorizer + Backend Lambda controller."""

    def __init__(
        self,
        *,
        data_region: str,
        expected_issuer_region: str,
        repository: InMemoryTransactionRepository,
        clock: FixedClock,
        id_gen: SequentialIdGenerator,
    ) -> None:
        self.data_region = data_region
        self.expected_issuer_region = expected_issuer_region
        self.repository = repository
        self.capture = LogCapture()
        logger = logging.getLogger(f"dr_demo.failover.{data_region}.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(self.capture)
        region_provider = FixedRegionProvider(data_region)
        self.controller = ApiGatewayController(
            create_transaction=CreateTransaction(repository, clock, id_gen, region_provider),
            list_transactions=ListTransactions(repository),
            get_stats=GetStats(repository, clock, region_provider),
            get_health=GetHealth(repository, region_provider),
            get_account=GetAccount(repository, region_provider),
            get_consistency=GetConsistency(repository, region_provider),
            region=data_region,
            allowed_origins=(BANK_ORIGIN, DASHBOARD_ORIGIN),
            logger=logger,
            allowed_client_ids=ALLOWED_CLIENT_IDS,
        )

    def send(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str | None,
        origin: str = BANK_ORIGIN,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        req_headers = {"Origin": origin, **(headers or {})}
        if path != "/health":
            if not bearer_token:
                return {"statusCode": 401, "body": json.dumps({"message": "Unauthorized"})}
            claims = _decode_jwt_claims(bearer_token)
            expected_iss = (
                f"https://cognito-idp.{self.expected_issuer_region}.amazonaws.com/{USER_POOL_ID}"
            )
            if claims.get("iss") != expected_iss:
                return {
                    "statusCode": 401,
                    "body": json.dumps({"message": "Unauthorized: issuer mismatch"}),
                }
            request_context: dict[str, Any] = {
                "requestId": f"req-{self.data_region}",
                "authorizer": {"claims": claims},
            }
        else:
            request_context = {"requestId": f"req-{self.data_region}"}

        event: dict[str, Any] = {
            "httpMethod": method,
            "path": path,
            "headers": req_headers,
            "queryStringParameters": None,
            "requestContext": request_context,
            "body": None if body is None else json.dumps(body),
            "isBase64Encoded": False,
        }
        ctx = FakeContext()
        if method == "POST" and path == "/transactions":
            return self.controller.create_transaction(event, ctx)
        if method == "GET" and path == "/transactions":
            return self.controller.list_transactions(event, ctx)
        if method == "GET" and path == "/account":
            return self.controller.get_account(event, ctx)
        if method == "GET" and path == "/stats":
            return self.controller.get_stats(event, ctx)
        if method == "GET" and path == "/consistency":
            return self.controller.get_consistency(event, ctx)
        return self.controller.get_health(event, ctx)


class FailoverAwareFrontendClient:
    """Models the frontend client failover and transparent background re-authentication logic."""

    def __init__(
        self,
        *,
        client_id: str,
        origin: str,
        cognito: FakeCognitoMultiRegionCluster,
        stopwatch: FakeStopwatch,
    ) -> None:
        self.client_id = client_id
        self.origin = origin
        self.cognito = cognito
        self.stopwatch = stopwatch
        self.active_auth_region = "us-east-2"
        self.id_token: str | None = None
        self._memory_credentials: tuple[str, str] | None = None
        self.relogin_prompts_shown = 0

    def sign_in(self, username: str, password: str) -> None:
        result = self.cognito.initiate_auth(
            region="us-east-2",
            client_id=self.client_id,
            username=username,
            password=password,
        )
        self._memory_credentials = (username, password)
        self.active_auth_region = "us-east-2"
        self.id_token = result["AuthenticationResult"]["IdToken"]

    def _alternate_region(self) -> str:
        return "us-west-2" if self.active_auth_region == "us-east-2" else "us-east-2"

    def _failover_reauthenticate(self) -> float:
        if self._memory_credentials is None:
            self.relogin_prompts_shown += 1
            msg = "Missing in-memory session credentials"
            raise RuntimeError(msg)
        started = self.stopwatch.elapsed_seconds
        target_region = self._alternate_region()
        username, password = self._memory_credentials
        result = self.cognito.initiate_auth(
            region=target_region,
            client_id=self.client_id,
            username=username,
            password=password,
        )
        self.active_auth_region = target_region
        self.id_token = result["AuthenticationResult"]["IdToken"]
        return self.stopwatch.elapsed_seconds - started

    def request(
        self,
        dns_target: RegionalApiGatewaySimulator,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], float | None]:
        self.stopwatch.advance(0.05)
        response = dns_target.send(
            method,
            path,
            bearer_token=self.id_token,
            origin=self.origin,
            headers=headers,
            body=body,
        )
        status = int(response["statusCode"])
        if status in (401, 500, 502, 503, 504):
            switch_seconds = self._failover_reauthenticate()
            self.stopwatch.advance(0.05)
            retry_response = dns_target.send(
                method,
                path,
                bearer_token=self.id_token,
                origin=self.origin,
                headers=headers,
                body=body,
            )
            return retry_response, switch_seconds
        return response, None


def test_full_six_step_multi_region_failover_and_switchback_lifecycle() -> None:
    stopwatch = FakeStopwatch()
    cognito = FakeCognitoMultiRegionCluster(stopwatch)
    shared_ledger = InMemoryTransactionRepository(role="writer")
    clock = FixedClock(stopwatch.current_time)
    id_gen = SequentialIdGenerator()

    primary_api = RegionalApiGatewaySimulator(
        data_region="us-east-2",
        expected_issuer_region="us-east-2",
        repository=shared_ledger,
        clock=clock,
        id_gen=id_gen,
    )
    standby_api = RegionalApiGatewaySimulator(
        data_region="us-west-2",
        expected_issuer_region="us-west-2",
        repository=shared_ledger,
        clock=clock,
        id_gen=id_gen,
    )

    bank_client = FailoverAwareFrontendClient(
        client_id=BANK_CLIENT_ID,
        origin=BANK_ORIGIN,
        cognito=cognito,
        stopwatch=stopwatch,
    )
    dashboard_client = FailoverAwareFrontendClient(
        client_id=DASHBOARD_CLIENT_ID,
        origin=DASHBOARD_ORIGIN,
        cognito=cognito,
        stopwatch=stopwatch,
    )

    # Step 1: Authenticate both users against primary Cognito (us-east-2)
    bank_client.sign_in("bank-operator", "Bank#Operator123")
    dashboard_client.sign_in("dashboard-viewer", "Dash#Viewer1234")
    assert bank_client.active_auth_region == "us-east-2"
    assert dashboard_client.active_auth_region == "us-east-2"

    # Step 2: Primary API requests succeed and structured claims are logged
    deposit_resp, switch_time = bank_client.request(
        primary_api,
        "POST",
        "/transactions",
        headers={"Idempotency-Key": "idem-primary-1"},
        body={"type": "deposit", "amount": 150.00},
    )
    assert deposit_resp["statusCode"] == 201
    assert switch_time is None
    deposit_body = json.loads(deposit_resp["body"])
    assert deposit_body["balance"] == "150.00"
    assert deposit_body["served_by_region"] == "us-east-2"

    stats_resp, _ = dashboard_client.request(primary_api, "GET", "/stats")
    assert stats_resp["statusCode"] == 200
    assert json.loads(stats_resp["body"])["total_count"] == 1

    primary_logs = primary_api.capture.lines
    assert any(
        line.get("caller_username") == "bank-operator"
        and line.get("caller_sub") == "11111111-2222-3333-4444-555555555555"
        and line.get("caller_email") == "bank-operator@example.internal"
        for line in primary_logs
    )
    assert any(
        line.get("caller_username") == "dashboard-viewer"
        and line.get("caller_sub") == "66666666-7777-8888-9999-000000000000"
        for line in primary_logs
    )
    raw_log_dump = json.dumps(primary_logs)
    assert "Bank#Operator123" not in raw_log_dump
    assert "Dash#Viewer1234" not in raw_log_dump

    # Step 3: Simulate primary outage, activate Cognito replica in us-west-2, and switch DNS
    cognito.primary_available = False
    cognito.set_replica_status("ACTIVE")

    # Step 4 & 5: Clients hit standby API (us-west-2) and acquire us-west-2 tokens in <5s
    failover_deposit, bank_switch_sec = bank_client.request(
        standby_api,
        "POST",
        "/transactions",
        headers={"Idempotency-Key": "idem-standby-1"},
        body={"type": "withdrawal", "amount": 25.00},
    )
    assert failover_deposit["statusCode"] == 201
    assert bank_switch_sec is not None
    assert bank_switch_sec < TOKEN_SWITCH_BUDGET_SECONDS
    assert bank_client.active_auth_region == "us-west-2"
    assert bank_client.relogin_prompts_shown == 0
    standby_body = json.loads(failover_deposit["body"])
    assert standby_body["balance"] == "125.00"
    assert standby_body["served_by_region"] == "us-west-2"

    standby_stats, dash_switch_sec = dashboard_client.request(standby_api, "GET", "/stats")
    assert standby_stats["statusCode"] == 200
    assert dash_switch_sec is not None
    assert dash_switch_sec < TOKEN_SWITCH_BUDGET_SECONDS
    assert dashboard_client.active_auth_region == "us-west-2"
    assert dashboard_client.relogin_prompts_shown == 0

    consistency_resp, _ = dashboard_client.request(standby_api, "GET", "/consistency")
    assert consistency_resp["statusCode"] == 200
    assert json.loads(consistency_resp["body"])["consistent"] is True

    # Step 6: Planned switchback to us-east-2 (primary restored, replica set INACTIVE)
    cognito.primary_available = True
    cognito.set_replica_status("INACTIVE")

    switchback_deposit, switchback_sec = bank_client.request(
        primary_api,
        "POST",
        "/transactions",
        headers={"Idempotency-Key": "idem-switchback-1"},
        body={"type": "deposit", "amount": 50.00},
    )
    assert switchback_deposit["statusCode"] == 201
    assert switchback_sec is not None
    assert switchback_sec < TOKEN_SWITCH_BUDGET_SECONDS
    assert bank_client.active_auth_region == "us-east-2"
    assert bank_client.relogin_prompts_shown == 0
    assert json.loads(switchback_deposit["body"])["balance"] == "175.00"

    switchback_account, dash_switchback_sec = dashboard_client.request(
        primary_api, "GET", "/account"
    )
    assert switchback_account["statusCode"] == 200
    assert dash_switchback_sec is not None
    assert dash_switchback_sec < TOKEN_SWITCH_BUDGET_SECONDS
    assert dashboard_client.active_auth_region == "us-east-2"
    assert json.loads(switchback_account["body"])["balance"] == "175.00"


def test_standby_rejects_authentication_when_replica_is_inactive() -> None:
    stopwatch = FakeStopwatch()
    cognito = FakeCognitoMultiRegionCluster(stopwatch)
    with pytest.raises(PermissionError, match="INACTIVE"):
        cognito.initiate_auth(
            region="us-west-2",
            client_id=BANK_CLIENT_ID,
            username="bank-operator",
            password="Bank#Operator123",
        )
