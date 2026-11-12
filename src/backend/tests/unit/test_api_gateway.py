"""Unit tests for Cognito claim extraction, defense-in-depth validation, and identity logging."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dr_demo.adapters.inbound.api_gateway import ApiGatewayController, extract_caller_identity
from dr_demo.application.use_cases.create_transaction import CreateTransaction
from dr_demo.application.use_cases.get_account import GetAccount
from dr_demo.application.use_cases.get_consistency import GetConsistency
from dr_demo.application.use_cases.get_health import GetHealth
from dr_demo.application.use_cases.get_stats import GetStats
from dr_demo.application.use_cases.list_transactions import ListTransactions
from dr_demo.config.environment import (
    COGNITO_ALLOWED_CLIENT_IDS_ENV,
    get_allowed_client_ids,
    parse_allowed_client_ids,
)
from dr_demo.config.logging import JsonFormatter
from dr_demo.config.settings import Settings
from tests.fakes.in_memory import (
    FixedClock,
    FixedRegionProvider,
    InMemoryTransactionRepository,
    SequentialIdGenerator,
)

REGION = "us-east-2"
BANK_ORIGIN = "https://bank.example.com"
DASHBOARD_ORIGIN = "https://dashboard.example.com"
FOREIGN_ORIGIN = "https://attacker.example.net"
BANK_CLIENT_ID = "cgnc-useast1-drdemo-bank-prod"
DASHBOARD_CLIENT_ID = "cgnc-useast1-drdemo-dash-prod"
ALLOWED_CLIENTS = (BANK_CLIENT_ID, DASHBOARD_CLIENT_ID)
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

VALID_BANK_CLAIMS: dict[str, Any] = {
    "sub": "11111111-2222-3333-4444-555555555555",
    "cognito:username": "bank-operator",
    "email": "bank-operator@example.internal",
    "token_use": "id",
    "aud": BANK_CLIENT_ID,
}


@dataclass
class FakeContext:
    aws_request_id: str = "ctx-req-1"


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, Any]] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


def _make_controller(
    allowed_client_ids: tuple[str, ...] | None = ALLOWED_CLIENTS,
) -> tuple[ApiGatewayController, InMemoryTransactionRepository, LogCapture]:
    repository = InMemoryTransactionRepository()
    clock = FixedClock(NOW)
    region = FixedRegionProvider(REGION)
    logger = logging.getLogger(f"dr_demo.unit.{id(repository)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    capture = LogCapture()
    logger.addHandler(capture)
    controller = ApiGatewayController(
        create_transaction=CreateTransaction(repository, clock, SequentialIdGenerator(), region),
        list_transactions=ListTransactions(repository),
        get_stats=GetStats(repository, clock, region),
        get_health=GetHealth(repository, region),
        get_account=GetAccount(repository, region),
        get_consistency=GetConsistency(repository, region),
        region=REGION,
        allowed_origins=(BANK_ORIGIN, DASHBOARD_ORIGIN),
        logger=logger,
        allowed_client_ids=allowed_client_ids,
    )
    return controller, repository, capture


def _event(
    method: str,
    path: str,
    *,
    claims: Any = VALID_BANK_CLAIMS,
    body: dict[str, Any] | None = None,
    origin: str | None = BANK_ORIGIN,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    request_context: Any = ...,
) -> dict[str, Any]:
    merged_headers: dict[str, str] = {}
    if origin is not None:
        merged_headers["Origin"] = origin
    if headers:
        merged_headers.update(headers)
    if request_context is ...:
        request_context = (
            {"requestId": "req-unit-1", "authorizer": {"claims": claims}}
            if claims is not None
            else {"requestId": "req-unit-1"}
        )
    return {
        "httpMethod": method,
        "path": path,
        "headers": merged_headers,
        "queryStringParameters": query,
        "requestContext": request_context,
        "body": None if body is None else json.dumps(body),
        "isBase64Encoded": False,
    }


def test_extract_caller_identity_reads_cognito_username_and_falls_back_to_username() -> None:
    primary_event = _event("GET", "/account", claims=VALID_BANK_CLAIMS)
    caller = extract_caller_identity(primary_event)

    assert caller is not None
    assert caller.sub == "11111111-2222-3333-4444-555555555555"
    assert caller.username == "bank-operator"
    assert caller.email == "bank-operator@example.internal"
    assert caller.token_use == "id"
    assert caller.aud == BANK_CLIENT_ID

    fallback_claims = {
        "sub": "99999999-8888-7777-6666-555555555555",
        "username": "dashboard-viewer",
        "token_use": "id",
        "aud": DASHBOARD_CLIENT_ID,
    }
    fallback_caller = extract_caller_identity(_event("GET", "/stats", claims=fallback_claims))

    assert fallback_caller is not None
    assert fallback_caller.sub == "99999999-8888-7777-6666-555555555555"
    assert fallback_caller.username == "dashboard-viewer"
    assert fallback_caller.email is None


def test_parse_allowed_client_ids_from_env_and_settings(tmp_path: Path) -> None:
    raw = f" {BANK_CLIENT_ID} , {DASHBOARD_CLIENT_ID} "
    assert parse_allowed_client_ids(raw) == (BANK_CLIENT_ID, DASHBOARD_CLIENT_ID)
    assert parse_allowed_client_ids(None) == ()
    assert parse_allowed_client_ids("") == ()
    assert parse_allowed_client_ids(" , ") == ()
    assert get_allowed_client_ids({COGNITO_ALLOWED_CLIENT_IDS_ENV: raw}) == (
        BANK_CLIENT_ID,
        DASHBOARD_CLIENT_ID,
    )

    bundle = tmp_path / "bundle.pem"
    bundle.write_text("placeholder", encoding="utf-8")
    settings = Settings.from_env(
        {
            "DB_HOST": "db.example.internal",
            "DB_NAME": "drdemo",
            "DB_USER": "app_user",
            "AWS_REGION": REGION,
            "ALLOWED_ORIGINS": f"{BANK_ORIGIN},{DASHBOARD_ORIGIN}",
            "DB_CA_BUNDLE_PATH": str(bundle),
            COGNITO_ALLOWED_CLIENT_IDS_ENV: raw,
        }
    )
    assert settings.allowed_client_ids == (BANK_CLIENT_ID, DASHBOARD_CLIENT_ID)


def test_protected_routes_succeed_with_valid_claims() -> None:
    controller, repository, _ = _make_controller()
    ctx = FakeContext()

    post_res = controller.create_transaction(
        _event("POST", "/transactions", body={"type": "deposit", "amount": 25.00}),
        ctx,
    )
    list_res = controller.list_transactions(_event("GET", "/transactions"), ctx)
    account_res = controller.get_account(_event("GET", "/account"), ctx)
    dash_claims = {
        "sub": "22222222-3333-4444-5555-666666666666",
        "cognito:username": "dashboard-viewer",
        "email": "dashboard-viewer@example.internal",
        "token_use": "id",
        "aud": DASHBOARD_CLIENT_ID,
    }
    stats_res = controller.get_stats(_event("GET", "/stats", claims=dash_claims), ctx)
    consistency_res = controller.get_consistency(
        _event("GET", "/consistency", claims=dash_claims), ctx
    )

    assert post_res["statusCode"] == 201
    assert list_res["statusCode"] == 200
    assert account_res["statusCode"] == 200
    assert stats_res["statusCode"] == 200
    assert consistency_res["statusCode"] == 200
    assert len(repository.stored) == 1


@pytest.mark.parametrize(
    "bad_event_kwargs",
    [
        {"request_context": None},
        {"request_context": "not-a-mapping"},
        {"request_context": {"requestId": "r1"}},
        {"request_context": {"requestId": "r1", "authorizer": None}},
        {"request_context": {"requestId": "r1", "authorizer": "not-a-mapping"}},
        {"request_context": {"requestId": "r1", "authorizer": {"claims": None}}},
        {"request_context": {"requestId": "r1", "authorizer": {"claims": "not-a-dict"}}},
        {"request_context": {"requestId": "r1", "authorizer": {"claims": []}}},
        {"claims": {}},
        {"claims": {**VALID_BANK_CLAIMS, "token_use": "access"}},
        {"claims": {**VALID_BANK_CLAIMS, "token_use": None}},
        {"claims": {**VALID_BANK_CLAIMS, "aud": "unknown-client-id"}},
        {"claims": {**VALID_BANK_CLAIMS, "aud": ""}},
        {"claims": {k: v for k, v in VALID_BANK_CLAIMS.items() if k != "aud"}},
        {"claims": {**VALID_BANK_CLAIMS, "sub": ""}},
        {"claims": {**VALID_BANK_CLAIMS, "sub": "   "}},
        {"claims": {k: v for k, v in VALID_BANK_CLAIMS.items() if k != "sub"}},
    ],
)
@pytest.mark.parametrize(
    "route_name",
    ["create_transaction", "list_transactions", "get_account", "get_stats", "get_consistency"],
)
def test_protected_routes_reject_missing_or_invalid_claims_with_cors_headers(
    route_name: str,
    bad_event_kwargs: dict[str, Any],
) -> None:
    controller, repository, _ = _make_controller()
    ctx = FakeContext()
    handler = getattr(controller, route_name)
    event = _event(
        "POST" if route_name == "create_transaction" else "GET",
        "/transactions" if "transaction" in route_name else f"/{route_name.removeprefix('get_')}",
        body={"type": "deposit", "amount": 10.0} if route_name == "create_transaction" else None,
        origin=BANK_ORIGIN,
        **bad_event_kwargs,
    )

    result = handler(event, ctx)

    assert result["statusCode"] in (401, 403)
    body = json.loads(result["body"])
    assert "error" in body
    assert result["headers"]["Access-Control-Allow-Origin"] == BANK_ORIGIN
    assert result["headers"]["Vary"] == "Origin"
    assert result["headers"]["X-Served-By-Region"] == REGION
    assert repository.stored == []


def test_protected_route_omits_cors_allow_header_for_foreign_origin_on_auth_rejection() -> None:
    controller, _, _ = _make_controller()
    event = _event("GET", "/account", claims=None, origin=FOREIGN_ORIGIN)

    result = controller.get_account(event, FakeContext())

    assert result["statusCode"] in (401, 403)
    assert "Access-Control-Allow-Origin" not in result["headers"]
    assert result["headers"]["Vary"] == "Origin"


def test_protected_routes_fail_closed_when_allowed_client_ids_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(COGNITO_ALLOWED_CLIENT_IDS_ENV, raising=False)
    controller, repository, _ = _make_controller(allowed_client_ids=())

    result = controller.create_transaction(
        _event("POST", "/transactions", body={"type": "deposit", "amount": 10.0}),
        FakeContext(),
    )

    assert result["statusCode"] in (401, 403)
    assert repository.stored == []


def test_controller_reads_allowed_client_ids_from_environment_when_not_explicitly_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(COGNITO_ALLOWED_CLIENT_IDS_ENV, f"{BANK_CLIENT_ID},{DASHBOARD_CLIENT_ID}")
    controller, _, _ = _make_controller(allowed_client_ids=None)

    result = controller.get_account(_event("GET", "/account"), FakeContext())

    assert result["statusCode"] == 200


def test_structured_identity_logging_includes_caller_fields_and_never_logs_tokens() -> None:
    controller, repository, capture = _make_controller()
    raw_jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.secret-signature"
    event = _event(
        "POST",
        "/transactions",
        body={"type": "deposit", "amount": 50.25},
        headers={
            "Authorization": f"Bearer {raw_jwt}",
            "Idempotency-Key": "idem-audit-1",
        },
    )

    result = controller.create_transaction(event, FakeContext())

    assert result["statusCode"] == 201
    assert len(capture.lines) == 1
    log_entry = capture.lines[0]
    assert log_entry["route"] == "POST /transactions"
    assert log_entry["status"] == 201
    assert log_entry["movement_id"] == str(repository.stored[0].id)
    assert log_entry["movement_type"] == "deposit"
    assert log_entry["caller_sub"] == VALID_BANK_CLAIMS["sub"]
    assert log_entry["caller_username"] == VALID_BANK_CLAIMS["cognito:username"]
    assert log_entry["caller_email"] == VALID_BANK_CLAIMS["email"]

    dumped = json.dumps(capture.lines)
    assert raw_jwt not in dumped
    assert "secret-signature" not in dumped
    assert "idem-audit-1" not in dumped


def test_unauthenticated_health_endpoint_succeeds_without_claims() -> None:
    controller, _, capture = _make_controller(allowed_client_ids=())
    shallow_event = _event("GET", "/health", claims=None)
    deep_event = _event("GET", "/health", claims=None, query={"deep": "true"})

    shallow_res = controller.get_health(shallow_event, FakeContext())
    deep_res = controller.get_health(deep_event, FakeContext())

    assert shallow_res["statusCode"] == 200
    assert json.loads(shallow_res["body"]) == {"status": "ok", "region": REGION}
    assert deep_res["statusCode"] == 200
    assert json.loads(deep_res["body"])["database_role"] == "writer"
    assert capture.lines[0]["caller_sub"] is None
    assert capture.lines[0]["caller_username"] is None
    assert capture.lines[0]["caller_email"] is None
