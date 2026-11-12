"""Port: identifier generation."""

from typing import Protocol
from uuid import UUID


class IdGenerator(Protocol):
    def new_id(self) -> UUID:
        """Return a new unique identifier."""
        ...
