"""Inbound adapter: API Gateway REST proxy events in, proxy responses out."""

import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from dr_demo.adapters.inbound.responses import (
    ApiResponse,
    account_json,
    consistency_json,
    created_json,
    error_response,
    health_json,
    proxy_response,
    stats_json,
    transaction_list_json,
)
from dr_demo.application.use_cases.create_transaction import CreateTransaction
from dr_demo.application.use_cases.get_account import GetAccount
from dr_demo.application.use_cases.get_consistency import GetConsistency
from dr_demo.application.use_cases.get_health import GetHealth
from dr_demo.application.use_cases.get_stats import GetStats
from dr_demo.application.use_cases.list_transactions import ListTransactions
from dr_demo.domain.errors import DomainError, FieldError, ValidationError

Event = Mapping[str, Any]
Action = Callable[[], ApiResponse]
_MAX_EXPONENT = 100
_ALLOWED_CLIENT_IDS_ENV = "COGNITO_ALLOWED_CLIENT_IDS"
_UNAUTHORIZED_CODE = "unauthorized"
_ID_CLAIM_USE = "id"


class LambdaContext(Protocol):
    aws_request_id: str


@dataclass(frozen=True)
class CallerIdentity:
    """Verified caller identity extracted from API Gateway Cognito authorizer claims."""

    sub: str
    username: str | None
    email: str | None
    token_use: str | None = None
    aud: str | None = None


class ApiGatewayController:
    """Parses events, calls one use case per route and shapes the HTTP result and the log line."""

    def __init__(
        self,
        create_transaction: CreateTransaction,
        list_transactions: ListTransactions,
        get_stats: GetStats,
        get_health: GetHealth,
        get_account: GetAccount,
        get_consistency: GetConsistency,
        region: str,
        allowed_origins: Collection[str],
        logger: logging.Logger,
        allowed_client_ids: Collection[str] | None = None,
    ) -> None:
        self._create_transaction = create_transaction
        self._list_transactions = list_transactions
        self._get_stats = get_stats
        self._get_health = get_health
        self._get_account = get_account
        self._get_consistency = get_consistency
        self._region = region
        self._allowed_origins = frozenset(allowed_origins)
        self._logger = logger
        self._allowed_client_ids = (
            frozenset(_clean_client_ids(allowed_client_ids))
            if allowed_client_ids is not None
            else None
        )

    def create_transaction(self, event: Event, context: LambdaContext) -> dict[str, Any]:
        started = time.perf_counter()

        def action() -> ApiResponse:
            payload = _parse_json_body(event)
            key = _headers(event).get("idempotency-key")
            result = self._create_transaction.execute(payload, key)
            server_ms = round((time.perf_counter() - started) * 1000, 1)
            return ApiResponse(
                201 if result.created else 200,
                created_json(result, self._region, server_ms),
                log_fields={
                    "movement_id": str(result.transaction.id),
                    "movement_type": result.transaction.type,
                },
            )

        return self._handle("POST /transactions", event, context, action)

    def list_transactions(self, event: Event, context: LambdaContext) -> dict[str, Any]:
        def action() -> ApiResponse:
            items = self._list_transactions.execute(_query(event).get("limit"))
            return ApiResponse(200, transaction_list_json(items))

        return self._handle("GET /transactions", event, context, action)

    def get_stats(self, event: Event, context: LambdaContext) -> dict[str, Any]:
        def action() -> ApiResponse:
            return ApiResponse(200, stats_json(self._get_stats.execute()))

        return self._handle("GET /stats", event, context, action)

    def get_account(self, event: Event, context: LambdaContext) -> dict[str, Any]:
        def action() -> ApiResponse:
            return ApiResponse(200, account_json(self._get_account.execute()))

        return self._handle("GET /account", event, context, action)

    def get_consistency(self, event: Event, context: LambdaContext) -> dict[str, Any]:
        def action() -> ApiResponse:
            return ApiResponse(200, consistency_json(self._get_consistency.execute()))

        return self._handle("GET /consistency", event, context, action)

    def get_health(self, event: Event, context: LambdaContext) -> dict[str, Any]:
        def action() -> ApiResponse:
            deep = _query(event).get("deep", "").lower() == "true"
            return ApiResponse(200, health_json(self._get_health.execute(deep=deep)))

        return self._handle("GET /health", event, context, action, require_auth=False)

    def _effective_allowed_client_ids(self) -> frozenset[str]:
        if self._allowed_client_ids is not None:
            return self._allowed_client_ids
        raw = os.environ.get(_ALLOWED_CLIENT_IDS_ENV, "")
        return frozenset(_clean_client_ids(raw.split(",")))

    def _is_authorized(self, caller: CallerIdentity | None) -> bool:
        allowed_client_ids = self._effective_allowed_client_ids()
        if not allowed_client_ids or caller is None:
            return False
        return (
            caller.token_use == _ID_CLAIM_USE
            and caller.aud is not None
            and caller.aud in allowed_client_ids
        )

    def _handle(
        self,
        route: str,
        event: Event,
        context: LambdaContext,
        action: Action,
        *,
        require_auth: bool = True,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        caller = extract_caller_identity(event)
        try:
            if require_auth and not self._is_authorized(caller):
                response = _unauthorized_response()
            else:
                response = action()
        except DomainError as error:
            response = error_response(error)
        except Exception as error:
            self._logger.exception(
                "unhandled error",
                extra={"fields": _log_fields(event, context, route, self._region, caller)},
            )
            response = error_response(error)
        self._logger.info(
            "request completed",
            extra={
                "fields": {
                    **_log_fields(event, context, route, self._region, caller),
                    **response.log_fields,
                    "status": response.status,
                    "error_code": response.error_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            },
        )
        return proxy_response(
            response, self._region, _headers(event).get("origin"), self._allowed_origins
        )


def extract_caller_identity(event: Event) -> CallerIdentity | None:
    """Extract caller identity claims from the API Gateway Cognito authorizer context."""
    claims = _extract_claims(event)
    if claims is None:
        return None
    sub = _clean_claim(claims.get("sub"))
    if sub is None:
        return None
    username = _clean_claim(claims.get("cognito:username")) or _clean_claim(claims.get("username"))
    email = _clean_claim(claims.get("email"))
    token_use = _clean_claim(claims.get("token_use"))
    aud = _clean_claim(claims.get("aud"))
    return CallerIdentity(
        sub=sub,
        username=username,
        email=email,
        token_use=token_use,
        aud=aud,
    )


def _extract_claims(event: Event) -> Mapping[str, Any] | None:
    request_context = event.get("requestContext")
    if not isinstance(request_context, Mapping):
        return None
    authorizer = request_context.get("authorizer")
    if not isinstance(authorizer, Mapping):
        return None
    claims = authorizer.get("claims")
    if not isinstance(claims, Mapping):
        return None
    return claims


def _clean_claim(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _clean_client_ids(items: Collection[str]) -> tuple[str, ...]:
    return tuple(
        stripped
        for raw in items
        if isinstance(raw, str)
        and (stripped := raw.strip())
        and stripped.isascii()
        and not any(ch.isspace() for ch in stripped)
    )


def _unauthorized_response() -> ApiResponse:
    return ApiResponse(
        401,
        {
            "error": {
                "code": _UNAUTHORIZED_CODE,
                "message": "Authentication required.",
                "fields": [],
            }
        },
        error_code=_UNAUTHORIZED_CODE,
    )


def _log_fields(
    event: Event,
    context: LambdaContext,
    route: str,
    region: str,
    caller: CallerIdentity | None = None,
) -> dict[str, Any]:
    request_context = event.get("requestContext")
    request_id = (
        request_context.get("requestId") if isinstance(request_context, Mapping) else None
    ) or getattr(context, "aws_request_id", None)
    return {
        "request_id": request_id,
        "region": region,
        "route": route,
        "caller_sub": caller.sub if caller is not None else None,
        "caller_username": caller.username if caller is not None else None,
        "caller_email": caller.email if caller is not None else None,
    }


def _headers(event: Event) -> dict[str, str]:
    raw = event.get("headers")
    if not isinstance(raw, Mapping):
        return {}
    return {str(name).lower(): str(value) for name, value in raw.items()}


def _query(event: Event) -> dict[str, str]:
    raw = event.get("queryStringParameters")
    if not isinstance(raw, Mapping):
        return {}
    return {str(name): str(value) for name, value in raw.items()}


def _parse_json_body(event: Event) -> object:
    raw = event.get("body")
    try:
        if raw is None:
            raise ValueError("empty body")
        text = (
            base64.b64decode(raw, validate=True).decode() if event.get("isBase64Encoded") else raw
        )
        return json.loads(text, parse_float=_parse_decimal, parse_constant=_reject_constant)
    except (ValueError, TypeError, ArithmeticError, RecursionError, binascii.Error) as error:
        raise ValidationError([FieldError("body", "must be valid JSON")]) from error


def _parse_decimal(text: str) -> Decimal:
    """Parse a JSON number exactly; an exponent far outside any valid amount is refused."""
    value = Decimal(text)
    if abs(value.adjusted()) > _MAX_EXPONENT:
        raise ValueError("number out of range")
    return value


def _reject_constant(name: str) -> None:
    raise ValueError(f"unsupported JSON constant {name}")
