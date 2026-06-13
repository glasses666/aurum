# Bitcoin-only Paper Duel Terminal v2

## Decision recorded

Queen Glasser's corrected target is not a slow hourly LLM suggestion board. Aurum should become a visible paper-trading terminal:

- First 7-day contest window trades **Bitcoin-related Polymarket markets only**.
- Market data is shared: one recorder/universe produces the same Bitcoin slate and frames for every agent.
- Each agent has its own bot, wallet, rules, event log, and ROI/ranking.
- The hot path should become a resident bot loop with a hard **5s minimum interval**, not a cron process spawned every 5 seconds.
- The 5h pro review loop updates strategy rules/prompts only after validation; it cannot expand risk caps, enable live trading, request wallets, or touch keys.

## Dashboard target

The public site should feel like a trading terminal, not a report dashboard:

1. **Left rail** — contest scope, agent state, rank, current equity, ROI, cash, positions, rule excerpt.
2. **Center stage** — one live chart combining:
   - Bitcoin market price/probability from the recorder/snapshot stream;
   - SuperWing ROI;
   - DeepSeek ROI;
   - plotted order/fill points by agent color.
3. **Right rail** — event log with time, agent, action, side, outcome, notional, limit/fill price, market, and reason/rejection/note.

Visual rules:

- Avoid heavy cards.
- Use thin separators, table/terminal density, and mostly black/white with only semantic color.
- Public page stays static HTML generated from local paper-ledger files.
- Public page must never include API keys, SSH info, wallet/private-key fields, or live-order credentials.

## Config defaults

`.env.example` now includes the first-window defaults:

```text
AURUM_DUEL_UNIVERSE=bitcoin
AURUM_DUEL_SEARCH_QUERY=bitcoin
AURUM_FIRST_CONTEST_DAYS=7
AURUM_BOT_MIN_INTERVAL_SEC=5
AURUM_BOT_DEFAULT_INTERVAL_SEC=15
```

## Implementation note

`agent_duel.fetch_markets()` now supports `AURUM_DUEL_UNIVERSE=bitcoin` and searches Gamma public-search for Bitcoin before falling back to the normal markets endpoint. New snapshots keep `condition_id` and `category` so the dashboard/recorder can identify Bitcoin markets cleanly.

`generate_dashboard.py` now renders the trading-terminal layout and reads Bitcoin points from saved snapshots under `data/paper_duel/snapshots/`.

## Still paper-only

This change does not add live trading. Current safety boundary remains:

- no wallet;
- no private key;
- no real CLOB order;
- no USDC;
- no live trading credential;
- no geoblock bypass.
