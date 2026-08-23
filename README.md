# phoenixhibachi

Delta-neutral funding-rate arbitrage bot across two perp DEXes:

- **[Hibachi](https://hibachi.xyz)** — off-chain orderbook perp exchange with a
  documented REST/WebSocket API (USDT-settled, Arbitrum deposits).
- **[Phoenix Perpetuals](https://phoenix.trade)** — Ellipsis Labs' fully
  on-chain perp DEX on Solana (USDC collateral).

The strategy: when the funding-rate spread between the two venues is wide
enough, go **short the venue with the higher funding** (collecting funding)
and **long the other** (paying less), equal size on both legs. Net price
exposure is ~zero; the position earns the funding differential until the
spread collapses, then closes.

## How the Phoenix side works (the "no API" problem)

Phoenix has no *conventional* trading REST API — trading is on-chain — but it
exposes everything a bot needs:

1. **Public data API** (`https://perp-api.phoenix.trade`, no key needed):
   market configs, mark/oracle prices, funding rates, orderbooks.
2. **Instruction-builder API** (`POST /v1/ix/place-isolated-market-order`,
   JWT-authenticated): the server returns ready-made Solana instructions
   (`{programId, keys, data}`) for the requested order.
3. **Local signing + RPC**: the bot assembles those instructions into a
   transaction, signs it with your Solana wallet keypair (the key never
   leaves your machine), and submits it to a Solana RPC node.

Authentication is a JWT obtained by signing a server challenge with the same
wallet (ed25519). Wire formats were taken from the official SDK
([Ellipsis-Labs/rise-public](https://github.com/Ellipsis-Labs/rise-public)).

> **Phoenix access is gated.** As of mid-2026 Phoenix Perpetuals is in a
> structured rollout: your wallet must be onboarded (waitlist, invite code, or
> referral) at phoenix.trade and funded with USDC before the bot can trade
> there. Without an onboarded account, run the bot in paper mode.

The Hibachi side uses the documented REST API with locally-signed orders
(secp256k1/ECDSA over a binary payload — byte-for-byte compatible with the
official `hibachi-xyz` SDK, verified by differential tests).

## Layout

```
src/deltabot/
├── models.py            shared domain models (Decimal end-to-end)
├── config.py            YAML config with ${ENV} interpolation
├── state.py             crash-safe persistent state (atomic writes)
├── main.py              CLI: run / status / close
├── venues/
│   ├── base.py          PerpVenue interface
│   ├── hibachi/         signing.py + client.py + adapter.py
│   ├── phoenix/         client.py (data + auth + tx) + adapter.py
│   └── paper.py         simulator: live data, simulated fills
└── strategy/
    ├── funding.py       spread math and orientation
    ├── risk.py          entry gates and sizing
    ├── execution.py     two-legged execution with unwind-on-failure
    └── engine.py        FLAT→ENTERING→OPEN→EXITING state machine
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -e ".[dev]"
cp config.example.yaml config.yaml
cp .env.example .env      # fill in credentials
```

**Hibachi**: create an API key at hibachi.xyz → gear icon → API. Note the
`accountId`, API key, and private key. Wallet-created accounts sign with
secp256k1 (hex private key); web accounts use the HMAC secret — both work.

**Phoenix**: onboard your wallet at phoenix.trade, deposit USDC, and put the
wallet's base58 keypair + a Solana RPC URL in `.env`. A private RPC endpoint
(Helius, Triton, QuickNode) is strongly recommended.

## Running

```bash
set -a; source .env; set +a

phxbot status --config config.yaml   # funding on both venues + current spread
phxbot run    --config config.yaml   # the loop (paper: true by default!)
phxbot close  --config config.yaml   # emergency: flatten both venues
```

Start with `paper: true` (default): live market data, simulated fills, no
keys needed for Phoenix. Watch the logged funding spread and simulated
entries until you trust the behavior, then set `paper: false`.

## Dashboard

While `phxbot run` is up it serves a control panel at
**http://127.0.0.1:8790** (configurable under `dashboard:`):

- **Live view** — funding spread chart with your entry/exit thresholds drawn
  in, per-venue funding/mark/book/position/equity panels, open-position card
  with estimated accrued funding, KPI tiles (spread, carry, equity, net
  delta), and the incident log.
- **Controls** — pause/resume entries, close the open pair (two-click
  confirm), acknowledge-and-clear a HALT, and tune `entry_apr` / `exit_apr` /
  notional caps live (applied on the next engine tick, no restart).
- With the dashboard attached a HALT no longer exits the process — the bot
  idles frozen so you can inspect the reason and clear it from the UI.

It binds to `127.0.0.1` deliberately: anyone who can reach this port can
close positions. Don't expose it without putting real authentication in
front (SSH tunnel to your server: `ssh -L 8790:127.0.0.1:8790 yourbox`).

## Strategy & risk controls

Every `poll_interval_s` the engine snapshots funding, books, balances, and
positions on both venues, then:

- **Entry** (`FLAT`): requires spread ≥ `entry_apr`, sane top-of-book on both
  venues (≤ `max_book_spread_bps`), venue marks within `max_mark_divergence`,
  and margin headroom (`min_free_collateral_frac` reserved). Sizing is capped
  by `target_notional` / collateral and rounded to both venues' step sizes.
- **Execution**: first leg (`first_leg`, default phoenix — the harder fill),
  confirmed, then the hedge leg for the exact filled quantity. If the hedge
  fails after retries, the first leg is unwound immediately — the bot never
  knowingly carries a naked leg. Any incident triggers a `cooldown_s` pause.
- **Open** (`OPEN`): exits when carry falls below `exit_apr` (hysteresis) or
  the spread flips; rebalances when net delta drifts beyond
  `delta_rebalance_notional`; if one leg vanishes (liquidation, manual
  close), the survivor is flattened immediately.
- **Recovery**: state is persisted atomically; on restart the engine
  reconciles its record against live positions. Unrecognizable live positions
  → `HALTED` (it refuses to trade around positions it doesn't understand).
- **Halt**: an unwind failure or unknown exception halts the bot loudly
  rather than continuing.

Funding normalization: each venue's rate is stored per-interval with its
interval — Phoenix settles hourly, Hibachi every 8 hours (00:00/08:00/16:00
UTC; the adapter re-infers the cadence from settlement history at startup) —
and compared as annualized rates.
Positive funding = longs pay shorts on both venues; the Phoenix adapter
cross-checks its annualization against the API's own figure and refuses to
trade if the scales disagree (protects against fraction/percent drift).

## Tests

```bash
pytest
```

Notable: `tests/test_hibachi_signing.py` verifies our order payloads and
signatures **byte-for-byte against the official Hibachi SDK** when it is
vendored at `tests/vendor_sdk` (gitignored):

```bash
pip download hibachi-xyz --no-deps --python-version 3.13 --only-binary=:all: -d /tmp/hib
unzip -q /tmp/hib/hibachi_xyz-*.whl -d tests/vendor_sdk
pip install aiohttp orjson prettyprinter eth-keys
```

Phoenix adapter tests mock the HTTP/RPC layer and assert the full flow:
challenge signing, JWT attachment, instruction assembly, transaction
signing, and submission.

## Honest limitations

- **Live-fire testing**: order signing and wire formats are verified against
  official SDK sources and by differential/mocked tests, but this repo was
  built without funded accounts on either venue. Run small first.
- Phoenix fills are treated as confirmed-transaction = filled; partial-fill
  edge cases on thin books reconcile on the next tick rather than instantly.
- One symbol pair per process (run two processes for two pairs).
- Funding *prediction* is naive (current rate extrapolated); no historical
  smoothing yet — `GET /market/data/funding-rates` (Hibachi) and
  `/v1/funding/{symbol}/rates` (Phoenix) are wired in the clients if you want
  to add it.
- Not financial advice; perp funding arb carries real risks: liquidation
  during divergence, funding flips, venue halts, withdrawal gates.
