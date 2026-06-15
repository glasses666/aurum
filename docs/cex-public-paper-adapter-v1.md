# Aurum CEX public-data paper adapter v1

This branch moves Aurum's main experiment path from Polymarket prediction markets toward traditional crypto quant while preserving the existing arena discipline.

## Why this exists

Polymarket is an event-probability market. It is useful for AI research, but traditional quant needs structured price/orderbook/funding data rather than event-resolution forecasting.

Aurum should therefore split into two lanes:

- `polymarket`: prediction-market research demo; lower priority until a stronger search/news system exists.
- `cex`: traditional crypto quant paper arena over public exchange data.

## v1 scope

`scripts/cex_arena.py` is a public-data, paper-only compatibility layer. It provides:

- public snapshot normalization for OKX, Bybit, and Binance perpetual APIs;
- a common CEX frame contract with `exchange`, `symbol`, `last`, `best_bid`, `best_ask`, `mid`, `spread_bps`, `funding_rate`, candles, and fee assumptions;
- an exchange-like paper account with cash, positions, trades, fees, and realized PnL;
- recorder-style output at `normalized/cex/latest_markets.json`, `reports/cex_recorder_health.json`, and `raw/cex/<day>/snapshots.jsonl` when `snapshot --data-dir` is used;
- long-only paper market buy/reduce-sell fills by default;
- an explicit safety guard rejecting exchange credential/live-trading env vars.

It does **not** submit live orders, sign requests, read wallets, or require API keys.

## Frame contract

A normalized frame looks like:

```json
{
  "source": "cex_public_api_v0",
  "exchange": "okx",
  "symbol": "BTC-USDT-SWAP",
  "market_type": "cex_perp",
  "last": 65000.1,
  "best_bid": 64999.8,
  "best_ask": 65000.4,
  "mid": 65000.1,
  "spread_bps": 0.0923,
  "funding_rate": 0.0001,
  "fee_bps": 5.0
}
```

This is intentionally different from Polymarket `Yes/No` outcome markets. It should feed traditional quant strategies that reason over price, spread, funding, candles, volatility, and microstructure.

## Paper execution rules

v1 paper fills are deliberately conservative:

- buy uses best ask plus slippage;
- sell/reduce uses best bid minus slippage;
- taker fee is charged as `gross * fee_bps / 10000`;
- shorting is disabled by default;
- no live credentials are allowed in the process environment.

This is enough for BTC/ETH baseline strategies before adding leverage, margin, shorting, maker queue simulation, or exchange testnet support.

## Safety boundary

The CEX adapter fails closed when it sees exchange-scoped credentials such as `OKX_API_KEY`, `BYBIT_API_SECRET`, `BINANCE_API_KEY`, `HYPERLIQUID_PRIVATE_KEY`, or a truthy `AURUM_CEX_ENABLE_LIVE_TRADING`.

Public keys for unrelated local tools are not blocked; only exchange-scoped/live-trading env is treated as unsafe.

## Suggested next steps

1. Add a resident CEX recorder service writing `normalized/cex/latest_markets.json`.
2. Add CEX baseline strategies: momentum, mean reversion, volatility breakout, funding/basis filter, and grid shadow.
3. Add exchange-like replay/backtest with fee/slippage and drawdown gates.
4. Extend dashboard to show a CEX terminal view separate from the Polymarket terminal.
5. Deploy as a new release only after runtime proof; do not mutate the current Polymarket canary in place.
