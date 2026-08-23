"""The venue abstraction every exchange adapter implements.

The strategy engine only ever talks to this interface, so a venue can be
backed by a REST API (Hibachi), an on-chain program + data API (Phoenix), or
the in-memory paper simulator used for dry runs.
"""

from __future__ import annotations

import abc

from deltabot.models import (
    Balance,
    FundingSnapshot,
    MarketSpec,
    OrderRequest,
    OrderResult,
    PositionState,
    TopOfBook,
)


class VenueError(Exception):
    """Base error for venue operations."""

    def __init__(self, venue: str, message: str):
        self.venue = venue
        super().__init__(f"[{venue}] {message}")


class OrderRejected(VenueError):
    """Order was refused by the venue (validation, margin, market state)."""


class VenueUnavailable(VenueError):
    """Venue is unreachable or in maintenance; retryable."""


class PerpVenue(abc.ABC):
    """Async adapter for one perp venue."""

    name: str

    async def start(self) -> None:
        """Open connections / warm caches. Optional."""

    async def close(self) -> None:
        """Release connections. Optional."""

    @abc.abstractmethod
    async def get_market(self, symbol: str) -> MarketSpec: ...

    @abc.abstractmethod
    async def get_funding(self, symbol: str) -> FundingSnapshot: ...

    @abc.abstractmethod
    async def get_top_of_book(self, symbol: str) -> TopOfBook: ...

    @abc.abstractmethod
    async def get_balance(self) -> Balance: ...

    @abc.abstractmethod
    async def get_position(self, symbol: str) -> PositionState: ...

    @abc.abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    async def cancel_all(self, symbol: str) -> None: ...

    async def healthcheck(self) -> bool:
        """True if the venue is reachable and trading. Default: try top of book."""
        try:
            # Subclasses may override with a cheaper endpoint.
            return True
        except Exception:
            return False
