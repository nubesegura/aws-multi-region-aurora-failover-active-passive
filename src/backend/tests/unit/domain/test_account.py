"""Criteria 1.5 and 7.1/7.2: balance rule and consistency check."""

from decimal import Decimal
from uuid import UUID

import pytest

from dr_demo.domain.account import CONSISTENCY_LIMIT, LedgerRow, check_consistency, next_balance
from dr_demo.domain.errors import DomainError, FieldError, InsufficientFunds, ValidationError
from dr_demo.domain.transaction import MAX_AMOUNT, MovementType


def _id(number: int) -> UUID:
    return UUID(int=number, version=4)


def _row(
    number: int,
    movement_type: MovementType,
    amount: str,
    balance_after: str,
    key: str | None = "",
    region: str = "us-east-2",
) -> LedgerRow:
    return LedgerRow(
        _id(number),
        movement_type,
        Decimal(amount),
        Decimal(balance_after),
        f"key-{number}" if key == "" else key,
        region,
    )


def _clean_ledger() -> list[LedgerRow]:
    return [
        _row(1, "deposit", "100.00", "100.00"),
        _row(2, "withdrawal", "30.25", "69.75", region="us-west-2"),
        _row(3, "deposit", "0.25", "70.00"),
    ]


def test_deposit_adds_to_the_balance() -> None:
    assert next_balance(Decimal("10.00"), "deposit", Decimal("0.01")) == Decimal("10.01")


def test_withdrawal_of_the_exact_balance_leaves_zero() -> None:
    assert next_balance(Decimal("10.00"), "withdrawal", Decimal("10.00")) == Decimal("0.00")


def test_overdraft_raises_with_the_current_balance() -> None:
    with pytest.raises(InsufficientFunds) as caught:
        next_balance(Decimal("10.00"), "withdrawal", Decimal("10.01"))

    assert caught.value.balance == Decimal("10.00")
    assert isinstance(caught.value, DomainError)


def test_deposit_reaching_exactly_the_maximum_is_allowed() -> None:
    result = next_balance(MAX_AMOUNT - Decimal("0.01"), "deposit", Decimal("0.01"))

    assert result == MAX_AMOUNT


def test_deposit_one_cent_above_the_maximum_is_rejected_on_the_amount_field() -> None:
    with pytest.raises(ValidationError) as caught:
        next_balance(MAX_AMOUNT, "deposit", Decimal("0.01"))

    assert caught.value.fields == (FieldError("amount", "balance would exceed the maximum"),)


def test_withdrawal_from_a_maximum_balance_is_unaffected() -> None:
    assert next_balance(MAX_AMOUNT, "withdrawal", Decimal("0.01")) == MAX_AMOUNT - Decimal("0.01")


def test_clean_ledger_is_consistent() -> None:
    report = check_consistency(_clean_ledger(), Decimal("70.00"), CONSISTENCY_LIMIT)

    assert report.consistent is True
    assert report.count == 3
    assert report.total_deposited == Decimal("100.25")
    assert report.total_withdrawn == Decimal("30.25")
    assert report.replayed_balance == Decimal("70.00")
    assert report.stored_balance == Decimal("70.00")
    assert report.balances_match is True
    assert report.chain_ok is True
    assert report.duplicate_keys == 0
    assert report.counts_by_region == {"us-east-2": 2, "us-west-2": 1}
    assert report.first_offender is None
    assert report.truncated is False


def test_empty_ledger_is_consistent_with_balance_zero() -> None:
    report = check_consistency([], Decimal("0.00"), CONSISTENCY_LIMIT)

    assert report.consistent is True
    assert report.count == 0
    assert report.replayed_balance == Decimal(0)
    assert report.counts_by_region == {}


def test_tampered_balance_after_breaks_the_chain_and_names_the_row() -> None:
    rows = _clean_ledger()
    rows[1] = _row(2, "withdrawal", "30.25", "60.00", region="us-west-2")

    report = check_consistency(rows, Decimal("70.00"), CONSISTENCY_LIMIT)

    assert report.consistent is False
    assert report.chain_ok is False
    assert report.first_offender == _id(2)


def test_first_row_must_start_from_zero() -> None:
    rows = [_row(1, "deposit", "100.00", "150.00")]

    report = check_consistency(rows, Decimal("150.00"), CONSISTENCY_LIMIT)

    assert report.chain_ok is False
    assert report.first_offender == _id(1)


def test_replay_that_goes_negative_is_a_chain_break() -> None:
    rows = [_row(1, "withdrawal", "5.00", "-5.00")]

    report = check_consistency(rows, Decimal("-5.00"), CONSISTENCY_LIMIT)

    assert report.chain_ok is False
    assert report.consistent is False


def test_duplicate_key_is_counted_and_names_the_second_row() -> None:
    rows = _clean_ledger()
    rows[2] = _row(3, "deposit", "0.25", "70.00", key="key-1")

    report = check_consistency(rows, Decimal("70.00"), CONSISTENCY_LIMIT)

    assert report.duplicate_keys == 1
    assert report.consistent is False
    assert report.first_offender == _id(3)


def test_rows_without_a_key_are_not_duplicates() -> None:
    rows = [_row(1, "deposit", "1", "1", key=None), _row(2, "deposit", "1", "2", key=None)]

    report = check_consistency(rows, Decimal(2), CONSISTENCY_LIMIT)

    assert report.duplicate_keys == 0
    assert report.consistent is True


def test_stored_balance_that_differs_is_inconsistent() -> None:
    report = check_consistency(_clean_ledger(), Decimal("71.00"), CONSISTENCY_LIMIT)

    assert report.balances_match is False
    assert report.chain_ok is True
    assert report.consistent is False
    assert report.first_offender == _id(3)


def test_non_zero_stored_balance_on_an_empty_ledger_has_no_offender_row() -> None:
    report = check_consistency([], Decimal("1.00"), CONSISTENCY_LIMIT)

    assert report.consistent is False
    assert report.first_offender is None


def test_ledger_larger_than_the_limit_is_truncated_and_claims_nothing() -> None:
    rows = [
        _row(1, "deposit", "1.00", "1.00"),
        _row(2, "deposit", "1.00", "2.00"),
        _row(3, "deposit", "1.00", "3.00"),
    ]

    report = check_consistency(rows, Decimal("3.00"), 2)

    assert report.truncated is True
    assert report.consistent is None
    assert report.balances_match is None
    assert report.count == 2
    assert report.replayed_balance == Decimal("2.00")


def test_ledger_exactly_at_the_limit_is_not_truncated() -> None:
    rows = [_row(1, "deposit", "1.00", "1.00"), _row(2, "deposit", "1.00", "2.00")]

    report = check_consistency(rows, Decimal("2.00"), 2)

    assert report.truncated is False
    assert report.consistent is True


def test_truncated_ledger_with_a_chain_break_in_the_part_read_is_inconsistent() -> None:
    rows = [
        _row(1, "deposit", "1.00", "1.00"),
        _row(2, "deposit", "1.00", "5.00"),
        _row(3, "deposit", "1.00", "6.00"),
    ]

    report = check_consistency(rows, Decimal("6.00"), 2)

    assert report.truncated is True
    assert report.consistent is False
    assert report.chain_ok is False
    assert report.balances_match is None
    assert report.first_offender == _id(2)


def test_truncated_ledger_with_a_duplicate_key_in_the_part_read_is_inconsistent() -> None:
    rows = [
        _row(1, "deposit", "1.00", "1.00", key="same"),
        _row(2, "deposit", "1.00", "2.00", key="same"),
        _row(3, "deposit", "1.00", "3.00"),
    ]

    report = check_consistency(rows, Decimal("3.00"), 2)

    assert report.truncated is True
    assert report.consistent is False
    assert report.duplicate_keys == 1
    assert report.first_offender == _id(2)
