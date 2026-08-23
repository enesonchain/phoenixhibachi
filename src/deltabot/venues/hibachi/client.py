"""Minimal async REST client for the Hibachi exchange.

Implements exactly the endpoints the bot needs, with request/response shapes
taken from the official SDK and https://api-doc.hibachi.xyz/:

- market data (no auth) on ``data-api.hibachi.xyz``:
    GET /market/exchange-info
    GET /market/data/prices?symbol=
    GET /market/data/orderbook?symbol=&depth=&granularity=
- trading/account (Authorization: <api key>) on ``api.hibachi.xyz``:
    GET    /trade/account/info?accountId=
    GET    /trade/orders?accountId=
    POST   /trade/order
    DELETE /trade/order
    DELETE /trade/orders
    GET    /capital/balance?accountId=
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from deltabot.venues.base import OrderRejected, VenueUnavailable
from deltabot.venues.hibachi.signing import (
    ContractMeta,
    HibachiSigner,
    cancel_payload,
    order_payload,
)

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.hibachi.xyz"
DEFAULT_DATA_API_URL = "https://data-api.hibachi.xyz"


def _now_us() -> int:
    return time.time_ns() // 1_000


def _dec_str(value: Decimal) -> str:
    """Plain decimal string, never exponent notation (2E-7 would be rejected
    and would also diverge from the bytes that were signed)."""
    return format(value, "f")


class HibachiClient:
    """Thin wrapper over Hibachi's REST API with request signing."""

    def __init__(
        self,
        api_key: str,
        account_id: int,
        private_key: str,
        api_url: str = DEFAULT_API_URL,
        data_api_url: str = DEFAULT_DATA_API_URL,
        timeout: float = 10.0,
    ):
        self.account_id = int(account_id)
        self._signer = HibachiSigner(private_key)
        self._api = httpx.AsyncClient(
            base_url=api_url,
            headers={"Authorization": api_key},
            timeout=timeout,
        )
        self._data_api = httpx.AsyncClient(base_url=data_api_url, timeout=timeout)
        self._contracts: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self._api.aclose()
        await self._data_api.aclose()

    # ------------------------------------------------------------------ http

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        json: dict | None = None,
    ) -> Any:
        try:
            response = await client.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise VenueUnavailable("hibachi", f"{method} {path}: {e}") from e
        if response.status_code >= 500 or response.status_code == 429:
            raise VenueUnavailable(
                "hibachi", f"{method} {path} -> {response.status_code}: {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise OrderRejected(
                "hibachi", f"{method} {path} -> {response.status_code}: {response.text[:300]}"
            )
        if not response.content:
            return {}
        body = response.json()
        # /market/exchange-info carries an exchange status; anything but NORMAL
        # means a maintenance window. (Error bodies use status:"failed" but
        # those arrive with 4xx/5xx and were raised above.)
        if isinstance(body, dict) and body.get("status") in (
            "SCHEDULED_MAINTENANCE",
            "UNSCHEDULED_MAINTENANCE",
            "MAINTENANCE",
        ):
            raise VenueUnavailable("hibachi", f"exchange status={body.get('status')}")
        return body

    async def _get_market_data(self, path: str) -> Any:
        return await self._request(self._data_api, "GET", path)

    async def _authed(self, method: str, path: str, json: dict | None = None) -> Any:
        return await self._request(self._api, method, path, json=json)

    # ---------------------------------------------------------- market data

    async def exchange_info(self) -> dict:
        return await self._get_market_data("/market/exchange-info")

    async def contract(self, symbol: str) -> dict:
        """Contract metadata for a symbol (cached after first exchange-info call)."""
        if symbol not in self._contracts:
            info = await self.exchange_info()
            self._contracts = {c["symbol"]: c for c in info["futureContracts"]}
        try:
            return self._contracts[symbol]
        except KeyError:
            raise OrderRejected("hibachi", f"unknown symbol {symbol!r}") from None

    async def prices(self, symbol: str) -> dict:
        """Mark/ask/bid/spot prices plus funding rate estimation."""
        return await self._get_market_data(f"/market/data/prices?symbol={symbol}")

    async def funding_rates(self, symbol: str, limit: int = 10) -> list[dict]:
        """Historical funding settlements: {contractId, fundingTimestamp,
        fundingRate, indexPrice} (timestamps in unix seconds)."""
        body = await self._get_market_data(
            f"/market/data/funding-rates?symbol={symbol}&limit={limit}"
        )
        if isinstance(body, dict):
            return body.get("data", [])
        return body

    async def orderbook(self, symbol: str, depth: int = 5) -> dict:
        contract = await self.contract(symbol)
        granularity = contract["orderbookGranularities"][0]
        return await self._get_market_data(
            f"/market/data/orderbook?symbol={symbol}&depth={depth}&granularity={granularity}"
        )

    # -------------------------------------------------------------- account

    async def account_info(self) -> dict:
        return await self._authed("GET", f"/trade/account/info?accountId={self.account_id}")

    async def capital_balance(self) -> dict:
        return await self._authed("GET", f"/capital/balance?accountId={self.account_id}")

    async def open_orders(self) -> list[dict]:
        body = await self._authed("GET", f"/trade/orders?accountId={self.account_id}")
        if isinstance(body, dict):
            return body.get("orders", [])
        return body

    # -------------------------------------------------------------- trading

    def _contract_meta(self, contract: dict) -> ContractMeta:
        return ContractMeta(
            id=int(contract["id"]),
            underlying_decimals=int(contract["underlyingDecimals"]),
            settlement_decimals=int(contract["settlementDecimals"]),
        )

    async def place_order(
        self,
        symbol: str,
        quantity: Decimal,
        is_buy: bool,
        max_fees_percent: Decimal = Decimal("0.0005"),
        price: Decimal | None = None,
        reduce_only: bool = False,
        post_only: bool = False,
        ioc: bool = False,
        creation_deadline_s: float | None = None,
        client_id: str | None = None,
    ) -> dict:
        """Place a market (price=None) or limit order. Returns {orderId, ...}."""
        contract = await self.contract(symbol)
        nonce = _now_us()
        payload = order_payload(
            self._contract_meta(contract),
            nonce,
            quantity,
            is_buy,
            max_fees_percent,
            price,
        )
        request: dict[str, Any] = {
            "accountId": self.account_id,
            "nonce": nonce,
            "symbol": symbol,
            "quantity": _dec_str(quantity),
            "orderType": "MARKET" if price is None else "LIMIT",
            "side": "BID" if is_buy else "ASK",
            "maxFeesPercent": _dec_str(max_fees_percent),
            "signature": self._signer.sign(payload),
        }
        if price is not None:
            request["price"] = _dec_str(price)
        flags = [f for f, on in (
            ("REDUCE_ONLY", reduce_only), ("POST_ONLY", post_only), ("IOC", ioc)
        ) if on]
        if len(flags) > 1:
            raise OrderRejected("hibachi", f"only one order flag allowed, got {flags}")
        if flags:
            request["orderFlags"] = flags[0]
        if creation_deadline_s is not None:
            request["creationDeadline"] = int((time.time() + creation_deadline_s) * 1_000_000)
        if client_id is not None:
            # Idempotency key: 1-32 chars of [A-Za-z0-9-], unique among
            # active/recent orders; also usable to query/cancel.
            request["clientId"] = client_id
        body = await self._authed("POST", "/trade/order", json=request)
        body["nonce"] = nonce
        return body

    async def order_status(self, order_id: int | str) -> dict:
        return await self._authed(
            "GET", f"/trade/order?accountId={self.account_id}&orderId={order_id}"
        )

    async def cancel_order(self, order_id: int | str) -> dict:
        request = {
            "accountId": self.account_id,
            "orderId": str(order_id),
            "signature": self._signer.sign(cancel_payload(order_id=int(order_id))),
        }
        return await self._authed("DELETE", "/trade/order", json=request)

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """Cancel open orders one by one (the bulk endpoint is unreliable per the
        official SDK, which ships the same workaround). Returns count canceled."""
        orders = await self.open_orders()
        canceled = 0
        for order in orders:
            if symbol is not None and order.get("symbol") != symbol:
                continue
            await self.cancel_order(int(order["orderId"]))
            canceled += 1
        return canceled
