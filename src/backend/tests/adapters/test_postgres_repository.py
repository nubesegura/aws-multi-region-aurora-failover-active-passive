"""Criteria 1.1-1.7, 2.2, 7.5: SQL, lock and commit order, error mapping (fake connection)."""

import logging
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pg8000.exceptions import DatabaseError, InterfaceError

from dr_demo.adapters.outbound.postgres_repository import (
    _CONNECT_TIMEOUT_SECONDS,
    _SOCKET_TIMEOUT_SECONDS,
    _STATEMENT_TIMEOUT_MS,
    PostgresTransactionRepository,
    UnexpectedDatabaseError,
)
from dr_demo.config.logging import JsonFormatter
from dr_demo.domain.account import Account, LedgerRow
from dr_demo.domain.errors import (
    DatabaseBusy,
    DatabaseReadOnly,
    DatabaseUnavailable,
    InsufficientFunds,
    ValidationError,
)
from dr_demo.domain.transaction import MovementDraft, Transaction
from tests.adapters.fake_db import FakeConnection

_ID = UUID("00000000-0000-4000-8000-000000000001")
_OTHER_ID = UUID("00000000-0000-4000-8000-000000000002")
_AT = datetime(2026, 10, 4, 12, 0, 0, 123_000, tzinfo=UTC)
_LAMBDA_TIMEOUT_SECONDS = 20
_ANSWER_MARGIN_SECONDS = 4


def _transaction(key: str | None = "key-1", identifier: UUID = _ID) -> Transaction:
    return Transaction(
        identifier, "deposit", Decimal("10.50"), Decimal("110.50"), "us-east-2", _AT, key
    )


def _draft(
    key: str | None = "key-1", movement_type: str = "deposit", amount: str = "10.50"
) -> MovementDraft:
    return MovementDraft(
        _ID,
        movement_type,  # type: ignore[arg-type]
        Decimal(amount),
        "us-east-2",
        _AT,
        key,
    )


def _row(transaction: Transaction, *, local_zone: bool = False) -> tuple[object, ...]:
    moment = _AT.astimezone(timezone(timedelta(hours=2))) if local_zone else _AT
    return (
        str(transaction.id),
        transaction.type,
        transaction.amount,
        transaction.balance_after,
        transaction.processed_region,
        moment,
        transaction.idempotency_key,
    )


def _writes(connection: FakeConnection) -> list[str]:
    return [sql for sql, _ in connection.statements() if sql.startswith(("INSERT", "UPDATE"))]


def _applied(balance: str = "100.00", *, keyed: bool = True) -> list[Any]:
    """Script of a successful movement: lock, [duplicate check], insert, update."""
    lock: list[Any] = [[(Decimal(balance),)]]
    check: list[Any] = [[]] if keyed else []
    return [*lock, *check, [(str(_ID),)], []]


class Factory:
    """Hands out the given connections in order and counts the calls."""

    def __init__(self, *connections: FakeConnection | BaseException) -> None:
        self._connections = list(connections)
        self.calls = 0

    def __call__(self) -> FakeConnection:
        self.calls += 1
        outcome = self._connections.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _repository(
    *connections: FakeConnection | BaseException,
) -> tuple[PostgresTransactionRepository, Factory]:
    factory = Factory(*connections)
    return PostgresTransactionRepository(factory), factory


def _read_only_error() -> DatabaseError:
    return DatabaseError(
        {"S": "ERROR", "C": "25006", "M": "cannot execute INSERT in a read-only transaction"}
    )


def test_movement_locks_then_inserts_then_updates_and_commits_last() -> None:
    connection = FakeConnection(_applied())
    repository, _ = _repository(connection)

    stored = repository.apply_movement(_draft())

    assert stored == _transaction()
    statements = connection.statements()
    assert "FROM account WHERE id = 1 FOR UPDATE" in statements[0][0]
    assert "WHERE idempotency_key = %s" in statements[1][0]
    assert statements[1][1] == ("key-1",)
    insert_sql, insert_params = statements[2]
    assert "INSERT INTO transactions" in insert_sql
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in insert_sql
    assert insert_params == (
        _ID,
        "key-1",
        "deposit",
        Decimal("10.50"),
        Decimal("110.50"),
        "us-east-2",
        _AT,
    )
    update_sql, update_params = statements[3]
    assert update_sql.startswith("UPDATE account SET balance = %s, movement_count = ")
    assert update_params == (Decimal("110.50"),)
    assert connection.names() == ["execute"] * 4 + ["cursor_close", "commit"]


def test_movement_never_interpolates_values_into_the_sql() -> None:
    connection = FakeConnection(_applied(keyed=False))
    repository, _ = _repository(connection)
    hostile = MovementDraft(_ID, "deposit", Decimal(1), "x'); DROP TABLE t;--", _AT, None)

    repository.apply_movement(hostile)

    assert all("DROP TABLE" not in sql for sql, _ in connection.statements())
    assert "x'); DROP TABLE t;--" in connection.statements()[1][1]


def test_keyless_movement_skips_the_duplicate_lookup() -> None:
    connection = FakeConnection(_applied(keyed=False))
    repository, _ = _repository(connection)

    repository.apply_movement(_draft(key=None))

    assert len(connection.statements()) == 3


def test_withdrawal_stores_the_reduced_balance() -> None:
    connection = FakeConnection(_applied("100.00"))
    repository, _ = _repository(connection)

    stored = repository.apply_movement(_draft(movement_type="withdrawal", amount="40.00"))

    assert stored.balance_after == Decimal("60.00")
    assert connection.statements()[3][1] == (Decimal("60.00"),)


def test_overdraft_is_refused_with_the_balance_and_nothing_is_written() -> None:
    connection = FakeConnection([[(Decimal("5.00"),)], []])
    repository, _ = _repository(connection)

    with pytest.raises(InsufficientFunds) as caught:
        repository.apply_movement(_draft(movement_type="withdrawal", amount="5.01"))

    assert caught.value.balance == Decimal("5.00")
    assert _writes(connection) == []
    assert "commit" not in connection.names()
    assert "rollback" in connection.names()
    assert "close" not in connection.names()


def test_replayed_key_returns_the_stored_movement_without_any_write() -> None:
    existing = _transaction(identifier=_OTHER_ID)
    connection = FakeConnection([[(Decimal("999.00"),)], [_row(existing, local_zone=True)]])
    repository, _ = _repository(connection)

    stored = repository.apply_movement(_draft())

    assert stored == existing
    assert stored.processed_at.utcoffset() == timedelta(0)
    assert _writes(connection) == []


def test_replayed_withdrawal_is_not_refused_when_the_balance_has_dropped_since() -> None:
    existing = _transaction()
    connection = FakeConnection([[(Decimal("0.00"),)], [_row(existing)]])
    repository, _ = _repository(connection)

    assert repository.apply_movement(_draft(movement_type="withdrawal", amount="50.00")) == existing


def test_insert_conflict_returns_the_stored_movement_without_updating_the_balance() -> None:
    existing = _transaction(identifier=_OTHER_ID)
    connection = FakeConnection([[(Decimal("100.00"),)], [], [], [_row(existing)]])
    repository, _ = _repository(connection)

    assert repository.apply_movement(_draft()) == existing
    assert [sql[:6] for sql in _writes(connection)] == ["INSERT"]


def test_insert_conflict_with_vanished_record_is_unavailable() -> None:
    repository, _ = _repository(FakeConnection([[(Decimal("100.00"),)], [], [], []]))

    with pytest.raises(DatabaseUnavailable):
        repository.apply_movement(_draft())


def test_lock_wait_ended_by_statement_timeout_is_busy_and_not_retried() -> None:
    warm = FakeConnection([[], _db_error("57014", "canceling statement due to statement timeout")])
    repository, factory = _repository(warm, FakeConnection())
    repository.list_latest(1)

    with pytest.raises(DatabaseBusy):
        repository.apply_movement(_draft())

    assert factory.calls == 1
    assert "commit" not in warm.names()[3:]
    assert "rollback" in warm.names()


def test_statement_timeout_on_another_statement_stays_unavailable() -> None:
    connection = FakeConnection([[(Decimal("100.00"),)], _db_error("57014")])
    repository, _ = _repository(connection)

    with pytest.raises(DatabaseUnavailable):
        repository.apply_movement(_draft())


def test_missing_account_row_is_an_internal_error() -> None:
    repository, _ = _repository(FakeConnection([[]]))

    with pytest.raises(UnexpectedDatabaseError):
        repository.apply_movement(_draft())


def _db_error(state: str, message: str = "detail that must not leak") -> DatabaseError:
    return DatabaseError({"S": "ERROR", "C": state, "M": message})


@pytest.mark.parametrize(
    "error",
    [_read_only_error(), _db_error("XX000", "cannot execute in a read-only transaction")],
)
def test_read_only_on_a_fresh_connection_is_mapped_without_retry(error: DatabaseError) -> None:
    connection = FakeConnection([error])
    repository, factory = _repository(connection)

    with pytest.raises(DatabaseReadOnly):
        repository.apply_movement(_draft())

    assert "commit" not in connection.names()
    assert "close" in connection.names()
    assert factory.calls == 1


def test_read_only_on_a_reused_connection_reconnects_and_retries_once() -> None:
    demoted = FakeConnection([[], _read_only_error()])
    promoted = FakeConnection(_applied(keyed=False))
    repository, factory = _repository(demoted, promoted)
    repository.list_latest(1)

    stored = repository.apply_movement(_draft(key=None))

    assert stored == _transaction(key=None)
    assert factory.calls == 2
    assert "close" in demoted.names()
    assert "commit" not in demoted.names()[-3:]
    assert promoted.names()[-1] == "commit"


def test_read_only_on_both_connections_surfaces_after_a_single_retry() -> None:
    first = FakeConnection([[], _read_only_error()])
    second = FakeConnection([_read_only_error()])
    repository, factory = _repository(first, second, FakeConnection())
    repository.list_latest(1)

    with pytest.raises(DatabaseReadOnly):
        repository.apply_movement(_draft())

    assert factory.calls == 2


def test_database_role_always_uses_a_fresh_connection() -> None:
    old = FakeConnection([[], [(False,)]])
    new = FakeConnection([[(True,)]])
    repository, factory = _repository(old, new)
    repository.list_latest(1)

    assert repository.database_role() == "reader"

    assert factory.calls == 2
    assert "close" in old.names()
    assert len(old.statements()) == 1


@pytest.mark.parametrize("state", ["42P01", "23505", "22003", "XX000"])
def test_other_database_errors_are_internal_errors_and_never_retried(state: str) -> None:
    warm = FakeConnection([[], _db_error(state)])
    repository, factory = _repository(warm, FakeConnection())
    repository.list_latest(1)

    with pytest.raises(UnexpectedDatabaseError) as caught:
        repository.list_latest(1)

    assert "leak" not in str(caught.value)
    assert factory.calls == 1
    assert "close" in warm.names()


@pytest.mark.parametrize("state", ["28000", "28P01", "57014"])
def test_auth_and_timeout_errors_are_unavailable_and_not_retried(state: str) -> None:
    warm = FakeConnection([[], _db_error(state)])
    repository, factory = _repository(warm, FakeConnection())
    repository.list_latest(1)

    with pytest.raises(DatabaseUnavailable):
        repository.list_latest(1)

    assert factory.calls == 1


@pytest.mark.parametrize("state", ["08006", "57P01", "53300"])
def test_connection_resource_errors_are_retried_once_on_a_reused_connection(state: str) -> None:
    warm = FakeConnection([[], _db_error(state)])
    fresh = FakeConnection([[_row(_transaction())]])
    repository, factory = _repository(warm, fresh)
    repository.list_latest(1)

    assert repository.list_latest(1) == (_transaction(),)
    assert factory.calls == 2


def test_keyless_movement_is_not_replayed_when_the_commit_may_have_been_sent() -> None:
    warm = FakeConnection([[], *_applied(keyed=False)])
    repository, factory = _repository(warm, FakeConnection(_applied(keyed=False)))
    repository.list_latest(1)
    warm.commit_error = OSError("reset after commit was sent")

    with pytest.raises(DatabaseUnavailable):
        repository.apply_movement(_draft(key=None))

    assert factory.calls == 1


def test_keyed_movement_is_replayed_after_a_commit_failure_because_it_is_idempotent() -> None:
    warm = FakeConnection([[], *_applied()])
    fresh = FakeConnection([[(Decimal("110.50"),)], [_row(_transaction())]])
    repository, factory = _repository(warm, fresh)
    repository.list_latest(1)
    warm.commit_error = OSError("reset after commit was sent")

    stored = repository.apply_movement(_draft())

    assert stored == _transaction()
    assert factory.calls == 2


def test_keyless_movement_is_replayed_when_the_failure_happened_before_commit() -> None:
    warm = FakeConnection([[], InterfaceError("network error")])
    fresh = FakeConnection(_applied(keyed=False))
    repository, factory = _repository(warm, fresh)
    repository.list_latest(1)

    assert repository.apply_movement(_draft(key=None)) == _transaction(key=None)
    assert factory.calls == 2


def test_connect_failure_is_unavailable_and_not_retried() -> None:
    repository, factory = _repository(DatabaseUnavailable(), DatabaseUnavailable())

    with pytest.raises(DatabaseUnavailable):
        repository.list_latest(5)

    assert factory.calls == 1


def test_commit_failure_is_unavailable() -> None:
    connection = FakeConnection(_applied())
    connection.commit_error = OSError("connection reset")
    repository, _ = _repository(connection)

    with pytest.raises(DatabaseUnavailable):
        repository.apply_movement(_draft())


def test_warm_connection_is_reused_across_calls() -> None:
    connection = FakeConnection([[], []])
    repository, factory = _repository(connection)

    repository.list_latest(5)
    repository.list_latest(5)

    assert factory.calls == 1
    assert connection.names().count("commit") == 2


def test_stale_reused_connection_is_replaced_and_the_call_retried_once() -> None:
    stale = FakeConnection([[], InterfaceError("network error")])
    fresh = FakeConnection([[_row(_transaction())]])
    repository, factory = _repository(stale, fresh)

    repository.list_latest(5)
    result = repository.list_latest(5)

    assert result == (_transaction(),)
    assert factory.calls == 2
    assert "close" in stale.names()


def test_fresh_connection_failure_is_not_retried() -> None:
    first = FakeConnection([InterfaceError("network error")])
    repository, factory = _repository(first, FakeConnection())

    with pytest.raises(DatabaseUnavailable):
        repository.list_latest(5)

    assert factory.calls == 1


def test_find_by_idempotency_key() -> None:
    connection = FakeConnection([[_row(_transaction())], []])
    repository, _ = _repository(connection)

    assert repository.find_by_idempotency_key("key-1") == _transaction()
    assert repository.find_by_idempotency_key("other") is None
    assert connection.statements()[0][1] == ("key-1",)


def test_list_latest_orders_and_limits_in_sql() -> None:
    connection = FakeConnection([[_row(_transaction()), _row(_transaction(identifier=_OTHER_ID))]])
    repository, _ = _repository(connection)

    result = repository.list_latest(7)

    assert [item.id for item in result] == [_ID, _OTHER_ID]
    sql, params = connection.statements()[0]
    assert "ORDER BY processed_at DESC" in sql
    assert "LIMIT %s" in sql
    assert params == (7,)


def test_stats_combine_groups_and_latest_in_one_transaction() -> None:
    later = _AT + timedelta(seconds=5)
    connection = FakeConnection(
        [[("us-east-2", 3, _AT), ("us-west-2", 2, later)], [_row(_transaction())]]
    )
    repository, _ = _repository(connection)

    stats = repository.stats(10)

    assert stats.total_count == 5
    assert stats.counts_by_region == {"us-east-2": 3, "us-west-2": 2}
    assert stats.last_processed_at_by_region["us-west-2"] == later
    assert stats.latest == (_transaction(),)
    assert connection.statements()[1][1] == (10,)
    assert connection.names().count("commit") == 1


def test_empty_stats() -> None:
    repository, _ = _repository(FakeConnection([[], []]))

    stats = repository.stats(10)

    assert stats.total_count == 0
    assert stats.counts_by_region == {}
    assert stats.latest == ()


@pytest.mark.parametrize(("in_recovery", "role"), [(True, "reader"), (False, "writer")])
def test_database_role_comes_from_pg_is_in_recovery(in_recovery: bool, role: str) -> None:
    connection = FakeConnection([[(in_recovery,)]])
    repository, _ = _repository(connection)

    assert repository.database_role() == role
    assert connection.statements()[0][0] == "SELECT pg_is_in_recovery()"


def test_unexpected_errors_are_not_swallowed_and_drop_the_connection() -> None:
    connection = FakeConnection([ValueError("bug")])
    repository, _ = _repository(connection)

    with pytest.raises(ValueError, match="bug"):
        repository.list_latest(1)

    assert "close" in connection.names()


def test_failing_rollback_after_read_only_discards_the_connection() -> None:
    class BrokenRollback(FakeConnection):
        def rollback(self) -> None:
            super().rollback()
            raise OSError("gone")

    connection = BrokenRollback([_read_only_error()])
    repository, _ = _repository(connection)

    with pytest.raises(DatabaseReadOnly):
        repository.apply_movement(_draft())

    assert "close" in connection.names()


def test_failing_close_while_discarding_is_tolerated() -> None:
    class BrokenClose(FakeConnection):
        def close(self) -> None:
            raise InterfaceError("already closed")

    repository, _ = _repository(BrokenClose([InterfaceError("network error")]))

    with pytest.raises(DatabaseUnavailable):
        repository.list_latest(1)


def test_account_reads_balance_and_count() -> None:
    connection = FakeConnection([[(Decimal("42.00"), 3)]])
    repository, _ = _repository(connection)

    assert repository.account() == Account(Decimal("42.00"), 3)
    assert connection.statements()[0][0].startswith("SELECT balance, movement_count FROM account")


def test_account_missing_row_is_an_internal_error() -> None:
    repository, _ = _repository(FakeConnection([[]]))

    with pytest.raises(UnexpectedDatabaseError):
        repository.account()


def _ledger_record(balance: str, transaction: Transaction) -> tuple[object, ...]:
    return (
        Decimal(balance),
        transaction.id,
        transaction.type,
        transaction.amount,
        transaction.balance_after,
        transaction.idempotency_key,
        transaction.processed_region,
    )


def test_ledger_is_one_plain_select_ordered_by_seq_with_limit_plus_one() -> None:
    first = _transaction()
    second = _transaction(identifier=_OTHER_ID)
    records = [_ledger_record("120.00", first), _ledger_record("120.00", second)]
    connection = FakeConnection([records])
    repository, _ = _repository(connection)

    snapshot = repository.ledger(50_000)

    sql, params = connection.statements()[0]
    assert len(connection.statements()) == 1
    assert sql.startswith("SELECT ")
    assert "FOR UPDATE" not in sql
    assert "ORDER BY seq LIMIT %s" in sql
    assert params == (50_001,)
    assert snapshot.stored_balance == Decimal("120.00")
    assert snapshot.rows == (
        LedgerRow(_ID, "deposit", Decimal("10.50"), Decimal("110.50"), "key-1", "us-east-2"),
        LedgerRow(_OTHER_ID, "deposit", Decimal("10.50"), Decimal("110.50"), "key-1", "us-east-2"),
    )


def test_empty_ledger_still_returns_the_stored_balance() -> None:
    empty = (Decimal("0.00"), None, None, None, None, None, None)
    repository, _ = _repository(FakeConnection([[empty]]))

    snapshot = repository.ledger(10)

    assert snapshot.rows == ()
    assert snapshot.stored_balance == Decimal("0.00")


def test_ledger_without_account_row_is_an_internal_error() -> None:
    repository, _ = _repository(FakeConnection([[]]))

    with pytest.raises(UnexpectedDatabaseError):
        repository.ledger(10)


def test_business_refusal_rolls_back_but_keeps_the_warm_connection() -> None:
    connection = FakeConnection([[(Decimal("5.00"),)], [(Decimal("5.00"),)]])
    repository, factory = _repository(connection)

    with pytest.raises(InsufficientFunds):
        repository.apply_movement(_draft(movement_type="withdrawal", amount="9.00", key=None))
    with pytest.raises(InsufficientFunds):
        repository.apply_movement(_draft(movement_type="withdrawal", amount="9.00", key=None))

    assert factory.calls == 1
    assert connection.names().count("rollback") == 2
    assert "close" not in connection.names()


def test_balance_cap_refusal_keeps_the_warm_connection() -> None:
    connection = FakeConnection([[(Decimal("9999999999.99"),)]])
    repository, _ = _repository(connection)

    with pytest.raises(ValidationError):
        repository.apply_movement(_draft(amount="1.00", key=None))

    assert "rollback" in connection.names()
    assert "close" not in connection.names()


def test_database_error_text_with_row_values_never_reaches_the_log() -> None:
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = Capture()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dr_demo")
    logger.addHandler(handler)
    leak = "Failing row contains (secret-key-77, 12345.67)"
    connection = FakeConnection([_db_error("23514", leak)])
    repository, _ = _repository(connection)
    try:
        with pytest.raises(UnexpectedDatabaseError) as caught:
            repository.apply_movement(_draft())
        logger.exception("unhandled error", exc_info=caught.value)
    finally:
        logger.removeHandler(handler)

    text = " ".join(lines)
    assert "23514" in text
    assert "DatabaseError" in text
    assert "secret-key-77" not in text
    assert "12345.67" not in text


def test_time_budget_fits_a_stalled_attempt_plus_one_fresh_retry_in_the_lambda_timeout() -> None:
    stalled_attempt = _SOCKET_TIMEOUT_SECONDS
    fresh_retry = _CONNECT_TIMEOUT_SECONDS + _STATEMENT_TIMEOUT_MS / 1000

    assert _SOCKET_TIMEOUT_SECONDS > _STATEMENT_TIMEOUT_MS / 1000
    assert stalled_attempt + fresh_retry + _ANSWER_MARGIN_SECONDS <= _LAMBDA_TIMEOUT_SECONDS


def test_failing_rollback_after_a_business_refusal_discards_the_connection() -> None:
    class BrokenRollback(FakeConnection):
        def rollback(self) -> None:
            super().rollback()
            raise OSError("gone")

    connection = BrokenRollback([[(Decimal("5.00"),)]])
    repository, _ = _repository(connection)

    with pytest.raises(InsufficientFunds):
        repository.apply_movement(_draft(movement_type="withdrawal", amount="9.00", key=None))

    assert "close" in connection.names()
