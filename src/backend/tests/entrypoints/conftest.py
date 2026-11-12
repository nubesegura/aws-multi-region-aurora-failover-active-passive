import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from dr_demo.adapters.inbound.api_gateway import ApiGatewayController
from dr_demo.application.use_cases.create_transaction import CreateTransaction
from dr_demo.application.use_cases.get_account import GetAccount
from dr_demo.application.use_cases.get_consistency import GetConsistency
from dr_demo.application.use_cases.get_health import GetHealth
from dr_demo.application.use_cases.get_stats import GetStats
from dr_demo.application.use_cases.list_transactions import ListTransactions
from tests.fakes.in_memory import (
    FixedClock,
    FixedRegionProvider,
    InMemoryTransactionRepository,
    SequentialIdGenerator,
)

REGION = "us-east-2"
DASHBOARD_ORIGIN = "https://dashboard.example.com"
BANK_ORIGIN = "https://bank.example.com"
FOREIGN_ORIGIN = "https://evil.example.net"
BANK_CLIENT_ID = "cgnc-useast1-drdemo-bank-prod"
DASHBOARD_CLIENT_ID = "cgnc-useast1-drdemo-dash-prod"
DEFAULT_CLAIMS: dict[str, Any] = {
    "sub": "11111111-2222-3333-4444-555555555555",
    "cognito:username": "bank-operator",
    "email": "bank-operator@example.internal",
    "token_use": "id",
    "aud": BANK_CLIENT_ID,
}
NOW = datetime(2026, 10, 4, 12, 0, 0, 123_456, tzinfo=UTC)


@dataclass
class FakeContext:
    aws_request_id: str = "ctx-request-id"


@dataclass
class Wiring:
    controller: ApiGatewayController
    repository: InMemoryTransactionRepository
    logger: logging.Logger


@pytest.fixture
def context() -> FakeContext:
    return FakeContext()


@pytest.fixture
def wiring() -> Wiring:
    repository = InMemoryTransactionRepository()
    clock = FixedClock(NOW)
    region = FixedRegionProvider(REGION)
    logger = logging.getLogger("dr_demo.test")
    logger.setLevel(logging.INFO)
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
        allowed_client_ids=(BANK_CLIENT_ID, DASHBOARD_CLIENT_ID),
    )
    return Wiring(controller, repository, logger)


def post_event(
    body: object | str | None, headers: dict[str, str] | None = None, **extra: Any
) -> dict[str, Any]:
    raw = body if isinstance(body, str) or body is None else json.dumps(body)
    return {
        "httpMethod": "POST",
        "path": "/transactions",
        "headers": headers,
        "queryStringParameters": None,
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {"claims": dict(DEFAULT_CLAIMS)},
        },
        "body": raw,
        "isBase64Encoded": False,
        **extra,
    }


def get_event(
    path: str, query: dict[str, str] | None = None, origin: str | None = None
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if origin is not None:
        headers["Origin"] = origin
    return {
        "httpMethod": "GET",
        "path": path,
        "headers": headers,
        "queryStringParameters": query,
        "requestContext": {
            "requestId": "req-1",
            "authorizer": {"claims": dict(DEFAULT_CLAIMS)},
        },
        "body": None,
        "isBase64Encoded": False,
    }
