"""HTTP response mapping for API Gateway REST proxy integrations."""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from dr_demo.application.use_cases.create_transaction import CreateTransactionResult
from dr_demo.application.use_cases.get_account import AccountResult
from dr_demo.application.use_cases.get_consistency import ConsistencyResult
from dr_demo.application.use_cases.get_health import HealthResult
from dr_demo.application.use_cases.get_stats import StatsResult
from dr_demo.domain.errors import (
    DatabaseBusy,
    DatabaseReadOnly,
    DatabaseUnavailable,
    FieldError,
    InsufficientFunds,
    ValidationError,
)
from dr_demo.domain.transaction import Transaction

SERVED_BY_HEADER = "X-Served-By-Region"
RETRY_AFTER_SECONDS = "5"

VALIDATION_ERROR = "validation_error"
INSUFFICIENT_FUNDS = "insufficient_funds"
DATABASE_READ_ONLY = "database_read_only"
DATABASE_BUSY = "database_busy"
DATABASE_UNAVAILABLE = "database_unavailable"
INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)
    error_code: str | None = None
    log_fields: Mapping[str, Any] = field(default_factory=dict)


def proxy_response(
    response: ApiResponse,
    region: str,
    request_origin: str | None,
    allowed_origins: Collection[str],
) -> dict[str, Any]:
    """Build the API Gateway proxy result with the headers every response carries.

    The request ``Origin`` is echoed only when it is in the allow-list; ``Vary: Origin`` is
    always present so caches keep the variants apart.
    """
    cors = (
        {"Access-Control-Allow-Origin": request_origin}
        if request_origin is not None and request_origin in allowed_origins
        else {}
    )
    headers = {
        "Content-Type": "application/json",
        SERVED_BY_HEADER: region,
        "Access-Control-Expose-Headers": f"{SERVED_BY_HEADER}, Retry-After",
        "Vary": "Origin",
        **cors,
        **response.headers,
    }
    return {
        "statusCode": response.status,
        "headers": headers,
        "body": json.dumps(response.body, separators=(",", ":")),
    }


def error_response(error: Exception) -> ApiResponse:
    """Map an error to the contract; connection details and stack traces are never exposed."""
    if isinstance(error, ValidationError):
        return _error(400, VALIDATION_ERROR, "The request is not valid.", error.fields)
    if isinstance(error, InsufficientFunds):
        return _error(
            422,
            INSUFFICIENT_FUNDS,
            "The withdrawal is above the current balance.",
            extra={"balance": money(error.balance)},
        )
    if isinstance(error, DatabaseReadOnly):
        return _error(
            503,
            DATABASE_READ_ONLY,
            "The database in this region is read-only. Retry shortly.",
            headers={"Retry-After": RETRY_AFTER_SECONDS},
        )
    if isinstance(error, DatabaseBusy):
        return _error(
            503,
            DATABASE_BUSY,
            "The account is busy. Retry shortly.",
            headers={"Retry-After": RETRY_AFTER_SECONDS},
        )
    if isinstance(error, DatabaseUnavailable):
        return _error(503, DATABASE_UNAVAILABLE, "The database is not reachable.")
    return _error(500, INTERNAL_ERROR, "Unexpected error.")


def _error(
    status: int,
    code: str,
    message: str,
    fields: tuple[FieldError, ...] = (),
    headers: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> ApiResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "fields": [{"field": item.field, "message": item.message} for item in fields],
            **(extra or {}),
        }
    }
    return ApiResponse(status, body, headers or {}, error_code=code)


def format_time(moment: datetime) -> str:
    """ISO 8601 UTC with millisecond precision, for example ``2026-10-04T12:00:00.123Z``."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def money(value: Decimal) -> str:
    """Exact two-decimal text for an amount or a balance; never goes through ``float``."""
    return f"{value:.2f}"


def transaction_json(transaction: Transaction) -> dict[str, Any]:
    return {
        "id": str(transaction.id),
        "type": transaction.type,
        "amount": money(transaction.amount),
        "balance_after": money(transaction.balance_after),
        "processed_at": format_time(transaction.processed_at),
        "processed_region": transaction.processed_region,
    }


def transaction_list_json(transactions: tuple[Transaction, ...]) -> dict[str, Any]:
    return {"items": [transaction_json(item) for item in transactions]}


def created_json(
    result: CreateTransactionResult, served_by_region: str, server_ms: float
) -> dict[str, Any]:
    return {
        "transaction": transaction_json(result.transaction),
        "balance": money(result.balance),
        "served_by_region": served_by_region,
        "database_role": "writer",
        "server_ms": server_ms,
    }


def account_json(account: AccountResult) -> dict[str, Any]:
    return {
        "balance": money(account.balance),
        "movement_count": account.movement_count,
        "served_by_region": account.served_by_region,
        "database_role": account.database_role,
    }


def consistency_json(result: ConsistencyResult) -> dict[str, Any]:
    report = result.report
    return {
        "count": report.count,
        "total_deposited": money(report.total_deposited),
        "total_withdrawn": money(report.total_withdrawn),
        "replayed_balance": money(report.replayed_balance),
        "stored_balance": money(report.stored_balance),
        "balances_match": report.balances_match,
        "chain_ok": report.chain_ok,
        "duplicate_keys": report.duplicate_keys,
        "counts_by_region": dict(report.counts_by_region),
        "consistent": report.consistent,
        "first_offender": None if report.first_offender is None else str(report.first_offender),
        "truncated": report.truncated,
        "served_by_region": result.served_by_region,
    }


def stats_json(stats: StatsResult) -> dict[str, Any]:
    return {
        "total_count": stats.total_count,
        "counts_by_region": dict(stats.counts_by_region),
        "last_processed_at_by_region": {
            region: format_time(moment)
            for region, moment in stats.last_processed_at_by_region.items()
        },
        "latest_transactions": [transaction_json(item) for item in stats.latest],
        "served_by_region": stats.served_by_region,
        "database_role": stats.database_role,
        "generated_at": format_time(stats.generated_at),
    }


def health_json(health: HealthResult) -> dict[str, Any]:
    body: dict[str, Any] = {"status": health.status, "region": health.region}
    if health.database_role is not None:
        body["database_role"] = health.database_role
    return body
