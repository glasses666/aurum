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
- recorder-style output at `normalized/cex/latest_markets.json`, `reports/cex_recorder_health.json`, and `raw/cex/<day>/snapshots.jsonl` when `record-once`, `recorder-loop`, or `snapshot --data-dir` is used;
- multi-symbol resident recorder support via `scripts/run_cex_recorder.sh` and `deploy/systemd/aurum-cex-recorder.service`;
- baseline strategy lanes: `momentum`, `mean_reversion`, `volatility_breakout`, `funding_filter`, and `grid_shadow`;
- resident paper tick support via `scripts/run_cex_bot_loop.sh` and `deploy/systemd/aurum-cex-bot-loop.service`;
- dashboard public CEX summary embedded into the existing terminal manifest/HTML;
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

## Resident canary services

The CEX path is designed to run beside the existing Polymarket paper duel, not replace it in-place:

```bash
# one public-data capture
python3 scripts/cex_arena.py record-once \
  --data-dir data/cex_arena \
  --markets okx:BTC-USDT-SWAP,okx:ETH-USDT-SWAP

# one mechanical paper tick over the latest CEX frame
python3 scripts/cex_arena.py tick \
  --data-dir data/cex_arena \
  --strategies momentum,mean_reversion,volatility_breakout,funding_filter,grid_shadow

# public dashboard picks up CEX summary from AURUM_CEX_DATA_DIR or ../cex_arena
AURUM_CEX_DATA_DIR=data/cex_arena \
  python3 scripts/generate_dashboard.py --data-dir data/paper_duel --output-dir public/dashboard
```

Systemd units added for VPS canary:

- `aurum-cex-recorder.service` — public CEX data capture loop.
- `aurum-cex-bot-loop.service` — resident mechanical CEX paper tick + dashboard refresh.

Both set `AURUM_CEX_ENABLE_LIVE_TRADING=false` and the Python guard still fails closed if exchange-scoped credentials such as `OKX_API_KEY` or `BYBIT_API_SECRET` are present.

## Safety boundary

The CEX adapter fails closed when it sees exchange-scoped credentials such as `OKX_API_KEY`, `BYBIT_API_SECRET`, `BINANCE_API_KEY`, `HYPERLIQUID_PRIVATE_KEY`, or a truthy `AURUM_CEX_ENABLE_LIVE_TRADING`.

Public keys for unrelated local tools are not blocked; only exchange-scoped/live-trading env is treated as unsafe.

## Suggested next steps

1. Run VPS canary services beside the existing Polymarket paper duel and verify public dashboard CEX summary.
2. Add exchange-like replay/backtest with fee/slippage and drawdown gates.
3. Add maker/limit-order simulation only after the public-data/taker-paper lane proves stable.
4. Keep live trading, leverage, shorting, private keys, and real exchange account credentials out of this branch.
