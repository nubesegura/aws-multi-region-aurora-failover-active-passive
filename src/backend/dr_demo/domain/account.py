"""The single account: balance rule and the consistency check of the ledger."""

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from dr_demo.domain.errors import FieldError, InsufficientFunds, ValidationError
from dr_demo.domain.transaction import MAX_AMOUNT, MovementType

CONSISTENCY_LIMIT = 50_000


@dataclass(frozen=True)
class Account:
    """The balance and the number of movements applied to it."""

    balance: Decimal
    movement_count: int


class LedgerRow(NamedTuple):
    """One movement as the consistency check needs it, in ledger order."""

    id: UUID
    type: MovementType
    amount: Decimal
    balance_after: Decimal
    idempotency_key: str | None
    processed_region: str


@dataclass(frozen=True)
class LedgerSnapshot:
    """Up to ``limit + 1`` ledger rows in ledger order and the balance stored on the account."""

    rows: tuple[LedgerRow, ...]
    stored_balance: Decimal


@dataclass(frozen=True)
class ConsistencyReport:
    """Result of replaying the ledger.

    ``consistent`` is ``None`` when the ledger was truncated and nothing wrong was found.
    """

    count: int
    total_deposited: Decimal
    total_withdrawn: Decimal
    replayed_balance: Decimal
    stored_balance: Decimal
    balances_match: bool | None
    chain_ok: bool
    duplicate_keys: int
    counts_by_region: Mapping[str, int]
    consistent: bool | None
    first_offender: UUID | None
    truncated: bool


def next_balance(balance: Decimal, movement_type: MovementType, amount: Decimal) -> Decimal:
    """Return the balance after the movement.

    A withdrawal above the balance is refused; so is a deposit that would push the balance
    above ``MAX_AMOUNT`` (the ``numeric(12,2)`` maximum).
    """
    if movement_type == "deposit":
        if balance + amount > MAX_AMOUNT:
            raise ValidationError([FieldError("amount", "balance would exceed the maximum")])
        return balance + amount
    if amount > balance:
        raise InsufficientFunds(balance)
    return balance - amount


def check_consistency(
    rows: Sequence[LedgerRow], stored_balance: Decimal, limit: int
) -> ConsistencyReport:
    """Replay at most ``limit`` rows and compare the result with the stored balance.

    When more than ``limit`` rows are given the report is marked truncated: it never claims
    consistency, but a chain break or duplicate key already found in the rows read is reported.
    """
    truncated = len(rows) > limit
    analysed = rows[:limit]
    deposited = _total(analysed, "deposit")
    withdrawn = _total(analysed, "withdrawal")
    replayed = deposited - withdrawn
    offender, duplicates = _scan(analysed)
    chain_ok = not any(_chain_breaks(analysed))
    balances_match = None if truncated else replayed == stored_balance
    if balances_match is False and offender is None and analysed:
        offender = analysed[-1].id
    consistent = _verdict(chain_ok, duplicates, balances_match)
    return ConsistencyReport(
        count=len(analysed),
        total_deposited=deposited,
        total_withdrawn=withdrawn,
        replayed_balance=replayed,
        stored_balance=stored_balance,
        balances_match=balances_match,
        chain_ok=chain_ok,
        duplicate_keys=duplicates,
        counts_by_region=dict(Counter(row.processed_region for row in analysed)),
        consistent=consistent,
        first_offender=offender,
        truncated=truncated,
    )


def _verdict(chain_ok: bool, duplicates: int, balances_match: bool | None) -> bool | None:
    """``False`` when a fault was found, ``None`` when the balance could not be compared."""
    if not chain_ok or duplicates:
        return False
    return balances_match


def _chain_breaks(rows: Sequence[LedgerRow]) -> Iterator[bool]:
    """Yield, per row, whether its ``balance_after`` does not follow from the previous row."""
    previous = Decimal(0)
    for row in rows:
        expected = previous + row.amount if row.type == "deposit" else previous - row.amount
        yield row.balance_after != expected or row.balance_after < 0
        previous = row.balance_after


def _scan(rows: Sequence[LedgerRow]) -> tuple[UUID | None, int]:
    """Return the first row that breaks the chain or repeats a key, and the repeated-key count."""
    offender: UUID | None = None
    seen_keys: set[str] = set()
    duplicates = 0
    for row, broken in zip(rows, _chain_breaks(rows), strict=True):
        repeated = row.idempotency_key is not None and row.idempotency_key in seen_keys
        if row.idempotency_key is not None:
            seen_keys.add(row.idempotency_key)
        duplicates += repeated
        if offender is None and (broken or repeated):
            offender = row.id
    return offender, duplicates


def _total(rows: Sequence[LedgerRow], movement_type: MovementType) -> Decimal:
    return sum((row.amount for row in rows if row.type == movement_type), Decimal(0))
