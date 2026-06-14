# Aurum GPT Pro code-review entrypoints — 2026-06-14

Branch: `paper-account-sim-spec`

Use this file as the concrete code-review index. The status brief explains runtime state; the links below are the code paths GPT Pro should inspect for actual bugs.

## Review order
1. [`docs/current-status-for-gpt-pro-review-20260614.md`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/docs/current-status-for-gpt-pro-review-20260614.md)
2. [`docs/polymarket-market-recorder.md`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/docs/polymarket-market-recorder.md)
3. [`scripts/market_recorder.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/scripts/market_recorder.py)
4. [`tests/test_market_recorder.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/tests/test_market_recorder.py)
5. [`scripts/agent_bot_loop.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/scripts/agent_bot_loop.py)
6. [`tests/test_mechanical_bot_scripts.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/tests/test_mechanical_bot_scripts.py)
7. [`scripts/agent_duel.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/scripts/agent_duel.py)
8. [`scripts/bot_scripts.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/scripts/bot_scripts.py)
9. [`scripts/strategy_review.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/scripts/strategy_review.py)
10. [`deploy/systemd/aurum-market-recorder.service`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/deploy/systemd/aurum-market-recorder.service)
11. [`deploy/systemd/aurum-bot-loop.service`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/deploy/systemd/aurum-bot-loop.service)
12. [`scripts/generate_dashboard.py`](https://github.com/glasses666/aurum/blob/paper-account-sim-spec/scripts/generate_dashboard.py)

## Ask GPT Pro to distinguish

- BLOCKER before official competition
- SHOULD FIX before 7-day paper contest
- NICE TO HAVE later
- false positive / acceptable for current paper-only v0

## Concrete review focus

1. Does `market_recorder.py` produce enough raw data to replay market state, or is it only a latest-frame cache with raw snapshots?
2. Should paper fills in `agent_duel.py` be based on captured CLOB books rather than Gamma outcome prices?
3. Can `agent_bot_loop.py` safely fall back without hiding recorder failures?
4. Are `bot_scripts.py` mechanical rules too simple or too conservative after the fee-churn fix?
5. Is `strategy_review.py` promotion governance strict enough for LLM-written scripts?
6. Are systemd hardening and write paths sufficient for paper-only services?
7. Is there any path from env/config/service layout to live-order or secret exposure?
8. What is missing for agent context design: history window, feature schema, isolation, script promotion, rollback, shadow mode?

## Pasteable prompt

```text
Review this branch as a codebase, not just as a status memo:

https://github.com/glasses666/aurum/blob/paper-account-sim-spec/docs/current-status-for-gpt-pro-review-20260614.md

Then inspect the review-order files listed in:
https://github.com/glasses666/aurum/blob/paper-account-sim-spec/docs/gpt-pro-code-review-entrypoints-20260614.md

Return concrete findings only. Use this severity format:

BLOCKERS before official competition:
SHOULD FIX before 7-day paper contest:
NICE TO HAVE later:
FALSE POSITIVES / acceptable for v0:

Focus especially on market-recorder replay/audit sufficiency, paper fill realism, agent/context design, LLM script promotion safety, and ops/backup/restore hardening.
```
