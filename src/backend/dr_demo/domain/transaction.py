"""Movement entity, input validation and read models."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, get_args
from uuid import UUID

from dr_demo.domain.errors import FieldError, ValidationError

DatabaseRole = Literal["writer", "reader"]
MovementType = Literal["deposit", "withdrawal"]

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
LATEST_COUNT = 10
MAX_AMOUNT = Decimal("9999999999.99")
MAX_IDEMPOTENCY_KEY_LENGTH = 64
IDEMPOTENCY_KEY_FIELD = "Idempotency-Key"

_MOVEMENT_TYPES: tuple[str, ...] = get_args(MovementType)
_ALLOWED_FIELDS = frozenset({"type", "amount"})
_MAX_DECIMALS = 2
_DECIMAL_STEP = Decimal(1).scaleb(-_MAX_DECIMALS)


@dataclass(frozen=True)
class NewTransaction:
    """A validated request to move money."""

    type: MovementType
    amount: Decimal
    idempotency_key: str | None

    @classmethod
    def from_payload(cls, payload: object, idempotency_key: str | None) -> "NewTransaction":
        """Validate an untrusted payload; raise ``ValidationError`` listing every problem."""
        if not isinstance(payload, Mapping):
            raise ValidationError([FieldError("body", "must be a JSON object")])
        errors = [
            *_unknown_fields(payload),
            *_check(_type_error(payload.get("type"))),
            *_check(_amount_error(payload.get("amount"))),
            *_check(_idempotency_key_error(idempotency_key)),
        ]
        if errors:
            raise ValidationError(errors)
        return cls(
            type=payload["type"],
            amount=_to_decimal(payload["amount"]),
            idempotency_key=idempotency_key,
        )

    def draft(self, transaction_id: UUID, region: str, now: datetime) -> "MovementDraft":
        """Stamp the request with its id, serving region and UTC time (milliseconds)."""
        if now.tzinfo is None:
            raise ValueError("now must carry a timezone")
        utc = now.astimezone(UTC)
        return MovementDraft(
            id=transaction_id,
            type=self.type,
            amount=self.amount,
            processed_region=region,
            processed_at=utc.replace(microsecond=utc.microsecond // 1000 * 1000),
            idempotency_key=self.idempotency_key,
        )


@dataclass(frozen=True)
class MovementDraft:
    """A stamped movement whose resulting balance is not known until the account is locked."""

    id: UUID
    type: MovementType
    amount: Decimal
    processed_region: str
    processed_at: datetime
    idempotency_key: str | None

    def completed(self, balance_after: Decimal) -> "Transaction":
        """Return the stored movement once the balance it produced is known."""
        return Transaction(
            id=self.id,
            type=self.type,
            amount=self.amount,
            balance_after=balance_after,
            processed_region=self.processed_region,
            processed_at=self.processed_at,
            idempotency_key=self.idempotency_key,
        )


@dataclass(frozen=True)
class Transaction:
    """A stored movement."""

    id: UUID
    type: MovementType
    amount: Decimal
    balance_after: Decimal
    processed_region: str
    processed_at: datetime
    idempotency_key: str | None


@dataclass(frozen=True)
class TransactionStats:
    """Aggregates read from the database."""

    total_count: int
    counts_by_region: Mapping[str, int]
    last_processed_at_by_region: Mapping[str, datetime]
    latest: tuple[Transaction, ...]


def parse_limit(raw: str | None) -> int:
    """Return the page size: default when absent, otherwise an integer from 1 to 100."""
    if raw is None:
        return DEFAULT_LIMIT
    if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= MAX_LIMIT:
        return int(raw)
    raise ValidationError([FieldError("limit", f"must be an integer between 1 and {MAX_LIMIT}")])


def _check(error: FieldError | None) -> list[FieldError]:
    return [] if error is None else [error]


def _unknown_fields(payload: Mapping[str, Any]) -> list[FieldError]:
    return [
        FieldError(str(name), "unknown field") for name in payload if name not in _ALLOWED_FIELDS
    ]


def _to_decimal(value: int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _type_error(value: object) -> FieldError | None:
    if isinstance(value, str) and value in _MOVEMENT_TYPES:
        return None
    return FieldError("type", "must be one of: " + ", ".join(_MOVEMENT_TYPES))


def _amount_error(value: object) -> FieldError | None:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return FieldError("amount", "must be a number")
    amount = _to_decimal(value)
    if not amount.is_finite() or amount <= 0:
        return FieldError("amount", "must be a positive number")
    if amount > MAX_AMOUNT:
        return FieldError("amount", f"must not exceed {MAX_AMOUNT}")
    if amount != amount.quantize(_DECIMAL_STEP):
        return FieldError("amount", f"must have at most {_MAX_DECIMALS} decimals")
    return None


def _idempotency_key_error(value: str | None) -> FieldError | None:
    if value is None or 1 <= len(value) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        return None
    return FieldError(
        IDEMPOTENCY_KEY_FIELD, f"must be 1 to {MAX_IDEMPOTENCY_KEY_LENGTH} characters"
    )
