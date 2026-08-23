"""Phoenix adapter tests against mocked HTTP endpoints (respx)."""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import httpx
import pytest
import respx

from deltabot.models import OrderRequest, OrderStatus, Side
from deltabot.venues.base import OrderRejected, VenueError
from deltabot.venues.phoenix.adapter import PhoenixVenue

BASE = "https://perp-api.phoenix.trade"
RPC = "https://rpc.test"

MARKETS = [
    {
        "symbol": "BTC-PERP",
        "assetId": 1,
        "marketStatus": "active",
        "marketPubkey": "11111111111111111111111111111111",
        "splinePubkey": "11111111111111111111111111111111",
        "tickSize": 0.1,
        "baseLotsDecimals": 5,
        "takerFee": 0.0004,
        "makerFee": 0.0001,
        "leverageTiers": [],
        "riskFactors": {},
        "fundingIntervalSeconds": 3600,
        "fundingPeriodSeconds": 3600,
        "maxFundingRatePerInterval": 0.001,
        "maxFundingRatePerIntervalPercentage": 0.1,
        "openInterestCapBaseLots": "0",
        "maxLiquidationSizeBaseLots": "0",
        "isolatedOnly": True,
    }
]

STATS = {
    "symbol": "BTC-PERP",
    "timestamp_ms": 1755900000000,
    "mark_price": 65000.0,
    "oracle_price": 65010.0,
    "prev_day_mark_price": 64000.0,
    "open_interest": 120.5,
    "day_volume_usd": 1e7,
    "day_volume_base": 150.0,
    # Live wire semantics are PERCENT: 0.01%/hour -> 87.6% APR
    "current_funding_rate": 0.01,
    "eight_hour_funding_rate": 0.08,
    "annualized_funding_rate": 87.6,
}

ORDERBOOK = {
    "slot": 1,
    "symbol": "BTC-PERP",
    "bids": [[64990.0, 1.5], [64980.0, 2.0]],
    "asks": [[65010.0, 1.2], [65020.0, 3.0]],
}

# base58 for a fixed 64-byte test keypair is generated in the fixture below


@pytest.fixture
def keypair_b58():
    from solders.keypair import Keypair

    return str(Keypair.from_seed(bytes(range(32))))


def mock_data_routes(router: respx.MockRouter):
    router.get(f"{BASE}/v1/view/exchange/markets").mock(
        return_value=httpx.Response(200, json=MARKETS)
    )
    router.get(f"{BASE}/v1/market/BTC-PERP/stats/latest").mock(
        return_value=httpx.Response(200, json=STATS)
    )
    router.get(f"{BASE}/v1/view/orderbook/BTC-PERP").mock(
        return_value=httpx.Response(200, json=ORDERBOOK)
    )


@respx.mock
async def test_market_spec_from_config():
    mock_data_routes(respx.mock)
    venue = PhoenixVenue()
    spec = await venue.get_market("BTC-PERP")
    assert spec.step_size == Decimal("0.00001")
    assert spec.tick_size == Decimal("0.1")
    assert spec.extra["funding_interval_seconds"] == 3600
    await venue.close()


@respx.mock
async def test_funding_snapshot_hourly():
    mock_data_routes(respx.mock)
    venue = PhoenixVenue()
    funding = await venue.get_funding("BTC-PERP")
    assert funding.rate == Decimal("0.0001")
    assert funding.interval_hours == 1
    # 1bp/hour -> 87.6% APR; matches the API's own annualized figure
    assert abs(funding.annualized - Decimal("0.876")) < Decimal("0.001")
    await venue.close()


@respx.mock
async def test_funding_scale_mismatch_refuses():
    mock_data_routes(respx.mock)
    # annualized reported as a fraction while current stays percent: the
    # internal-consistency check must refuse rather than trade on it
    bad_stats = dict(STATS, annualized_funding_rate=0.876)
    respx.mock.get(f"{BASE}/v1/market/BTC-PERP/stats/latest").mock(
        return_value=httpx.Response(200, json=bad_stats)
    )
    venue = PhoenixVenue()
    with pytest.raises(VenueError, match="funding scale mismatch"):
        await venue.get_funding("BTC-PERP")
    await venue.close()


@respx.mock
async def test_symbol_resolution_bare_and_suffixed():
    """Config says BTC-PERP but the venue lists the market as 'BTC' (or vice
    versa): the client resolves the alias instead of 404ing."""
    bare = [dict(MARKETS[0], symbol="BTC")]
    respx.mock.get(f"{BASE}/v1/view/exchange/markets").mock(
        return_value=httpx.Response(200, json=bare)
    )
    respx.mock.get(f"{BASE}/v1/market/BTC/stats/latest").mock(
        return_value=httpx.Response(200, json=dict(STATS, symbol="BTC"))
    )
    venue = PhoenixVenue()
    funding = await venue.get_funding("BTC-PERP")  # suffixed config, bare venue
    assert funding.rate == Decimal("0.0001")
    spec = await venue.get_market("btc")  # case-insensitive too
    assert spec.venue == "phoenix"
    with pytest.raises(Exception, match="unknown symbol"):
        await venue.get_market("DOGE-PERP")
    await venue.close()


@respx.mock
async def test_top_of_book():
    mock_data_routes(respx.mock)
    venue = PhoenixVenue()
    book = await venue.get_top_of_book("BTC-PERP")
    assert book.bid == Decimal("64990.0")
    assert book.ask == Decimal("65010.0")
    await venue.close()


@respx.mock
async def test_data_only_mode_rejects_orders():
    mock_data_routes(respx.mock)
    venue = PhoenixVenue()
    with pytest.raises(OrderRejected, match="data-only"):
        await venue.place_order(
            OrderRequest(symbol="BTC-PERP", side=Side.BUY, qty=Decimal("0.001"))
        )
    await venue.close()


@respx.mock
async def test_wallet_order_flow_signs_and_sends(keypair_b58):
    from solders.pubkey import Pubkey

    mock_data_routes(respx.mock)

    respx.mock.get(f"{BASE}/v1/auth/nonce").mock(
        return_value=httpx.Response(
            200, json={"nonce_id": "n1", "message": "Sign in to Phoenix: abc",
                       "expires_at": "2026-01-01T00:00:00Z"}
        )
    )
    login_route = respx.mock.post(f"{BASE}/v1/auth/login/wallet").mock(
        return_value=httpx.Response(
            200,
            json={
                "token_type": "Bearer", "access_token": "jwt-token",
                "expires_in": 900, "refresh_token": "refresh",
                "refresh_expires_in": 86400, "pop_key": "cG9w",
            },
        )
    )
    ix_route = respx.mock.post(f"{BASE}/v1/ix/place-isolated-market-order").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "programId": "ComputeBudget111111111111111111111111111111",
                "keys": [],
                "data": [2, 64, 66, 15, 0],
            }],
        )
    )

    tx_signature = "5" * 87
    rpc_calls = []

    def rpc_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rpc_calls.append(body["method"])
        if body["method"] == "getLatestBlockhash":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "value": {"blockhash": "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k",
                          "lastValidBlockHeight": 100}}})
        if body["method"] == "sendTransaction":
            tx_b64 = body["params"][0]
            assert base64.b64decode(tx_b64)  # well-formed transaction bytes
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": tx_signature})
        if body["method"] == "getSignatureStatuses":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
                "value": [{"confirmationStatus": "confirmed", "err": None}]}})
        raise AssertionError(f"unexpected rpc method {body['method']}")

    respx.mock.post(RPC).mock(side_effect=rpc_handler)

    venue = PhoenixVenue(rpc_url=RPC, wallet_private_key=keypair_b58)
    result = await venue.place_order(
        OrderRequest(symbol="BTC-PERP", side=Side.BUY, qty=Decimal("0.001"))
    )
    assert result.status is OrderStatus.FILLED
    assert result.order_id == tx_signature
    assert rpc_calls == ["getLatestBlockhash", "sendTransaction", "getSignatureStatuses"]

    # order request body: correct authority and side, auth header attached
    order_request = json.loads(ix_route.calls[0].request.content)
    assert order_request["side"] == "buy"
    # exact integer lots: 0.001 base units at 5 baseLotsDecimals -> 100 lots
    assert order_request["numBaseLots"] == 100
    assert "quantity" not in order_request
    Pubkey.from_string(order_request["authority"])  # valid pubkey
    assert ix_route.calls[0].request.headers["Authorization"] == "Bearer jwt-token"

    # login used base64url-nopad ed25519 signature over the challenge message
    login_body = json.loads(login_route.calls[0].request.content)
    assert login_body["nonce_id"] == "n1"
    sig_b64 = login_body["signature"]
    padding = "=" * (-len(sig_b64) % 4)
    assert len(base64.urlsafe_b64decode(sig_b64 + padding)) == 64
    await venue.close()


@respx.mock
async def test_positions_parsed_from_trader_state(keypair_b58):
    mock_data_routes(respx.mock)
    respx.mock.get(f"{BASE}/v1/auth/nonce").mock(
        return_value=httpx.Response(200, json={"nonce_id": "n1", "message": "m",
                                               "expires_at": "x"})
    )
    respx.mock.post(f"{BASE}/v1/auth/login/wallet").mock(
        return_value=httpx.Response(200, json={
            "token_type": "Bearer", "access_token": "t", "expires_in": 900,
            "refresh_token": "r", "refresh_expires_in": 1, "pop_key": "cA"})
    )
    venue = PhoenixVenue(rpc_url=RPC, wallet_private_key=keypair_b58)
    authority = venue.trading.authority
    respx.mock.get(f"{BASE}/v1/trader/state/{authority}").mock(
        return_value=httpx.Response(200, json={
            "authority": authority, "traderPdaIndex": 0, "slot": 1, "slotIndex": 0,
            "snapshot": {"version": 1, "capabilities": {}, "makerFeeOverrideMultiplier": 1,
                         "takerFeeOverrideMultiplier": 1, "subaccounts": [
                {"subaccountIndex": 0, "sequence": 1, "collateral": "1500.5",
                 "positions": [], "orders": [], "splines": [], "triggers": []},
                {"subaccountIndex": 1, "sequence": 1, "collateral": "200",
                 "positions": [{"symbol": "BTC-PERP", "positionSequenceNumber": "1",
                                "basePositionLots": "-150000",
                                "entryPriceTicks": "650000",
                                "entryPriceUsd": "65000",
                                "virtualQuotePositionLots": "0",
                                "unsettledFundingQuoteLots": "0",
                                "accumulatedFundingQuoteLots": "0",
                                "takeProfitTriggers": [], "stopLossTriggers": [],
                                "conditionalTakeProfitTriggers": [],
                                "conditionalStopLossTriggers": []}],
                 "orders": [], "splines": [], "triggers": []},
            ]}})
    )
    position = await venue.get_position("BTC-PERP")
    # -150000 lots at 5 baseLotsDecimals -> -1.5 BTC short
    assert position.qty == Decimal("-1.5")
    assert position.entry_price == Decimal("65000")

    balance = await venue.get_balance()
    assert balance.equity == Decimal("1700.5")
    assert balance.available == Decimal("1500.5")
    await venue.close()
