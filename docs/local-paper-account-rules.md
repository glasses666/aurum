# Aurum Local Paper Account Rules

Status: proposal for Phase 0/1 while waiting for Polymarket Builder/sandbox access.

## Bottom line

Aurum should not wait for Polymarket email access. Build a local simulated account first:

- no wallet;
- no private key;
- no USDC;
- no live orders;
- no geoblock bypass;
- public Polymarket market/orderbook/trade data only;
- local SQLite paper ledger with conservative execution rules.

If official sandbox access arrives later, plug it in as a second execution gateway and compare it against this independent local simulator.

## Simulated account identity

Default account:

- account_id: `aurum-paper-001`
- mode: `paper`
- quote_currency: `paper_usdc`
- starting_equity: `1500`
- reserve_cash: `300`
- tradable_risk_capital: `1200`
- timezone: `Asia/Shanghai`

The simulated account is not a fake Polymarket user. It is a local accounting identity for paper trades.

## Account state

Track at minimum:

- cash_available
- cash_reserved_for_orders
- open_positions_by_token
- open_orders
- realized_pnl
- unrealized_pnl
- equity
- max_equity
- drawdown
- daily_realized_pnl
- consecutive_losses
- blocked_actions

## Hard risk rules

All rules fail closed.

- Single initial trade risk: max `3%` equity, starting at `45` paper USDC.
- Single market total exposure: max `8%` equity, starting at `120`.
- Single correlated theme exposure: max `15%` equity, starting at `225`.
- Total simultaneous open risk: max `35%` equity.
- Daily loss stop: `2.5%` equity.
- Weekly review stop: drawdown reaches `8%`; hard fail at `10%`.
- Consecutive loss stop: stop opening new positions after `4` consecutive losses.
- No single trade may contribute more than `25%` of total 7-day PnL for a pass verdict.
- Top three trades may not contribute more than `50%` of total 7-day PnL for a pass verdict.

## Execution rules

### General

- No midpoint fills.
- Every decision freezes visible data at signal time.
- Every simulated order stores the orderbook snapshot hash.
- Every order stores latency model version and pressure-mode version.
- Partial fills are allowed and must be logged.
- A skipped trade is a valid decision.

### Taker simulation

For a market buy/sell simulation:

1. Wait simulated latency.
2. Reconstruct visible book after latency from recorded deltas.
3. Walk visible book depth.
4. Fill only against available displayed depth.
5. Apply at least one-tick adverse slippage in pressure mode.
6. If depth is insufficient, record partial fill or no fill.

### Maker/passive simulation

For resting-limit paper orders:

1. Order enters a local queue model after latency.
2. It is not filled just because price touches the limit.
3. Fill requires later public trades or book movement consistent with queue progress.
4. Apply conservative queue haircut.
5. Cancel attempts take latency and can fail if a simulated fill happened before cancel arrival.
6. Stale quotes are cancelled by rule, not silently ignored.

## Fishnet/grid shadow lane

Allowed only in paper mode.

Track separately from the baseline ledger:

- quote placements
- cancels
- stale orders
- missed fills
- partial fills
- order-to-fill ratio
- cancel rate
- average resting time
- maker/taker mix
- inventory skew
- worst unresolved exposure
- PnL under pressure mode

Fishnet fails even if PnL is positive when profit depends on unrealistic passive fills, stale quotes, hidden look-ahead, or inventory concentration.

## Initial market universe

Start narrow:

1. high-liquidity short-duration crypto up/down markets;
2. high-liquidity crypto/finance intraday markets;
3. observation-only trending markets;
4. exclude politics, sports, geopolitics, low-liquidity markets, and direct wallet-copy for the first run.

Acceptance for a market before simulated trading:

- token IDs mapped;
- condition ID mapped if available;
- book snapshots received;
- deltas replay cleanly;
- spread/depth above minimum threshold;
- no imminent resolution ambiguity.

## Data model sketch

Suggested SQLite tables:

- markets
- tokens
- orderbook_snapshots
- orderbook_deltas
- trades
- simulated_accounts
- simulated_orders
- simulated_fills
- simulated_positions
- risk_checks
- strategy_signals
- daily_reports
- data_quality_runs

## Pass gates

### Phase 0 read-only recorder passes only if

- selected markets map to CLOB token IDs;
- websocket/orderbook recording is stable for 24h;
- snapshots and deltas replay to coherent books;
- data gaps are visible and reported;
- no look-ahead leakage is possible.

### Phase 1 local paper account passes only if

- every simulated trade is reproducible from stored snapshots/deltas;
- risk engine blocks invalid trades;
- daily reports compute equity, PnL, drawdown, expectancy, violations, concentration;
- pressure-mode results are generated separately;
- no live credentials or wallet keys are required.

### 7-day gold run target

- baseline: `40–100` closed trades over 7 days;
- preferred high-frequency research lane: `150–500` closed trades if market pool supports it;
- absolute minimum for verdict: `24` closed trades, otherwise inconclusive;
- net profit target: `>5%`;
- max drawdown: `<=10%`;
- audit log completeness: `100%`.

## Deployment recommendation

1. Build and test on Draco Mac first: `/Users/dracoglasser/workspace/aurum`.
2. Store data in local SQLite under `data/aurum.sqlite`, gitignored.
3. Run short local capture windows first: 15m, 1h, 6h.
4. Move 24/7 read-only recording to the online Linux node only after preflight.
5. Keep R6C as monitor/backup only; do not touch router defaults.

## Builder/sandbox fallback

If Polymarket later grants official sandbox access:

- add `official_sandbox_gateway` beside `local_paper_gateway`;
- do not delete local simulator;
- compare fill, cancel, partial-fill, rate-limit, and user-channel behavior;
- require sandbox behavior to be documented before trusting it for gate results.

## Non-goals

- No live order placement.
- No private key management.
- No account spoofing.
- No fake KYC / bypass.
- No claiming profitability from backtests alone.
