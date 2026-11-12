"""PostgreSQL (Aurora) adapter over pg8000 with IAM authentication and verified TLS."""

import logging
import socket
import ssl
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, auto
from pathlib import Path
from typing import Any, Protocol, TypeVar
from uuid import UUID

import pg8000.dbapi
from pg8000.exceptions import DatabaseError, Error

from dr_demo.adapters.outbound.iam_token import TokenProvider
from dr_demo.domain.account import Account, LedgerRow, LedgerSnapshot, next_balance
from dr_demo.domain.errors import (
    DatabaseBusy,
    DatabaseReadOnly,
    DatabaseUnavailable,
    DomainError,
)
from dr_demo.domain.transaction import (
    DatabaseRole,
    MovementDraft,
    Transaction,
    TransactionStats,
)

LOGGER = logging.getLogger("dr_demo.postgres")

_READ_ONLY_SQLSTATE = "25006"
_READ_ONLY_TEXT = "read-only transaction"
_STATEMENT_TIMEOUT_SQLSTATE = "57014"
_RETRYABLE_SQLSTATE_PREFIXES = ("08", "53", "57P")
_AUTH_SQLSTATE_PREFIX = "28"
# Time budget inside the 20 s Lambda timeout: a reused connection that black-holes waits at most
# the socket timeout (8 s); the single retry then needs a connect (3 s) and a statement (5 s):
# 8 + 3 + 5 = 16 s, which leaves 4 s to answer 503.
_CONNECT_TIMEOUT_SECONDS = 3
_STATEMENT_TIMEOUT_MS = 5000
_SOCKET_TIMEOUT_SECONDS = 8
_COLUMNS = "id, type, amount, balance_after, processed_region, processed_at, idempotency_key"

_LOCK_ACCOUNT = "SELECT balance FROM account WHERE id = 1 FOR UPDATE"
_UPDATE_ACCOUNT = (
    "UPDATE account SET balance = %s, movement_count = movement_count + 1 WHERE id = 1"
)
_INSERT = (
    "INSERT INTO transactions "
    "(id, idempotency_key, type, amount, balance_after, processed_region, processed_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
)
_SELECT_BY_KEY = f"SELECT {_COLUMNS} FROM transactions WHERE idempotency_key = %s"  # noqa: S608
_SELECT_LATEST = (
    f"SELECT {_COLUMNS} FROM transactions "  # noqa: S608
    "ORDER BY processed_at DESC, id DESC LIMIT %s"
)
_SELECT_ACCOUNT = "SELECT balance, movement_count FROM account WHERE id = 1"
# One statement, so the rows and the balance come from the same snapshot.
_SELECT_LEDGER = (
    "SELECT a.balance, t.id, t.type, t.amount, t.balance_after, t.idempotency_key, "
    "t.processed_region FROM account a LEFT JOIN "
    "(SELECT seq, id, type, amount, balance_after, idempotency_key, processed_region "
    "FROM transactions ORDER BY seq LIMIT %s) t ON TRUE WHERE a.id = 1 ORDER BY t.seq"
)
_SELECT_GROUPS = (
    "SELECT processed_region, COUNT(*), MAX(processed_at) FROM transactions "
    "GROUP BY processed_region"
)
_SELECT_RECOVERY = "SELECT pg_is_in_recovery()"

T = TypeVar("T")


class DbCursor(Protocol):
    def execute(self, operation: str, args: Sequence[Any] = ...) -> Any: ...
    def fetchone(self) -> Sequence[Any] | None: ...
    def fetchall(self) -> Sequence[Sequence[Any]]: ...
    def close(self) -> None: ...


class DbConnection(Protocol):
    def cursor(self) -> DbCursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[], DbConnection]


class PgConnectionFactory:
    """Open a TLS connection whose password is a fresh IAM token.

    The TCP connect is bounded by a short connect timeout. The server enforces
    ``statement_timeout`` on every statement; the longer socket timeout is only a backstop for a
    black-holed connection (for example to a dead writer).
    """

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        ca_bundle_path: Path,
        token_provider: TokenProvider,
        connect: Callable[..., DbConnection] = pg8000.dbapi.connect,
        ssl_context_factory: Callable[[Path], ssl.SSLContext] | None = None,
        open_socket: Callable[[str, int], socket.socket] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._ca_bundle_path = ca_bundle_path
        self._token_provider = token_provider
        self._connect = connect
        self._ssl_context_factory = ssl_context_factory or _verifying_context
        self._open_socket = open_socket or _open_socket
        self._ssl_context: ssl.SSLContext | None = None

    def __call__(self) -> DbConnection:
        sock: socket.socket | None = None
        try:
            password = self._token_provider()
            sock = self._open_socket(self._host, self._port)
            return self._connect(
                host=self._host,
                port=self._port,
                database=self._database,
                user=self._user,
                password=password,
                ssl_context=self._context(),
                sock=sock,
                startup_params={"statement_timeout": str(_STATEMENT_TIMEOUT_MS)},
            )
        except (Error, OSError) as error:
            if sock is not None:
                sock.close()
            LOGGER.error(
                "database connection failed",
                extra={"fields": {"error_type": type(error).__name__}},
            )
            raise DatabaseUnavailable from error

    def _context(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            self._ssl_context = self._ssl_context_factory(self._ca_bundle_path)
        return self._ssl_context


def _open_socket(host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.settimeout(_SOCKET_TIMEOUT_SECONDS)
    return sock


def _verifying_context(ca_bundle_path: Path) -> ssl.SSLContext:
    """TLS context that verifies the chain against the RDS CA bundle and the host name."""
    return ssl.create_default_context(cafile=str(ca_bundle_path))


class UnexpectedDatabaseError(Exception):
    """A database error that is neither connectivity nor role related (schema, data or bug)."""


class _Kind(Enum):
    READ_ONLY = auto()
    RETRYABLE = auto()
    UNAVAILABLE = auto()
    INTERNAL = auto()


class _AttemptFailed(Exception):  # noqa: N818 - internal control flow
    def __init__(self, cause: Exception, kind: _Kind, *, committing: bool) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause
        self.kind = kind
        self.committing = committing

    def may_retry(self, *, replay_safe: bool) -> bool:
        """Read-only rejections wrote nothing; connection failures are safe unless a commit
        may already have reached the server and replaying the write could duplicate it."""
        if self.kind is _Kind.READ_ONLY:
            return True
        return self.kind is _Kind.RETRYABLE and (replay_safe or not self.committing)

    def mapped(self) -> Exception:
        if self.kind is _Kind.READ_ONLY:
            return DatabaseReadOnly()
        LOGGER.error(
            "database operation failed",
            extra={
                "fields": {
                    "error_type": type(self.cause).__name__,
                    "sqlstate": _sqlstate(self.cause),
                    "kind": self.kind.name.lower(),
                }
            },
        )
        if self.kind is _Kind.INTERNAL:
            return UnexpectedDatabaseError()
        return DatabaseUnavailable()


class PostgresTransactionRepository:
    """Implements the ``TransactionRepository`` port.

    One connection is opened lazily and reused by warm invocations. A business refusal only rolls
    back (releasing the row lock); any other failure discards it. The database error text (which
    can carry row values) is never chained to the mapped error, so it cannot reach the logs. If
    the failed connection had been reused, the operation is retried once on a fresh connection
    (new DNS resolution, TLS handshake and IAM token) when that is safe: after a read-only
    rejection (nothing was written, and the role may have changed) or a connection failure.
    A write without idempotency key is not replayed once its COMMIT may have been sent.
    ``database_role`` always uses a fresh connection so it never reports a stale role.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory
        self._connection: DbConnection | None = None

    def apply_movement(self, draft: MovementDraft) -> Transaction:
        def work(cursor: DbCursor) -> Transaction:
            balance = _lock_account(cursor)
            if draft.idempotency_key is not None:
                stored = _find(cursor, draft.idempotency_key)
                if stored is not None:
                    return stored
            movement = draft.completed(next_balance(balance, draft.type, draft.amount))
            cursor.execute(_INSERT, _insert_args(movement))
            if cursor.fetchone() is None:
                return _existing(_find(cursor, draft.idempotency_key))
            cursor.execute(_UPDATE_ACCOUNT, (movement.balance_after,))
            return movement

        return self._run(work, replay_safe=draft.idempotency_key is not None)

    def find_by_idempotency_key(self, key: str) -> Transaction | None:
        def work(cursor: DbCursor) -> Transaction | None:
            return _find(cursor, key)

        return self._run(work)

    def account(self) -> Account:
        def work(cursor: DbCursor) -> Account:
            cursor.execute(_SELECT_ACCOUNT)
            row = cursor.fetchone()
            if row is None:
                raise UnexpectedDatabaseError
            return Account(balance=Decimal(row[0]), movement_count=int(row[1]))

        return self._run(work)

    def ledger(self, limit: int) -> LedgerSnapshot:
        def work(cursor: DbCursor) -> LedgerSnapshot:
            cursor.execute(_SELECT_LEDGER, (limit + 1,))
            rows = cursor.fetchall()
            if not rows:
                raise UnexpectedDatabaseError
            return LedgerSnapshot(
                rows=tuple(_to_ledger_row(row) for row in rows if row[1] is not None),
                stored_balance=Decimal(rows[0][0]),
            )

        return self._run(work)

    def list_latest(self, limit: int) -> tuple[Transaction, ...]:
        def work(cursor: DbCursor) -> tuple[Transaction, ...]:
            return _latest(cursor, limit)

        return self._run(work)

    def stats(self, latest_count: int) -> TransactionStats:
        def work(cursor: DbCursor) -> TransactionStats:
            cursor.execute(_SELECT_GROUPS)
            groups = cursor.fetchall()
            return TransactionStats(
                total_count=sum(int(row[1]) for row in groups),
                counts_by_region={str(row[0]): int(row[1]) for row in groups},
                last_processed_at_by_region={str(row[0]): _utc(row[2]) for row in groups},
                latest=_latest(cursor, latest_count),
            )

        return self._run(work)

    def database_role(self) -> DatabaseRole:
        def work(cursor: DbCursor) -> DatabaseRole:
            cursor.execute(_SELECT_RECOVERY)
            row = cursor.fetchone()
            return "reader" if row is not None and row[0] else "writer"

        return self._run(work, fresh=True)

    def _run(
        self, work: Callable[[DbCursor], T], *, replay_safe: bool = True, fresh: bool = False
    ) -> T:
        if fresh and self._connection is not None:
            self._discard(self._connection)
        reused = self._connection is not None
        try:
            return self._attempt(work)
        except _AttemptFailed as failed:
            if not (reused and failed.may_retry(replay_safe=replay_safe)):
                raise failed.mapped() from None
        try:
            return self._attempt(work)
        except _AttemptFailed as failed:
            raise failed.mapped() from None

    def _attempt(self, work: Callable[[DbCursor], T]) -> T:
        connection = self._connection or self._connection_factory()
        self._connection = connection
        committing = False
        try:
            cursor = connection.cursor()
            try:
                result = work(cursor)
            finally:
                cursor.close()
            committing = True
            connection.commit()
        except (Error, OSError) as error:
            self._discard(connection)
            raise _AttemptFailed(error, _classify(error), committing=committing) from error
        except DomainError:
            self._release_locks(connection)
            raise
        except BaseException:
            self._discard(connection)
            raise
        return result

    def _release_locks(self, connection: DbConnection) -> None:
        """Roll back a refused business operation but keep the healthy warm connection."""
        try:
            connection.rollback()
        except (Error, OSError):
            self._discard(connection)

    def _discard(self, connection: DbConnection) -> None:
        self._connection = None
        for release in (connection.rollback, connection.close):
            try:
                release()
            except (Error, OSError):
                LOGGER.warning("could not cleanly release a broken database connection")


def _classify(error: Exception) -> _Kind:
    """Classify by SQLSTATE class; non-database errors (sockets, protocol) are connectivity."""
    if not isinstance(error, DatabaseError):
        return _Kind.RETRYABLE
    state = _sqlstate(error) or ""
    if state == _READ_ONLY_SQLSTATE or _READ_ONLY_TEXT in _message(error).lower():
        return _Kind.READ_ONLY
    if state.startswith(_RETRYABLE_SQLSTATE_PREFIXES):
        return _Kind.RETRYABLE
    if state.startswith(_AUTH_SQLSTATE_PREFIX) or state == _STATEMENT_TIMEOUT_SQLSTATE:
        return _Kind.UNAVAILABLE
    return _Kind.INTERNAL


def _message(error: Exception) -> str:
    detail = error.args[0] if error.args else None
    return str(detail.get("M", "")) if isinstance(detail, dict) else str(detail)


def _sqlstate(error: Exception) -> str | None:
    detail = error.args[0] if error.args else None
    code = detail.get("C") if isinstance(detail, dict) else None
    return code if isinstance(code, str) else None


def _latest(cursor: DbCursor, limit: int) -> tuple[Transaction, ...]:
    cursor.execute(_SELECT_LATEST, (limit,))
    return tuple(_to_transaction(row) for row in cursor.fetchall())


def _lock_account(cursor: DbCursor) -> Decimal:
    """Lock the account row; a wait ended by the statement timeout means the account is busy."""
    try:
        cursor.execute(_LOCK_ACCOUNT)
    except DatabaseError as error:
        if _sqlstate(error) == _STATEMENT_TIMEOUT_SQLSTATE:
            LOGGER.warning("account lock not obtained within the statement timeout")
            raise DatabaseBusy from error
        raise
    row = cursor.fetchone()
    if row is None:
        raise UnexpectedDatabaseError
    return Decimal(row[0])


def _find(cursor: DbCursor, key: str | None) -> Transaction | None:
    cursor.execute(_SELECT_BY_KEY, (key,))
    row = cursor.fetchone()
    return None if row is None else _to_transaction(row)


def _insert_args(movement: Transaction) -> tuple[Any, ...]:
    return (
        movement.id,
        movement.idempotency_key,
        movement.type,
        movement.amount,
        movement.balance_after,
        movement.processed_region,
        movement.processed_at,
    )


def _existing(transaction: Transaction | None) -> Transaction:
    if transaction is None:
        raise DatabaseUnavailable
    return transaction


def _to_ledger_row(row: Sequence[Any]) -> LedgerRow:
    _, identifier, movement_type, amount, balance_after, key, region = row
    return LedgerRow(
        id=_uuid(identifier),
        type=movement_type,
        amount=Decimal(amount),
        balance_after=Decimal(balance_after),
        idempotency_key=key,
        processed_region=region,
    )


def _to_transaction(row: Sequence[Any]) -> Transaction:
    identifier, movement_type, amount, balance_after, region, processed_at, key = row
    return Transaction(
        id=_uuid(identifier),
        type=movement_type,
        amount=Decimal(amount),
        balance_after=Decimal(balance_after),
        processed_region=region,
        processed_at=_utc(processed_at),
        idempotency_key=key,
    )


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)
