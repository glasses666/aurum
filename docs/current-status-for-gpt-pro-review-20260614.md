# Aurum current status for GPT 5.5 Pro review — 2026-06-14

Generated at: 2026-06-14 12:27:58 CST (+0800)

Local branch: `paper-account-sim-spec`

Current commit before this document: `df24c4d [verified] feat: add polymarket market recorder`

This note is intentionally factual and conservative. It is for an external GPT 5.5 Pro review of what is already implemented, what is only partially designed, and what still needs critique.

## 1. Current operating goal

Aurum is in **pre-competition Bitcoin-only paper stability trading**, not live-money trading.

The current goal is to keep a stable paper-trading runtime that can:

1. record public Polymarket market/microstructure data independently;
2. feed a resident mechanical paper bot loop from that recorder;
3. maintain paper-only risk, fills, fee accounting, ledger, dashboard, backups, watchdogs, and maintenance reports;
4. surface failures early before any official contest or live deployment decision.

## 2. Hard safety boundary

Current implementation is intended to be **paper-only**.

Explicit non-goals / forbidden boundaries at this stage:

- no live orders;
- no real funds;
- no wallet/private-key use;
- no CLOB auth credentials required for the recorder;
- no secrets in docs, logs, or review prompts;
- recorder systemd unit does not load the shared `/opt/aurum/.env`.

The deployed recorder service runs as unprivileged `aurum` user, not root.

## 3. Current runtime architecture

```text
Polymarket public APIs
  -> aurum-market-recorder.service
       -> raw append-only JSONL frames
       -> hash-chained manifest.jsonl
       -> normalized latest_markets.json
       -> market_recorder_health.json

agent/review layer
  -> periodically writes/reviews mechanical bot scripts

resident bot layer
  -> aurum-bot-loop.service
       -> every ~15s reads mechanical scripts
       -> loads fresh recorder latest_markets.json only if recorder health is ok
       -> falls back to direct fetch only if recorder missing/stale/unhealthy
       -> mechanically emits buy/sell/hold decisions

paper runner
  -> paper-only risk validation
  -> simulated fills and fees
  -> ledger/state/ticks/events/decisions
  -> dashboard render

automation
  -> watchdog every 10m
  -> backup every 6h
  -> 5h maintenance windows
  -> final stability report job
```

## 4. Implemented code/docs

### Market recorder

Implemented files:

- `scripts/market_recorder.py`
- `scripts/run_market_recorder.sh`
- `deploy/systemd/aurum-market-recorder.service`
- `docs/polymarket-market-recorder.md`
- `tests/test_market_recorder.py`

Recorder captures:

- Gamma public market/search frame;
- CLOB markets frame;
- Data API trades frame;
- CLOB orderbook frames for selected token IDs.

Recorder writes:

```text
/opt/aurum/data/raw/polymarket/YYYY-MM-DD/
  gamma_markets.jsonl
  clob_markets.jsonl
  data_trades.jsonl
  clob_books.jsonl
  manifest.jsonl

/opt/aurum/data/normalized/polymarket/
  latest_markets.json
  latest_summary.json

/opt/aurum/data/reports/
  market_recorder_health.json
```

Manifest entries include:

- `line_sha256`
- `payload_sha256`
- `prev_manifest_sha256`
- `manifest_sha256`

### Bot loop / recorder consumption

Modified files:

- `scripts/agent_bot_loop.py`
- `scripts/generate_dashboard.py`
- `README.md`
- `tests/test_mechanical_bot_scripts.py`

Behavior now:

- bot loop first checks recorder health;
- if health is fresh and ok, it loads `normalized/polymarket/latest_markets.json`;
- if health is stale/missing/unhealthy, it falls back to direct fetch and labels `market_source.source = direct_fetch_fallback`;
- recorder-fed markets still apply `limit` and `min_volume` filters;
- dashboard manifest includes `market_recorder` health.

## 5. Verification already performed

Local verification after implementation:

```text
python3 -m pytest -q tests/test_market_recorder.py tests/test_mechanical_bot_scripts.py
.................. [100%]
18 passed in 0.06s
```

Static added-line scan:

```text
static_scan_findings 0
```

Independent code/security review:

```json
{
  "passed": true,
  "security_concerns": [],
  "logic_errors": [],
  "summary": "Reviewed the staged market recorder diff, verified the prior blockers are resolved, ran the full pytest suite successfully (18 passed), and found no concrete security or logic blockers."
}
```

Remote deployment evidence after cutover:

```text
aurum-market-recorder.service: active / enabled / NRestarts=0
aurum-bot-loop.service: active / enabled / NRestarts=0
```

Remote recorder health sample:

```json
{
  "ok": true,
  "age_seconds": 30.0,
  "market_count": 1,
  "token_count": 2,
  "trade_count": 100,
  "sources": {
    "clob_book": {
      "ok_frames": 2,
      "requested_tokens": 2,
      "errors": 0
    }
  }
}
```

Remote raw recorder counts after several cycles:

```text
gamma_markets.jsonl: 5
clob_markets.jsonl: 5
data_trades.jsonl: 5
clob_books.jsonl: 10
manifest.jsonl: 25
```

Latest remote bot tick consumed recorder data:

```json
{
  "market_source": {
    "source": "polymarket_market_recorder_v0",
    "health_ok": true,
    "max_stale_seconds": 180
  }
}
```

Post-deploy backup evidence:

```text
backup_dir: /Users/dracoglasser/.hermes/reports/aurum-stability/backups/20260614T042227Z
has_raw: true
has_normalized: true
has_reports: true
has_snapshots: true
copied_remote_env: false
changed_remote_state: false
trading_mode_change: false
```

Watchdog manual run after deployment:

```text
stdout_len=0
```

For this script-only watchdog, empty stdout means healthy/silent.

## 6. Previous incident already fixed: fee churn

Before the recorder work, the 15-second resident loop exposed a mechanical bot bug:

- same-tick sell then buy could happen;
- buy exposure checks did not include fee correctly;
- recent-market cooldown was missing;
- repeated rejections and fee bleed polluted the paper ROI sample.

Fixes already implemented and deployed before recorder cutover:

- buy checks estimate fill price/shares/fee/gross cost;
- if there is a sell in a tick, the same tick does not also buy;
- same market default cooldown is 300 seconds;
- tests cover these cases.

Known artifact: dashboard ROI window may still contain old bad historical samples until they age out or are reset.

## 7. Current known limitations / areas needing GPT Pro review

### 7.1 Market recorder limitations

Current recorder is a working v0, but not a complete market-data platform.

Review questions:

1. Is the current raw frame + hash-chained manifest enough for replay/audit, or should every frame also include monotonic local sequence, source-specific sequence, HTTP headers, and response status metadata?
2. Should CLOB orderbook capture be expanded from selected token IDs to all eligible BTC markets, or kept capped for stability and rate-limit control?
3. Should trades be filtered by selected markets/token IDs rather than storing the latest generic `data-api.polymarket.com/trades?limit=100` frame?
4. Is the current BTC universe filter robust enough, or should there be a canonical allowlist/market registry?
5. Should `latest_markets.json` include best bid/ask from captured books instead of only outcome prices from Gamma?
6. Should recorder write a replay cursor / deterministic replay tool now, or is manifest + raw JSONL sufficient for this phase?

### 7.2 Paper engine limitations

Paper execution is still a simplified model.

Review questions:

1. Is the current simulated fill/fee model sufficiently conservative for Polymarket microstructure?
2. Should fills use captured CLOB book bids/asks rather than normalized outcome prices?
3. Should stale orderbook checks block trading even if Gamma price is fresh?
4. Should paper PnL be marked to bid/ask mid, bid for longs, or Gamma outcome price?
5. Should paper account state include slippage and liquidity capacity explicitly?

### 7.3 Agent / context design not yet deeply reviewed

This is the area Queen Glasser explicitly has not reviewed yet.

Current design is mostly:

```text
agent/review layer periodically writes mechanical bot scripts
resident bot loop executes scripts without calling LLM every tick
strategy-review timer can propose/promote script/rule updates
bad JSON from model should not crash service or promote invalid rules
```

Open design questions:

1. What context should each strategy agent receive when writing/updating mechanical scripts?
2. How much history should be summarized vs raw-fed?
3. Should agents see raw orderbook/trade frames, engineered features, or only compact market/account summaries?
4. How do we prevent context contamination between SuperWing and DeepSeek agents?
5. Should the review layer be a tournament judge, a risk officer, or two separate roles?
6. What exact promotion gate moves a proposed script into active resident execution?
7. Should agent output be strictly a DSL/schema instead of free-form markdown plus parser?
8. What is the rollback mechanism for a newly promoted bad script?
9. Should there be a “shadow mode” where new scripts are simulated for N ticks before active use?
10. What permanent memory should agents have, if any, versus run-local state?

### 7.4 Operations limitations

Current system is deployed and watched, but ops maturity is still early.

Review questions:

1. Is systemd hardening sufficient for the recorder and bot loop?
2. Should recorder and bot loop have separate Linux users and stricter write paths?
3. Should watchdog alert on too many `direct_fetch_fallback` ticks?
4. Should backup verify manifest hash chain, not just file inclusion?
5. Should there be a restore drill from backup before official competition?
6. Should public dashboard include a visible “recorder freshness / source” warning when fallback occurs?

## 8. Concrete files to inspect first

Recommended review order:

1. `docs/polymarket-market-recorder.md`
2. `scripts/market_recorder.py`
3. `tests/test_market_recorder.py`
4. `scripts/agent_bot_loop.py`
5. `tests/test_mechanical_bot_scripts.py`
6. `scripts/agent_duel.py`
7. `scripts/bot_scripts.py`
8. `scripts/strategy_review.py`
9. `deploy/systemd/aurum-market-recorder.service`
10. `deploy/systemd/aurum-bot-loop.service`
11. `scripts/generate_dashboard.py`
12. `docs/current-status-for-gpt-pro-review-20260614.md`

## 9. Suggested GPT 5.5 Pro review prompt

```text
You are reviewing Aurum, a pre-competition Polymarket Bitcoin-only paper trading system.

Please review the repository state described in docs/current-status-for-gpt-pro-review-20260614.md and inspect the relevant code files.

Focus on concrete gaps, not cosmetic suggestions. I want a fail-closed review of:

1. Polymarket market-data recorder correctness and replay/audit sufficiency.
2. Whether paper engine execution should use captured CLOB book data rather than Gamma prices.
3. Agent/context design: what context agents should receive, how script promotion should work, and how to prevent contaminated or unstable strategy updates.
4. Safety: any path from paper-only to live-order risk, secret exposure, or unsafe deployment assumptions.
5. Operations: watchdog, backup, restore, manifest verification, service isolation.

Return:
- BLOCKERS before official competition
- SHOULD FIX before 7-day paper contest
- NICE TO HAVE later
- Specific file/function references where possible
- A proposed next implementation sequence
```

## 10. Honest current verdict

Current state is **runtime-complete for a v0 paper stability recorder + resident mechanical bot loop**.

It is **not yet design-complete** for:

- full market microstructure replay;
- official contest-grade fill model;
- agent context architecture;
- robust script promotion governance;
- restore drills and manifest-chain audit tooling.

Recommended next phase: GPT 5.5 Pro review should focus less on “does a recorder exist” and more on whether the **agent context + market feature + paper fill model** are strong enough for a credible 7-day paper contest.
