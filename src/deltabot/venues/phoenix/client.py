"""Phoenix Perpetuals (phoenix.trade, Ellipsis Labs "Rise") clients.

Wire formats mirror the official SDK at github.com/Ellipsis-Labs/rise-public
(TypeScript SDK v0.4.x):

Public data (no auth), base https://perp-api.phoenix.trade:
    GET /v1/view/exchange/markets                 market configs (tickSize,
                                                  baseLotsDecimals, fees,
                                                  fundingIntervalSeconds, ...)
    GET /v1/market/{symbol}/stats/latest          mark/oracle price + funding
    GET /v1/view/orderbook/{symbol}               {bids: [[px, sz]], asks: ...}

Auth (JWT):
    GET  /v1/auth/nonce?wallet_pubkey=..          -> {nonce_id, message}
    POST /v1/auth/login/wallet                    {wallet_pubkey, signature,
                                                   nonce_id} -> {access_token,
                                                   refresh_token, expires_in}
    POST /v1/auth/refresh                         {refresh_token}
    The signature is the wallet's ed25519 signature over the challenge
    ``message`` bytes, base64url-encoded without padding.

Trading (Bearer auth):
    GET  /v1/trader/state/{authority}             positions + collateral
    POST /v1/ix/place-isolated-market-order       -> [{programId, keys, data}]
    The response instructions are assembled into a Solana transaction, signed
    by the trader keypair locally, and submitted to an RPC node.
"""

from __future__ import annotations

import base64
import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from deltabot.venues.base import OrderRejected, VenueUnavailable

log = logging.getLogger(__name__)

DEFAULT_DATA_API_URL = "https://perp-api.phoenix.trade"


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _wrap_http(method: str, url: str, response: httpx.Response) -> Any:
    if response.status_code >= 500 or response.status_code == 429:
        raise VenueUnavailable(
            "phoenix", f"{method} {url} -> {response.status_code}: {response.text[:300]}"
        )
    if response.status_code >= 400:
        raise OrderRejected(
            "phoenix", f"{method} {url} -> {response.status_code}: {response.text[:300]}"
        )
    return response.json() if response.content else {}


class PhoenixDataClient:
    """Unauthenticated market data."""

    def __init__(self, base_url: str = DEFAULT_DATA_API_URL, timeout: float = 10.0):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._markets: dict[str, dict] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise VenueUnavailable("phoenix", f"GET {path}: {e}") from e
        return _wrap_http("GET", path, response)

    async def markets(self) -> list[dict]:
        return await self._get("/v1/view/exchange/markets")

    async def market(self, symbol: str) -> dict:
        if symbol not in self._markets:
            self._markets = {m["symbol"]: m for m in await self.markets()}
        try:
            return self._markets[symbol]
        except KeyError:
            known = ", ".join(sorted(self._markets)) or "<none>"
            raise OrderRejected(
                "phoenix", f"unknown symbol {symbol!r}; known: {known}"
            ) from None

    async def stats_latest(self, symbol: str) -> dict:
        """{symbol, mark_price, oracle_price, current_funding_rate,
        eight_hour_funding_rate, annualized_funding_rate, ...}"""
        return await self._get(f"/v1/market/{symbol}/stats/latest")

    async def orderbook(self, symbol: str) -> dict:
        """{slot, symbol, bids: [[price, size], ...], asks: [[price, size], ...]}"""
        return await self._get(f"/v1/view/orderbook/{symbol}")


class PhoenixTradingClient:
    """Authenticated trading: JWT auth, server-built instructions, local
    signing with the trader's Solana keypair, submission over RPC.

    Requires an onboarded Phoenix account (the wallet must have been activated
    on phoenix.trade — via invite, referral, or builder onboarding — before
    API trading will be accepted).
    """

    def __init__(
        self,
        wallet_private_key: str,  # base58-encoded Solana keypair (64 bytes)
        rpc_url: str,
        base_url: str = DEFAULT_DATA_API_URL,
        timeout: float = 15.0,
    ):
        from solders.keypair import Keypair  # local import: optional dependency

        self.keypair = Keypair.from_base58_string(wallet_private_key)
        self.authority = str(self.keypair.pubkey())
        self.rpc_url = rpc_url
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._rpc = httpx.AsyncClient(timeout=timeout)
        self._access_token: str | None = None
        self._access_expiry: float = 0.0
        self._refresh_token: str | None = None

    async def close(self) -> None:
        await self._http.aclose()
        await self._rpc.aclose()

    # ------------------------------------------------------------------ auth

    async def _login(self) -> None:
        nonce = await self._request("GET", "/v1/auth/nonce",
                                    params={"wallet_pubkey": self.authority}, auth=False)
        message: str = nonce["message"]
        signature = self.keypair.sign_message(message.encode())
        body = {
            "wallet_pubkey": self.authority,
            "signature": _b64url_nopad(bytes(signature)),
            "nonce_id": nonce["nonce_id"],
        }
        session = await self._request("POST", "/v1/auth/login/wallet", json=body, auth=False)
        self._store_session(session)
        log.info("phoenix: authenticated as %s", self.authority)

    async def _refresh(self) -> None:
        if not self._refresh_token:
            await self._login()
            return
        try:
            session = await self._request(
                "POST", "/v1/auth/refresh",
                json={"refresh_token": self._refresh_token}, auth=False,
            )
            self._store_session(session)
        except (OrderRejected, VenueUnavailable):
            log.info("phoenix: token refresh failed; performing full login")
            await self._login()

    def _store_session(self, session: dict) -> None:
        self._access_token = session["access_token"]
        self._refresh_token = session.get("refresh_token", self._refresh_token)
        # renew one minute before expiry
        self._access_expiry = time.time() + float(session.get("expires_in", 300)) - 60

    async def _ensure_auth(self) -> None:
        if self._access_token is None:
            await self._login()
        elif time.time() >= self._access_expiry:
            await self._refresh()

    # ------------------------------------------------------------------ http

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
        auth: bool = True,
    ) -> Any:
        headers = {}
        if auth:
            await self._ensure_auth()
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            response = await self._http.request(
                method, path, params=params, json=json, headers=headers
            )
        except httpx.HTTPError as e:
            raise VenueUnavailable("phoenix", f"{method} {path}: {e}") from e
        if response.status_code == 401 and auth:
            # stale token: force one re-auth and retry
            self._access_token = None
            await self._ensure_auth()
            headers["Authorization"] = f"Bearer {self._access_token}"
            try:
                response = await self._http.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as e:
                raise VenueUnavailable("phoenix", f"{method} {path}: {e}") from e
        return _wrap_http(method, path, response)

    # ----------------------------------------------------------------- state

    async def trader_state(self) -> dict:
        """{authority, traderPdaIndex, slot, snapshot: {subaccounts: [
        {subaccountIndex, collateral, positions: [{symbol, basePositionLots,
        basePositionUnits?, entryPriceUsd?, ...}], orders: [...]}]}}"""
        return await self._request("GET", f"/v1/trader/state/{self.authority}")

    # ----------------------------------------------------------------- trade

    async def place_isolated_market_order(
        self,
        symbol: str,
        side: str,  # "buy" | "sell"
        quantity: Decimal,
        reduce_only: bool = False,
        transfer_amount_atoms: int | None = None,
        max_price_in_ticks: int | None = None,
    ) -> str:
        """Build (server-side), sign (locally), and submit an isolated market
        order. Returns the transaction signature."""
        request: dict[str, Any] = {
            "authority": self.authority,
            "symbol": symbol,
            "side": side,
            "quantity": float(quantity),
        }
        if reduce_only:
            request["isReduceOnly"] = True
        if transfer_amount_atoms is not None:
            request["transferAmount"] = int(transfer_amount_atoms)
        if max_price_in_ticks is not None:
            request["maxPriceInTicks"] = int(max_price_in_ticks)
        instructions = await self._request(
            "POST", "/v1/ix/place-isolated-market-order", json=request
        )
        if not isinstance(instructions, list) or not instructions:
            raise OrderRejected("phoenix", f"no instructions returned: {instructions}")
        return await self._sign_and_send(instructions)

    # ------------------------------------------------------------------- rpc

    async def _rpc_call(self, method: str, params: list) -> Any:
        try:
            response = await self._rpc.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
        except httpx.HTTPError as e:
            raise VenueUnavailable("phoenix", f"rpc {method}: {e}") from e
        body = response.json()
        if "error" in body:
            raise OrderRejected("phoenix", f"rpc {method} error: {body['error']}")
        return body["result"]

    async def _sign_and_send(self, api_instructions: list[dict]) -> str:
        from solders.hash import Hash  # local imports: optional dependency
        from solders.instruction import AccountMeta, Instruction
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction

        instructions = []
        for api_ix in api_instructions:
            accounts = [
                AccountMeta(
                    pubkey=Pubkey.from_string(k["pubkey"]),
                    is_signer=bool(k["isSigner"]),
                    is_writable=bool(k["isWritable"]),
                )
                for k in api_ix["keys"]
            ]
            instructions.append(
                Instruction(
                    Pubkey.from_string(api_ix["programId"]),
                    bytes(api_ix["data"]),
                    accounts,
                )
            )

        blockhash_info = await self._rpc_call(
            "getLatestBlockhash", [{"commitment": "confirmed"}]
        )
        blockhash = Hash.from_string(blockhash_info["value"]["blockhash"])
        message = Message.new_with_blockhash(
            instructions, self.keypair.pubkey(), blockhash
        )
        tx = Transaction([self.keypair], message, blockhash)
        tx_b64 = base64.b64encode(bytes(tx)).decode()
        signature = await self._rpc_call(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        )
        log.info("phoenix: sent transaction %s", signature)
        return signature

    async def confirm_transaction(self, signature: str, timeout_s: float = 30.0) -> bool:
        """Poll until the transaction is confirmed or the timeout elapses."""
        import asyncio

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            result = await self._rpc_call("getSignatureStatuses", [[signature]])
            status = (result.get("value") or [None])[0]
            if status is not None:
                if status.get("err") is not None:
                    raise OrderRejected(
                        "phoenix", f"transaction {signature} failed: {status['err']}"
                    )
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(1.0)
        return False
