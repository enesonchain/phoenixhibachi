# Venue API reference notes

Condensed from primary sources (Aug 2026): the official `hibachi-xyz` Python
SDK v0.3.1 (PyPI / github.com/hibachi-xyz/hibachi_sdk) and the official
Phoenix "Rise" SDK (github.com/Ellipsis-Labs/rise-public, TS v0.4.x). Useful
when extending the adapters.

## Hibachi (hibachi.xyz)

Base URLs
- Trading/account REST: `https://api.hibachi.xyz` — header `Authorization: <api key>`
- Market data REST (no auth): `https://data-api.hibachi.xyz`
- WebSockets: `wss://data-api.hibachi.xyz/ws/market` (public),
  `wss://api.hibachi.xyz/ws/account?accountId=N`,
  `wss://api.hibachi.xyz/ws/trade?accountId=N` (both with Authorization header)

Conventions
- Symbols: `BTC/USDT-P`, `ETH/USDT-P`, `SOL/USDT-P` (linear USDT perps;
  USDT-on-Arbitrum collateral). Contract ids from `/market/exchange-info`.
- JSON uses decimal strings for prices/quantities. Prices multiples of
  `tickSize`, quantities of `stepSize`.
- `nonce`: client-generated epoch microseconds; doubles as an order handle.
- `maxFeesPercent` (required on place/modify): max accepted fee as a decimal
  fraction (`0.00045` = 4.5 bps taker default), part of the signature.
- Order flags: single optional value of `POST_ONLY | IOC | REDUCE_ONLY`.
- Funding: settled hourly; estimate at `/market/data/prices`
  (`fundingRateEstimation {estimatedFundingRate, nextFundingTimestamp}`),
  history at `/market/data/funding-rates?contractId=N`, payments in
  `/trade/account/settlements_history`.

Order signing (see `venues/hibachi/signing.py`, verified byte-for-byte
against the SDK): big-endian binary payload
`nonce(8) | contractId(4) | qty*10^underlyingDecimals(8) | side(4: 0=ASK,1=BID)
| [price*2^32*10^(settlementDecimals-underlyingDecimals)](8, limit only)
| maxFeesPercent*10^8(8)`; sign sha256(payload) with secp256k1, hex `r||s||v`
(65 bytes). Cancel signs `orderId(8)` or `nonce(8)`. Web accounts replace
ECDSA with HMAC-SHA256(secret, payload) hex.

Endpoints used by the bot
```
GET    /market/exchange-info
GET    /market/data/prices?symbol=
GET    /market/data/orderbook?symbol=&depth=&granularity=
GET    /market/data/funding-rates?contractId=
GET    /trade/account/info?accountId=
GET    /trade/orders?accountId=
GET    /trade/order?accountId=&orderId=
POST   /trade/order        (place; accountId in body)
DELETE /trade/order        (cancel)
GET    /capital/balance?accountId=
```
Errors: non-2xx JSON `{errorCode, message, status:"failed"}`; codes seen:
2=bad signature, 3=not found, 4=bad input. 429 carries
`{name, count, limit, windowDuration}`. Exchange-info `status` may announce
maintenance windows. No public testnet.

## Phoenix Perpetuals (phoenix.trade / "Rise")

Base: `https://perp-api.phoenix.trade`; WS: `wss://perp-api.phoenix.trade/v1/ws`.
Fully on-chain CLOB on Solana (mainnet program
`EtrnLzgbS7nMMy5fbD42kXiUzGg8XQzJ972Xtk1cjWih`); USDC collateral; subaccount
0 = cross margin, 1+ = isolated. Taker 3.5 bps / maker 0.5 bps (per-market
fields). **Access is gated** (waitlist / invite `POST /v1/invite/activate` /
referral / builder onboarding) as of Aug 2026.

Public data (no auth)
```
GET /v1/view/exchange/markets            market configs: tickSize,
                                         baseLotsDecimals, fees, leverageTiers,
                                         fundingIntervalSeconds (3600), ...
GET /v1/market/{symbol}/stats/latest     snake_case: mark_price, oracle_price,
                                         current_funding_rate (per interval),
                                         eight_hour_funding_rate,
                                         annualized_funding_rate, open_interest
GET /v1/view/orderbook/{symbol}          {slot, bids: [[px, sz]...], asks: ...}
GET /v1/funding/{symbol}/rates           history {timestamp(s),
                                         fundingRatePercentage(string)}
GET /v1/candles/{symbol}?timeframe=&limit=
GET /v1/view/exchange/status             {active, gated, withdrawals_available}
```
Funding: hourly settlement of a 24h-normalized mark-index premium; positive
rate → longs pay shorts; annualized = per-interval × (31,536,000 /
fundingIntervalSeconds).

Auth (JWT)
```
GET  /v1/auth/nonce?wallet_pubkey=X      -> {nonce_id, message, expires_at}
POST /v1/auth/login/wallet               {wallet_pubkey, signature, nonce_id}
POST /v1/auth/refresh                    {refresh_token}
```
`signature` = ed25519 signature over the challenge `message` bytes by the
wallet key, base64url-encoded without padding. Session:
`{access_token, expires_in, refresh_token, ...}` used as `Bearer`.

Trading (Bearer)
```
GET  /v1/trader/state/{authority}        snapshot.subaccounts[]: collateral,
                                         positions[{symbol, basePositionLots,
                                         basePositionUnits?, entryPriceUsd?}]
POST /v1/ix/place-isolated-market-order  {authority, symbol, side: buy|sell,
                                         quantity | numBaseLots, isReduceOnly?,
                                         transferAmount?, maxPriceInTicks?}
                                         -> [{programId, keys:[{pubkey,
                                         isSigner, isWritable}], data:[u8]}]
POST /v1/ix/place-isolated-limit-order   (+ -enhanced, conditionals, stop-loss
                                         and cancel variants)
```
The instruction JSON is assembled into a Solana transaction, signed by the
trader authority keypair, and sent through any RPC (`getLatestBlockhash` →
`sendTransaction` → `getSignatureStatuses`). Lots→units: units = lots /
10^baseLotsDecimals. `quantity` fields are base units (floats).

WS channels (subscribe `{"type":"subscribe","subscription":{"channel":...}}`):
`market{symbol}`, `orderbook{symbol}`, `trades{symbol}`,
`candles{symbol,timeframe}`, `fundingRate{symbol}`, `exchange`, `allMids`,
`traderState{authority,traderPdaIndex}`, `l2Book`, `markPrice`, `fills`.
