# DeepSeek Agent Duel — paper-wallet competition

## Verdict

Use a **thin local paper-wallet harness** inside Aurum, not a black-box trading bot.

GitHub scout result:

1. `agent-next/polymarket-paper-trader` is the closest match for Polymarket paper trading:
   - MIT license;
   - Python;
   - built for AI agents;
   - supports paper accounts, live order books, MCP tooling, and P&L tracking;
   - useful reference / later integration candidate.
2. `SpartanLabsXyz/simmer-sdk` is powerful but asks for an external Simmer API key and includes live/paper multi-venue behavior. Keep it out of the first local-only lane.
3. `TauricResearch/TradingAgents` and `virattt/ai-hedge-fund` are strong LLM-finance frameworks, but they are stock/portfolio research systems and heavier than needed for first Polymarket paper testing.
4. `LangGraph` is a good orchestration substrate later if the decision loop needs durable multi-step state, but Phase 0 does not need that complexity.

Decision: start with Aurum-owned stdlib Python harness for auditable local state. The corrected hot path is a resident mechanical bot loop: the agent/review layer writes versioned JSON bot scripts, and the bot loop executes those scripts mechanically without a per-tick LLM call.

## Safety boundary

This duel is **paper-only**:

- no real Polymarket account;
- no wallet connection;
- no private key / seed phrase;
- no USDC;
- no live order endpoint;
- no geoblock bypass;
- no third-party black-box trading bot custody.

The word “wallet” means a local virtual ledger in JSON/SQLite, denominated in `paper_usdc`.

## Accounts

Default accounts:

- `superwing`: SuperWing/Hermes baseline strategy account.
- `deepseek`: DeepSeek self-decision account.

Starting balance per account:

- `starting_equity`: `1500 paper_usdc`
- `reserve_cash`: `300 paper_usdc`
- `tradable_risk_capital`: `1200 paper_usdc`

## Competition score

Score is mark-to-market ROI minus explicit anti-YOLO penalties:

```text
portfolio_value = cash + sum(position_shares * latest_outcome_price)
roi = (portfolio_value - starting_equity) / starting_equity
raw_roi_score = roi * 100
score = raw_roi_score - fee_churn - drawdown - yolo_exposure penalties
```

Rank by `score`, but victory is stricter than rank: a valid winner requires rank #1 **and** ROI `> 5%` after fees within the defined scoring window. If rank #1 has ROI `<= 5%`, the final report must say `no valid victory / no winner` even if that account leads the scoreboard.

Scoreboard context exposes each agent's own ledger/fills/risk events to that agent, plus peer aggregate rank/score/ROI/drawdown/cash/exposure/order-count fields. It must not expose peer raw trades, prompts, or strategy internals on the per-agent hot path.

Also report risk metrics:

- open risk;
- per-market exposure;
- number of trades;
- realized/unrealized P&L;
- max daily drawdown once time series exists;
- invalid/rejected orders.

## Risk limits

Defaults from local paper account rules:

- per-trade notional <= 3% of equity (`45 paper_usdc` at 1500);
- per-market exposure <= 8% of equity (`120 paper_usdc`);
- correlated theme exposure <= 15% of equity;
- total open risk <= 35% of equity;
- daily loss stop = 2.5%;
- stop opening new risk after 4 consecutive losses until review.

The runner enforces hard caps regardless of what DeepSeek asks for.

## Decision contract for DeepSeek

DeepSeek receives a compact market slate and the account state, then must output JSON only:

```json
{
  "agent_id": "deepseek",
  "orders": [
    {
      "market_id": "string",
      "outcome": "Yes or No or exact listed outcome",
      "side": "buy",
      "notional": 25,
      "limit_price": 0.62,
      "confidence": 0.58,
      "rationale": "one sentence"
    }
  ],
  "notes": "brief reason for holding or trading"
}
```

Allowed side in the corrected mechanical-bot lane: `buy` and `sell`. Sells are paper-only position exits triggered by scripted take-profit / stop-loss / max-hold rules after the ledger has an open position.

## Fill model

First slice uses conservative taker-style mark fills:

- buy price = observed outcome price + slippage buffer;
- no midpoint fantasy fills;
- rejected if above limit price;
- rejected if risk caps would be breached;
- partial fills and maker queue simulation are future work.

## Local files

Default runtime directory on the server:

```text
/opt/aurum/data/paper_duel/
  state.json          # virtual wallets, positions, fills
  decisions.jsonl     # prompts/decisions, no API keys
  events.jsonl        # fills/rejections/scores
```

Local repo script:

```bash
python3 scripts/agent_duel.py --data-dir /opt/aurum/data/paper_duel init
python3 scripts/agent_duel.py --data-dir /opt/aurum/data/paper_duel decide --agent superwing
python3 scripts/agent_duel.py --data-dir /opt/aurum/data/paper_duel decide --agent deepseek
python3 scripts/agent_duel.py --data-dir /opt/aurum/data/paper_duel score
```

## DeepSeek configuration

Do not put keys in chat. Put them in a chmod-600 `.env` on the server:

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1/chat/completions
DEEPSEEK_MODEL=deepseek-v4-flash

# Default: review-only. DeepSeek may propose decisions, but cannot alter paper wallet state.
AURUM_DEEPSEEK_ALLOW_PAPER_APPLY=false
AURUM_DEEPSEEK_OPERATOR_CONFIRM=

# Runner-enforced caps. DeepSeek cannot override them from its JSON response.
AURUM_DEEPSEEK_MAX_ORDERS=2
AURUM_DEEPSEEK_MAX_NOTIONAL=45
AURUM_DEEPSEEK_TEMPERATURE=0.2
AURUM_DEEPSEEK_THINKING=enabled
AURUM_DEEPSEEK_REASONING_EFFORT=high
AURUM_DUEL_MODE=review_only
```

To let DeepSeek apply paper fills after reviewing a dry-run, the operator must change both gates locally and upload the env again:

```text
AURUM_DEEPSEEK_ALLOW_PAPER_APPLY=true
AURUM_DEEPSEEK_OPERATOR_CONFIRM=ALLOW_DEEPSEEK_PAPER_APPLY
```

Without `DEEPSEEK_API_KEY`, the runner can initialize wallets and run the `superwing` baseline, but it cannot actually ask DeepSeek to self-decide.

## First deployment gate

Before starting recurring decisions:

1. Server direct Polymarket preflight must pass.
2. Virtual wallet state must initialize cleanly.
3. `score` must run before any trades.
4. `superwing` dry-run must execute without external keys.
5. DeepSeek key must be provided locally, not in chat.
6. First DeepSeek decision must be one-shot and human-auditable before scheduling repeated runs.
