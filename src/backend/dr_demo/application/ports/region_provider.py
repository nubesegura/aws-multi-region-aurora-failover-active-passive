"""Port: the region serving the current request."""

from typing import Protocol


class RegionProvider(Protocol):
    def region(self) -> str:
        """Return the name of the region that runs this code."""
        ...
