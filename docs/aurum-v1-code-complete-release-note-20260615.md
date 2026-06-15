# Aurum v1 Code-Complete Release Note - 2026-06-15

## Verdict

Code-complete locally for the v1 repair scope; runtime proof is still required
before calling the VPS deployment runtime-complete.

## What Changed

- Public dashboard is Chinese-first and labels the plotted BTC metric as BTC Yes
  probability, separate from agent ROI.
- Dashboard manifest now records BTC chart flat/variable status so a genuinely
  flat source series is not confused with a rendering bug.
- BTC chart selector root cause fixed: the old public chart selected the highest-volume BTC-tagged market, which on the live dashboard was the long-horizon low-probability `Will bitcoin hit $1m before GTA VI?` market. That market can be legitimately near-flat even while BTC is moving. The selector now scores persistent BTC candidates by non-extreme latest Yes price, observed variation, sample count, then liquidity/volume, and records the selector diagnostic in the safe public manifest.
- Quant-lane governance scaffold covers SuperWing, DeepSeek, GPT, Claude, and
  manual lanes with paper-only public/private separation.
- Slow review protocol now requires `KEEP_CURRENT_STRATEGY`,
  `PROPOSE_UPDATE`, or `REQUEST_HOLD_ONLY`.
- `PROPOSE_UPDATE` writes proposals only; promotion additionally requires local
  replay/holdout/baseline gate pass.
- Baselines cover no-trade, buy-and-hold, simple momentum, simple
  mean-reversion, and random-safe using the paper fee model.
- Black-swan flow executes deterministic paper protective close/reduce orders first, records redacted evidence/account delta, then freezes the lane for model resume/update/retire review.
- Private operator output remains separate from public dashboard output and can
  be controlled with `scripts/quant_lanes.py`.
- Operator proposal workflow lists proposal artifacts and requires a passed schema/replay/holdout/baseline promotion gate before `proposal-decision approve`; rejected/ungated proposals stay non-executable.
- Slow review cadence is quota-safe: no-change cycles and high quota usage can skip model calls while preserving `KEEP_CURRENT_STRATEGY` semantics and generating a skip record/dashboard refresh.

## Rollback Boundaries

- Dashboard-only rollback: revert `scripts/generate_dashboard.py`.
- Lane/review rollback: revert `scripts/quant_lanes.py`,
  `scripts/strategy_review.py`, and the small `agent_bot_loop.py` lane-control
  hook.
- Ops cadence rollback: revert `.env.example` and
  `deploy/systemd/aurum-strategy-review.*`.

## Runtime Proof Still Needed

No deploy or remote mutation was performed in this code-complete pass. Runtime
completion still requires the normal VPS backup, timestamped release, service
restart, service/timer verification, recorder health, gate/tick/dashboard
checks, watchdog silence, and backup `ok=true` evidence.

## Local Commands

```bash
/Users/dracoglasser/venvs/omlx-jang/bin/python -m pytest -q
/Users/dracoglasser/venvs/omlx-jang/bin/python -m py_compile scripts/*.py ~/.hermes/scripts/aurum_paper_duel_watch.py ~/.hermes/scripts/aurum_stability_backup.py
git diff --check
/Users/dracoglasser/venvs/omlx-jang/bin/python scripts/changed_line_secret_scan.py --repo /Users/dracoglasser/workspace/aurum
```
