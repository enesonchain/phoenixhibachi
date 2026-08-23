"""Hibachi adapter tests against mocked HTTP endpoints (respx)."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx

from deltabot.models import OrderRequest, OrderStatus, Side
from deltabot.venues.hibachi.adapter import HibachiVenue

API = "https://api.hibachi.xyz"
DATA = "https://data-api.hibachi.xyz"
TEST_KEY = "0x" + "ab" * 32

EXCHANGE_INFO = {
    "status": "NORMAL",
    "feeConfig": {"tradeMakerFeeRate": "0.00015", "tradeTakerFeeRate": "0.00045"},
    "futureContracts": [
        {
            "displayName": "BTC/USDT Perps",
            "id": 2,
            "symbol": "BTC/USDT-P",
            "underlyingSymbol": "BTC",
            "underlyingDecimals": 10,
            "settlementSymbol": "USDT",
            "settlementDecimals": 6,
            "tickSize": "0.1",
            "stepSize": "0.0001",
            "minNotional": "10",
            "minOrderSize": "0.0001",
            "orderbookGranularities": ["0.1", "1"],
            "initialMarginRate": "0.05",
            "maintenanceMarginRate": "0.03",
            "status": "LIVE",
        }
    ],
    "instantWithdrawalLimit": {"lowerLimit": "10", "upperLimit": "100000"},
    "maintenanceWindow": [],
}

PRICES = {
    "symbol": "BTC/USDT-P",
    "markPrice": "65000.0",
    "spotPrice": "64995.0",
    "tradePrice": "65001.0",
    "askPrice": "65002.0",
    "bidPrice": "64998.0",
    "fundingRateEstimation": {
        "estimatedFundingRate": "0.0000125",
        "nextFundingTimestamp": 1756000000,
    },
}


def make_venue() -> HibachiVenue:
    return HibachiVenue(api_key="test-key", account_id=42, private_key=TEST_KEY)


def mock_data(router: respx.MockRouter):
    router.get(f"{DATA}/market/exchange-info").mock(
        return_value=httpx.Response(200, json=EXCHANGE_INFO)
    )
    router.get(f"{DATA}/market/data/prices").mock(
        return_value=httpx.Response(200, json=PRICES)
    )


@respx.mock
async def test_funding_and_market_spec():
    mock_data(respx.mock)
    venue = make_venue()
    funding = await venue.get_funding("BTC/USDT-P")
    assert funding.rate == Decimal("0.0000125")
    assert funding.interval_hours == 1
    assert funding.mark_price == Decimal("65000.0")

    spec = await venue.get_market("BTC/USDT-P")
    assert spec.step_size == Decimal("0.0001")
    assert spec.extra["contract_id"] == 2
    await venue.close()


@respx.mock
async def test_place_market_order_polls_until_filled(monkeypatch):
    mock_data(respx.mock)

    order_route = respx.mock.post(f"{API}/trade/order").mock(
        return_value=httpx.Response(200, json={"orderId": "9001"})
    )
    status_responses = iter([
        {"orderId": "9001", "status": "PENDING", "symbol": "BTC/USDT-P",
         "accountId": 42, "availableQuantity": "0.001", "orderType": "MARKET",
         "side": "BID"},
        {"orderId": "9001", "status": "FILLED", "symbol": "BTC/USDT-P",
         "accountId": 42, "availableQuantity": "0", "totalQuantity": "0.001",
         "orderType": "MARKET", "side": "BID", "price": "65002.0"},
    ])
    respx.mock.get(f"{API}/trade/order").mock(
        side_effect=lambda request: httpx.Response(200, json=next(status_responses))
    )

    import asyncio as aio
    monkeypatch.setattr(aio, "sleep", lambda *_: _instant())

    venue = make_venue()
    result = await venue.place_order(
        OrderRequest(symbol="BTC/USDT-P", side=Side.BUY, qty=Decimal("0.001"))
    )
    assert result.status is OrderStatus.FILLED
    assert result.filled_qty == Decimal("0.001")
    assert result.avg_price == Decimal("65002.0")

    body = json.loads(order_route.calls[0].request.content)
    assert body["accountId"] == 42
    assert body["symbol"] == "BTC/USDT-P"
    assert body["orderType"] == "MARKET"
    assert body["side"] == "BID"
    assert body["quantity"] == "0.001"
    assert "price" not in body
    assert len(body["signature"]) == 130  # ECDSA r||s||v hex
    assert order_route.calls[0].request.headers["Authorization"] == "test-key"
    await venue.close()


async def _instant():
    return None


@respx.mock
async def test_reduce_only_flag_and_rejection():
    mock_data(respx.mock)
    respx.mock.post(f"{API}/trade/order").mock(
        return_value=httpx.Response(
            400, json={"errorCode": 4, "message": "bad input", "status": "failed"}
        )
    )
    venue = make_venue()
    from deltabot.venues.base import OrderRejected

    with pytest.raises(OrderRejected):
        await venue.place_order(
            OrderRequest(
                symbol="BTC/USDT-P", side=Side.SELL, qty=Decimal("0.001"),
                reduce_only=True,
            )
        )
    await venue.close()


@respx.mock
async def test_maintenance_status_is_unavailable():
    respx.mock.get(f"{DATA}/market/exchange-info").mock(
        return_value=httpx.Response(
            200, json={**EXCHANGE_INFO, "status": "SCHEDULED_MAINTENANCE"}
        )
    )
    venue = make_venue()
    from deltabot.venues.base import VenueUnavailable

    with pytest.raises(VenueUnavailable, match="MAINTENANCE"):
        await venue.client.exchange_info()
    await venue.close()


@respx.mock
async def test_position_parsing_short_direction():
    mock_data(respx.mock)
    respx.mock.get(f"{API}/trade/account/info").mock(
        return_value=httpx.Response(200, json={
            "balance": "5000.0",
            "maximalWithdraw": "4200.0",
            "assets": [],
            "positions": [{
                "symbol": "BTC/USDT-P", "quantity": "0.5", "direction": "Short",
                "openPrice": "64000.0", "markPrice": "65000.0",
                "entryNotional": "32000", "notionalValue": "32500",
                "unrealizedFundingPnl": "1.5", "unrealizedTradingPnl": "-500",
            }],
            "totalOrderNotional": "0", "totalPositionNotional": "32500",
            "totalUnrealizedFundingPnl": "1.5", "totalUnrealizedPnl": "-498.5",
            "totalUnrealizedTradingPnl": "-500", "numFreeTransfersRemaining": 1,
            "tradeMakerFeeRate": "0.00015", "tradeTakerFeeRate": "0.00045",
        })
    )
    venue = make_venue()
    position = await venue.get_position("BTC/USDT-P")
    assert position.qty == Decimal("-0.5")
    assert position.entry_price == Decimal("64000.0")
    assert position.unrealized_pnl == Decimal("-498.5")

    balance = await venue.get_balance()
    assert balance.equity == Decimal("5000.0")
    assert balance.available == Decimal("4200.0")
    await venue.close()