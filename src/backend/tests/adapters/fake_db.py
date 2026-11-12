"""A scripted fake of the pg8000 DB-API connection and cursor (no network)."""

from collections.abc import Sequence
from typing import Any


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection
        self._rows: list[Sequence[Any]] = []

    def execute(self, operation: str, args: Sequence[Any] = ()) -> None:
        self._connection.events.append(("execute", operation, tuple(args)))
        outcome = self._connection.script.pop(0) if self._connection.script else []
        if isinstance(outcome, BaseException):
            raise outcome
        self._rows = list(outcome)

    def fetchone(self) -> Sequence[Any] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> Sequence[Sequence[Any]]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        self._connection.events.append(("cursor_close",))


class FakeConnection:
    """``script`` holds one outcome per ``execute``: a list of rows, or an exception to raise."""

    def __init__(self, script: Sequence[Sequence[Sequence[Any]] | BaseException] = ()) -> None:
        self.script: list[Any] = list(script)
        self.events: list[tuple[Any, ...]] = []
        self.commit_error: BaseException | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.events.append(("commit",))
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.events.append(("rollback",))

    def close(self) -> None:
        self.events.append(("close",))

    def names(self) -> list[str]:
        return [str(event[0]) for event in self.events]

    def statements(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [(event[1], event[2]) for event in self.events if event[0] == "execute"]
