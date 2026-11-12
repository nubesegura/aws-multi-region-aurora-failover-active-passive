"""Domain error taxonomy. Inbound adapters translate these into transport responses."""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FieldError:
    """One invalid input field and the reason."""

    field: str
    message: str


class DomainError(Exception):
    """Base class of every error the core raises on purpose."""


class ValidationError(DomainError):
    """The input is invalid; ``fields`` lists every offending field."""

    def __init__(self, fields: Iterable[FieldError]) -> None:
        self.fields: tuple[FieldError, ...] = tuple(fields)
        super().__init__("validation failed: " + ", ".join(item.field for item in self.fields))


class InsufficientFunds(DomainError):  # noqa: N818 - name fixed by the spec
    """A withdrawal is above the balance; ``balance`` is the unchanged current balance."""

    def __init__(self, balance: Decimal) -> None:
        self.balance = balance
        super().__init__("insufficient funds")


class DatabaseReadOnly(DomainError):  # noqa: N818 - name fixed by the spec
    """The database accepts reads only (for example a secondary cluster before promotion)."""


class DatabaseUnavailable(DomainError):  # noqa: N818 - name fixed by the spec
    """The database cannot be reached or did not answer."""


class DatabaseBusy(DomainError):  # noqa: N818 - name fixed by the spec
    """The account row lock was not obtained within the statement timeout; nothing was stored."""
