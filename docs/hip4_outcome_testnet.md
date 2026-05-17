# HIP-4 Outcome Pod B Testnet

`HIP4OutcomeEdgePod` is an experimental Pod B candidate for Hyperliquid HIP-4 outcome tokens.

It is intentionally standalone from the current directional Pod B pipeline, and is not wired as a Pod B replacement for now, because outcomes are binary, spot-like assets with explicit expiry and bounded loss.

## Run Modes

- `observer`: discover markets, read books, compute edges, log signals, no orders.
- `paper`: simulate fills at the visible ask and estimate settlement from reference price.
- `testnet`: send real Hyperliquid testnet IOC buy orders for approved opportunities.

## Short-Expiry Mode

The pod now has a dedicated `SHORT_EXPIRY` edge type for the OpenClaw-style path:

- prioritize markets inside `short_expiry_window_minutes`
- maintain a rolling settlement-aligned price history in the pod state
- compute short-horizon momentum over 30s/60s/180s
- combine distance-to-strike, momentum, YES/NO book probability, book imbalance, and the static probability model
- log every short-expiry assessment, including rejected/warming signals, to `short_expiry_features.csv`

This remains dry-run/paper by default. Real testnet orders still require `mode = "testnet"`,
credentials, and `allow_testnet_orders = true`.

## Reference Prices

The probability model uses a median reference price from configured public sources:

- Binance spot ticker, e.g. `BTCUSDT`
- OKX spot ticker, e.g. `BTC-USDT`, with swap fallback `BTC-USDT-SWAP`
- Bybit spot ticker, e.g. `BTCUSDT`, with linear fallback
- Coinbase Exchange ticker, e.g. `BTC-USD`, with `USDT` fallback
- Kraken ticker, e.g. `XBTUSD`/`BTC`, with `USDT` fallback
- Hyperliquid `allMids`

Quotes outside `max_source_deviation_bps` from the median are rejected before the final median is used.
Each opportunity records accepted and rejected reference sources in its metadata.

## Testnet Command

```bash
export TRIDENT_SECRET_KEY=0x...
export TRIDENT_ACCOUNT_ADDRESS=0x... # optional; derived from key if omitted
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_testnet.toml \
  --mode testnet
```

One-shot smoke run:

```bash
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_testnet.toml \
  --mode observer \
  --once
```

Readiness check without placing orders:

```bash
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_testnet.toml \
  --mode testnet \
  --preflight
```

Funding note: current HIP-4 outcome testnet execution requires quote balance in
`USDH`. A spot `USDC` balance is useful to buy `USDH` on testnet, but the Pod B
capital guard must see `USDH` in `spotClearinghouseState` before it approves
real outcome orders.

Replay the logged signals after a run:

```bash
uv run python -m app.backtest.hip4_outcome_replay \
  --logs-dir logs/hip4_outcome_paper \
  --output logs/hip4_outcome_paper/replay_latest.json
```

## Full Bot Dry-Run Integration

The full dry-run launcher starts this pod as an experimental paper runner by default:

```bash
uv run python -m app.live.trident_dry_run_launcher \
  --config config/trident.toml
```

To disable it for a dry-run:

```bash
uv run python -m app.live.trident_dry_run_launcher \
  --config config/trident.toml \
  --without-hip4-outcome
```

The Docker launcher also includes `hip4-outcome-dry-run` in dry-run mode by default.
It is disabled automatically for `--mode live`.

The default testnet config uses Hyperliquid `allMids` as the settlement-aligned reference for BTC/ETH/HYPE in paper mode. Public venue references can still be enabled for diagnostics, but they should not drive HYPE testnet settlement decisions because HYPE testnet can diverge materially from external spot venues.

The UI exposes:

- `/hip4-outcome`
- `/api/hip4-outcome`

## Outputs

- `logs/hip4_outcome_paper/opportunities.csv`
- `logs/hip4_outcome_paper/decisions.jsonl`
- `logs/hip4_outcome_paper/trades.csv`
- `logs/hip4_outcome_paper/settlements.csv`
- `logs/hip4_outcome_paper/latency_stats.csv`
- `logs/hip4_outcome_paper/edge_decay.csv`
- `logs/hip4_outcome_paper/short_expiry_features.csv`
- `logs/hip4_outcome_paper/reconciliation.jsonl`
- `logs/hip4_outcome_paper/daily_summary.csv`
- `logs/hip4_outcome_status.json`
- `runtime/hip4_outcome_paper_state.json`

## Testnet Reconciliation

In `testnet` mode the pod reconciles open testnet positions against:

- `spotClearinghouseState` for outcome token balances
- `userFillsByTime` for recent fills, matched by `oid` or `cloid`

The result is written to `reconciliation.jsonl` and copied into each open position under `metadata.last_reconciliation`.
This is deliberately HTTP-polling based so the experimental pod can be launched standalone without replacing or coupling into the current Pod B stream machinery.

## Safety

The testnet executor refuses non-testnet URLs when `require_testnet_url = true`.
Orders are capped by:

- `max_position_usdc`
- `max_total_outcome_exposure_usdc`
- `max_per_underlying_outcome_exposure_usdc`
- `max_outcome_markets_open`
- `blocked_opportunity_slices` for review-driven slice guardrails

Only buy-side outcome trades are implemented: `BUY_YES`, `BUY_NO`, and `BUY_BOTH`.
No shorting or martingale logic exists in this pod.

`blocked_opportunity_slices` uses `UNDERLYING:EDGE_TYPE:SIDE` values. The
HYPE-specific testnet blocks from the early review were removed after the
testnet data was judged non-representative; mainnet paper is now the active
dry-run profile and should stay unblocked unless a mainnet-paper review proves
an entry-time guardrail.
