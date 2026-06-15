# V1 Codex implementation spec — 2026-06-15

给 Codex Desktop 的完整实现规格。短 `/goal` 只需要引用本文件即可。

## Working context

- Repo: `the current Aurum repo`
- Branch: `paper-account-sim-spec`
- Work mode: implement working code/docs/tests, not just plans or TODOs.
- Keep changes rollback-friendly with clear local commit(s) after verification.

## Hard boundaries

- Paper-only.
- Do **not** add real wallets, CLOB credentials, private keys, live order endpoints, KYC/login automation, deposits, withdrawals, or real-money paths.
- Do **not** restore high-frequency model/agent cron loops.
- The hot trading path must remain mechanical and must never call an LLM per tick.
- Models are slow strategy reviewers/researchers, not direct trade executors.
- Do **not** print, create, commit, or expose secrets.
- Use bounded/tail JSONL reads only; do not read huge runtime logs fully.
- Do **not** push or deploy unless Queen Glasser explicitly asks after this task.
- Keep public dashboard safe: no raw private ledgers, private paths, host/IP details, account-like balances, tokens, env values, keys, or live trading controls.

## Product target

Aurum v1 should become a multi-model paper quant-bot laboratory:

- each model/strategy owns an independent quant lane/account;
- the hot path mechanically executes validated strategies against recorder data and risk gates;
- models run on a slow configurable cadence, around 30 minutes by default, to review their own lane and decide `KEEP_CURRENT_STRATEGY` / `PROPOSE_UPDATE` / `REQUEST_HOLD_ONLY`;
- strategy updates must pass schema validation, replay/baseline gate, fee/drawdown/churn checks, and operator approval/promotion rules before becoming executable;
- black-swan events must trigger deterministic protective action first, then freeze the lane and ask the model for resume/update/retire planning;
- the public dashboard is Chinese-first and trustworthy;
- a private operator console/control surface exists for lane management and proposal approval.

## 1. Fix the flat BTC chart problem

Investigate and fix why the public Aurum BTC market curve/chart appears flat.

Do not assume the market is actually flat until verifying:

- recorder data;
- chart data generation;
- sampled price/probability field;
- time window;
- y-axis scaling;
- frontend rendering.

The chart must clearly state what it plots:

- market probability/price;
- agent equity;
- ROI;
- score;
- or position value.

If the current chart mixes score history and market price history, separate them.

Add tests so:

- a non-constant synthetic BTC price/probability series produces a non-flat serialized chart series;
- a genuinely flat series is labeled as such.

## 2. Make the public dashboard Chinese-first

Translate primary public UI copy into concise Chinese:

- section titles;
- captions;
- status explanations;
- recorder/runtime/agent/risk/replay/backup/manifest/review labels;
- empty/error states.

Keep stable machine markers where useful:

- `runtime-complete`;
- `TRADE_ALLOWED`;
- `paper_apply`;
- `hold-only`;
- manifest hashes.

The surrounding UI should be readable in Chinese.

Add tests/snapshot assertions for:

- key Chinese labels;
- no obvious sensitive strings in generated HTML.

## 3. Implement/scaffold per-model independent quant lanes/accounts

Each lane should have its own:

- account state;
- strategy/script version;
- trades;
- positions;
- risk events;
- review records;
- public aggregate status.

Support current SuperWing and DeepSeek behavior without breaking existing state migration.

Add a lane registry/data model that can later support:

- SuperWing;
- DeepSeek;
- GPT;
- Claude;
- manual strategies.

Keep other agents’ raw ledgers out of a lane’s hot path. Review context may include peer aggregate scoreboard only. Each lane’s own trades/risk events must be available to its own review context.

## 4. Implement configurable slow model review protocol

Replace vague 5h strategy-review semantics with a configurable slow-review protocol suitable for per-model quant lanes.

Default target cadence should be around 30 minutes, but allow:

- operator-configured cadence;
- quota-safe skipping.

The model/review layer must return exactly one of:

- `KEEP_CURRENT_STRATEGY`;
- `PROPOSE_UPDATE`;
- `REQUEST_HOLD_ONLY`.

Rules:

- `KEEP_CURRENT_STRATEGY` is valid and must not be treated as failure.
- `KEEP_CURRENT_STRATEGY` must not rewrite the executable strategy.
- `PROPOSE_UPDATE` must produce a schema-validated strategy proposal, not direct orders.
- `REQUEST_HOLD_ONLY` freezes new entries for that lane.
- Non-JSON or invalid model output must be classified as `fallback_no_promote` and leave the executable strategy unchanged.
- No review result may directly bypass replay/baseline/promotion gates.

Update docs, timer/service defaults, env examples, or operator-facing copy carefully. Do not deploy.

## 5. Add traditional quant baselines and promotion gate

Implement or scaffold baseline lanes/replays for:

- no-trade;
- buy-and-hold;
- simple momentum;
- simple mean-reversion;
- random-safe.

All baselines must use the same recorder data and fee model.

A model strategy proposal promotion gate must compare:

- after-fee ROI;
- drawdown;
- trade count/churn;
- exposure concentration;
- at least one holdout/replay window;
- baseline performance.

A proposal that fails the gate must remain draft/rejected and must not become executable.

Add dashboard/report output that explains whether a strategy beat baselines without exposing raw private ledgers.

## 6. Add black-swan / extreme-event protection flow

Implement or scaffold extreme-event handling for paper quant lanes.

Detection should include:

### Market anomalies

- price jump;
- spread/depth collapse;
- recorder stale;
- manifest discontinuity;
- API disagreement;
- market closed/resolved/paused.

### Account anomalies

- drawdown breach;
- fee/equity spike;
- repeated stop-loss/rejects;
- concentrated exposure.

### System anomalies

- service restarts;
- replay/backup/status inconsistency.

When triggered:

1. deterministic preapproved risk rules execute the final protective action first;
2. protective action may reduce/close/hold-only/cancel simulated orders according to strategy;
3. freeze the lane;
4. snapshot market + ledger + reason evidence with redaction;
5. trigger model/review decision for resume/update/retire;
6. resume requires cooldown + gate validation.

The model must not panic-trade inside the hot path.

## 7. Build private operator console v1

Build a minimal private operator console/control surface separate from the public dashboard. It may be a local/private static HTML plus CLI/file-control workflow if a server is too much for this slice.

It must support:

- list lanes and statuses;
- view each lane’s current strategy version;
- view last review outcome;
- view ROI/fees/drawdown/trade count/risk rejects;
- pause/resume/hold-only a lane via explicit operator action;
- view pending strategy proposals;
- approve/reject a proposal only after schema/replay/baseline gate results exist;
- request/view a per-lane explanation of recent trades using only that lane’s own ledger plus public aggregate scoreboard.

Private console artifacts must not be published by the public dashboard generator. Public dashboard must remain read-only and safe.

## 8. Update docs and final release note

Update relevant docs to explain:

- Aurum v1 architecture;
- per-model lanes;
- hot-path no-LLM invariant;
- 30-minute slow review protocol;
- `KEEP` / `PROPOSE` / `HOLD` outcomes;
- promotion gate;
- baselines;
- black-swan flow;
- private operator console;
- public/private surface separation;
- deployment/runtime proof boundaries.

Produce a concise markdown release note under `docs/` summarizing:

- what is code-complete;
- what still requires deployment/runtime proof;
- rollback boundaries;
- how to run tests and generate dashboard/console artifacts.

## Verification requirements before completion

Before final response, verify:

- git diff reviewed;
- relevant tests pass;
- `python3 -m pytest -q` passes, or if full suite is impossible, clearly state exact failing tests and why;
- `python3 -m py_compile scripts/*.py` passes;
- changed-line/static secret scan passes;
- generated public dashboard no-secret scan passes;
- bounded JSONL reader behavior is preserved;
- lane isolation tests pass;
- hot-path no-LLM invariant is tested;
- `KEEP_CURRENT_STRATEGY` does not rewrite strategy;
- invalid model output becomes `fallback_no_promote`;
- promotion gate passing/failing tests pass;
- black-swan protective action happens before model trigger;
- lane freeze/resume gate behavior is tested;
- private console artifacts are not included in public dashboard output;
- Chinese public dashboard labels are present;
- BTC chart root cause is documented and fixed.

## Completion rules

- Create local commit(s) only after verification.
- Do not push.
- Do not deploy.
- Final response must include:
  - commit SHA(s);
  - files changed summary;
  - tests/verification commands and results;
  - exact BTC flat-chart root cause;
  - any remaining blockers or runtime-only proof still needed.
