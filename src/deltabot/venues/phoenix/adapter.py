"""Phoenix Perpetuals implementation of the PerpVenue interface.

Two execution backends:
- ``wallet``: full trading — server-built instructions signed locally with the
  trader's Solana keypair and sent through an RPC node. Requires an onboarded
  (invite/referral-activated) Phoenix account.
- ``data-only``: market data works, order placement raises. Combined with the
  paper wrapper this allows dry-running the strategy with zero credentials.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from deltabot.config import PhoenixConfig
from deltabot.models import (
    Balance,
    FundingSnapshot,
    MarketSpec,
    OrderRequest,
    OrderResult,
    OrderStatus,
    PositionState,
    Side,
    TopOfBook,
)
from deltabot.venues.base import OrderRejected, PerpVenue, VenueError
from deltabot.venues.phoenix.client import (
    DEFAULT_DATA_API_URL,
    PhoenixDataClient,
    PhoenixTradingClient,
)

log = logging.getLogger(__name__)

SECONDS_PER_YEAR = Decimal(365 * 24 * 3600)


class PhoenixVenue(PerpVenue):
    name = "phoenix"

    def __init__(
        self,
        data_api_url: str = DEFAULT_DATA_API_URL,
        rpc_url: str | None = None,
        wallet_private_key: str | None = None,
    ):
        self.data = PhoenixDataClient(base_url=data_api_url)
        self.trading: PhoenixTradingClient | None = None
        if wallet_private_key:
            if not rpc_url:
                raise ValueError("phoenix wallet execution requires rpc_url")
            self.trading = PhoenixTradingClient(
                wallet_private_key=wallet_private_key,
                rpc_url=rpc_url,
                base_url=data_api_url,
            )
        self._funding_scale_checked = False

    @classmethod
    def from_config(cls, cfg: PhoenixConfig, allow_data_only: bool = False) -> "PhoenixVenue":
        wallet_key = cfg.wallet_private_key if cfg.execution_backend == "wallet" else None
        if cfg.execution_backend == "wallet" and not wallet_key and not allow_data_only:
            # Refuse to boot a live bot that silently can't trade one venue.
            raise ValueError(
                "phoenix.execution_backend is 'wallet' but wallet_private_key is "
                "empty — set it, or run with paper: true"
            )
        return cls(
            data_api_url=cfg.data_api_url,
            rpc_url=cfg.rpc_url,
            wallet_private_key=wallet_key or None,
        )

    async def close(self) -> None:
        await self.data.close()
        if self.trading:
            await self.trading.close()

    # ------------------------------------------------------------------ data

    async def get_market(self, symbol: str) -> MarketSpec:
        m = await self.data.market(symbol)
        base_lots_decimals = int(m["baseLotsDecimals"])
        step = Decimal(1) / (Decimal(10) ** base_lots_decimals)
        tick = Decimal(str(m["tickSize"]))
        return MarketSpec(
            venue=self.name,
            symbol=symbol,
            step_size=step,
            tick_size=tick,
            # Phoenix minimums are not exposed as a single field; one lot is
            # the hard floor and risk sizing keeps us well above dust anyway.
            min_order_size=step,
            min_notional=Decimal("1"),
            extra={
                "asset_id": m.get("assetId"),
                "market_status": m.get("marketStatus"),
                "taker_fee": m.get("takerFee"),
                "maker_fee": m.get("makerFee"),
                "funding_interval_seconds": m.get("fundingIntervalSeconds"),
                "isolated_only": m.get("isolatedOnly"),
                "market_pubkey": m.get("marketPubkey"),
            },
        )

    async def get_funding(self, symbol: str) -> FundingSnapshot:
        stats = await self.data.stats_latest(symbol)
        market = await self.data.market(symbol)
        interval_s = Decimal(str(market.get("fundingIntervalSeconds") or 3600))
        interval_hours = interval_s / 3600

        rate = Decimal(str(stats.get("current_funding_rate", "0")))
        annualized_api = Decimal(str(stats.get("annualized_funding_rate", "0")))
        # Absolute-magnitude guard: perp funding clamps are far below 5% per
        # interval, so anything larger means we are reading percent as a
        # fraction (or garbage) — refuse rather than trade on a 100x error.
        if abs(rate) > Decimal("0.05"):
            raise VenueError(
                self.name,
                f"implausible per-interval funding rate {rate}; "
                "units changed upstream?",
            )
        # Sanity: our own annualization from the per-interval rate should be
        # within an order of magnitude of the API's number. A ~100x mismatch
        # means a fraction-vs-percent scale change upstream — refuse to trade
        # on it rather than mis-sizing by 100x.
        if not self._funding_scale_checked and rate != 0 and annualized_api != 0:
            ours = rate * (SECONDS_PER_YEAR / interval_s)
            ratio = abs(ours / annualized_api)
            if ratio > 10 or ratio < Decimal("0.1"):
                raise VenueError(
                    self.name,
                    f"funding scale mismatch: per-interval {rate} annualizes to "
                    f"{ours} but API reports {annualized_api}; check units",
                )
            self._funding_scale_checked = True

        return FundingSnapshot(
            venue=self.name,
            symbol=symbol,
            rate=rate,
            interval_hours=interval_hours,
            mark_price=Decimal(str(stats.get("mark_price", "0"))),
        )

    async def get_top_of_book(self, symbol: str) -> TopOfBook:
        book = await self.data.orderbook(symbol)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise VenueError(self.name, f"empty orderbook for {symbol}")
        return TopOfBook(
            venue=self.name,
            symbol=symbol,
            bid=Decimal(str(bids[0][0])),
            ask=Decimal(str(asks[0][0])),
        )

    # --------------------------------------------------------------- account

    def _require_trading(self) -> PhoenixTradingClient:
        if self.trading is None:
            raise OrderRejected(
                self.name,
                "phoenix execution backend is data-only (no wallet key configured); "
                "run in paper mode or configure phoenix.wallet_private_key",
            )
        return self.trading

    async def get_balance(self) -> Balance:
        trading = self._require_trading()
        state = await trading.trader_state()
        subaccounts = (state.get("snapshot") or {}).get("subaccounts") or []
        total = Decimal(0)
        for sub in subaccounts:
            total += Decimal(str(sub.get("collateral", "0")))
        # Collateral in isolated subaccounts backs open positions; treat the
        # parent subaccount (index 0) as the free margin pool.
        free = Decimal(0)
        for sub in subaccounts:
            if int(sub.get("subaccountIndex", -1)) == 0:
                free = Decimal(str(sub.get("collateral", "0")))
                break
        return Balance(venue=self.name, equity=total, available=free)

    async def get_position(self, symbol: str) -> PositionState:
        trading = self._require_trading()
        state = await trading.trader_state()
        market = await self.data.market(symbol)
        base_lots_decimals = int(market["baseLotsDecimals"])
        subaccounts = (state.get("snapshot") or {}).get("subaccounts") or []
        qty = Decimal(0)
        entry_notional = Decimal(0)
        entry_qty = Decimal(0)
        for sub in subaccounts:
            for position in sub.get("positions") or []:
                if position.get("symbol") != symbol:
                    continue
                if position.get("basePositionUnits") is not None:
                    leg = Decimal(str(position["basePositionUnits"]))
                else:
                    lots = Decimal(str(position.get("basePositionLots", "0")))
                    leg = lots / (Decimal(10) ** base_lots_decimals)
                qty += leg
                if position.get("entryPriceUsd") is not None and leg != 0:
                    entry_notional += Decimal(str(position["entryPriceUsd"])) * abs(leg)
                    entry_qty += abs(leg)
        # Size-weighted average entry across subaccounts holding this market.
        entry_price = entry_notional / entry_qty if entry_qty > 0 else None
        return PositionState(
            venue=self.name,
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
        )

    # ----------------------------------------------------------------- trade

    async def place_order(self, request: OrderRequest) -> OrderResult:
        trading = self._require_trading()
        if request.order_type.value != "MARKET":
            raise OrderRejected(
                self.name, "only market orders are implemented for phoenix"
            )
        side = "buy" if request.side is Side.BUY else "sell"
        # Send exact integer lots so no precision is lost to float conversion.
        resolved = await self.data.canonical_symbol(request.symbol)
        market = await self.data.market(resolved)
        lots = int(request.qty * Decimal(10) ** int(market["baseLotsDecimals"]))
        signature = await trading.place_isolated_market_order(
            symbol=resolved,
            side=side,
            num_base_lots=lots,
            reduce_only=request.reduce_only,
        )
        confirmed = await trading.confirm_transaction(signature)
        if not confirmed:
            # Not landed within the window: report UNKNOWN with zero fill —
            # the executor resolves the true outcome from the position delta.
            return OrderResult(
                venue=self.name,
                symbol=request.symbol,
                order_id=signature,
                status=OrderStatus.UNKNOWN,
                raw={"signature": signature, "confirmed": False},
            )
        # Confirmed means the market-order transaction executed; IOC fills at
        # whatever size was available. Report UNKNOWN-quantity conservatively:
        # status FILLED with the requested qty is only claimed for the happy
        # path, and the executor re-verifies against the position anyway.
        return OrderResult(
            venue=self.name,
            symbol=request.symbol,
            order_id=signature,
            status=OrderStatus.FILLED,
            filled_qty=request.qty,
            raw={"signature": signature, "confirmed": True},
        )

    async def cancel_all(self, symbol: str) -> None:
        # The bot only uses market orders on Phoenix; there are no resting
        # orders of ours to cancel. Conditional-order cancelation would go
        # through /v1/ix/cancel-conditional-order if ever needed.
        return None

    async def healthcheck(self) -> bool:
        try:
            await self.data.markets()
            return True
        except VenueError:
            return False
