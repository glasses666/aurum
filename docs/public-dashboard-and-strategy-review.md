# Public Dashboard and 5h Strategy Review

Aurum now exposes a static public transparency dashboard for the paper duel.

## Purpose

The dashboard removes the black-box feeling from the SuperWing vs DeepSeek paper duel. It shows:

- LLM / agent names;
- starting paper funds;
- current paper funds and ROI score;
- recent trading dynamics: orders, fills, rejections, notes;
- the current trading flow and safety gates;
- the current SuperWing rules and DeepSeek strategy prompt;
- a score-history chart;
- the latest 5h advanced review and rule updates.

## Safety boundary

The public site is static HTML generated from local paper-duel files. It must not contain:

- API keys;
- SSH material;
- wallet/private-key values;
- CLOB credentials;
- live order endpoints;
- real-money balances.

The site includes `noindex,nofollow` by default, because it is public for inspection but not meant as marketing or SEO.

## Files

Local repo files:

- `scripts/generate_dashboard.py` — static dashboard generator.
- `scripts/strategy_rules.py` — versioned SuperWing and DeepSeek paper-strategy rule files.
- `scripts/strategy_review.py` — 5h advanced review and rule-prompt updater.
- `scripts/run_strategy_review.sh` — server wrapper with `flock`.
- `deploy/systemd/aurum-strategy-review.service`
- `deploy/systemd/aurum-strategy-review.timer`

Server runtime paths:

- App: `/opt/aurum/app`
- Data: `/opt/aurum/data/paper_duel`
- Static dashboard root: `/opt/aurum/public/dashboard`
- Current rules: `/opt/aurum/data/paper_duel/strategy_rules/current/`
- Rule proposals: `/opt/aurum/data/paper_duel/strategy_rules/proposals/`
- Rule history: `/opt/aurum/data/paper_duel/strategy_rules/history/`
- Review records: `/opt/aurum/data/paper_duel/strategy_reviews/`

## Timers

The paper-duel tick remains hourly:

```text
aurum-paper-duel-tick.timer
```

The advanced strategy review runs every 5 hours:

```text
aurum-strategy-review.timer
```

The tick service also regenerates the static dashboard after each successful tick. The 5h review service regenerates it after each review.

## Rule-update policy

The review model can propose and, if explicitly gated, promote strategy-rule updates.

Safe default in `.env.example`:

```text
AURUM_RULE_AUTO_PROMOTE=false
AURUM_RULE_OPERATOR_CONFIRM=
```

For the paper-only VPS experiment, the operator may enable automatic prompt/rule updates with:

```text
AURUM_RULE_AUTO_PROMOTE=true
AURUM_RULE_OPERATOR_CONFIRM=ALLOW_RULE_AUTO_PROMOTE
```

Even when auto-promote is enabled:

- only strategy prompts/rules are updated;
- live-trading terms are rejected;
- SuperWing numeric rules are clamped to safe ranges;
- DeepSeek rules are validated for paper-only/buy-only/hold-if-no-edge language;
- previous rules are copied to history before promotion;
- every update is recorded in `versions.jsonl` and the public dashboard.

## Advanced model

Default review model:

```text
AURUM_REVIEW_MODEL=deepseek-v4-pro
AURUM_REVIEW_THINKING=disabled
AURUM_REVIEW_REASONING_EFFORT=high
```

If the configured review model is unavailable, the script falls back to the normal decision model and records which model actually produced the review. Review thinking is disabled by default because `deepseek-v4-pro` produced cleaner machine-parseable JSON in this mode; DeepSeek's per-tick decision lane still uses thinking/reasoning.

## Manual commands

Generate dashboard once:

```bash
cd /opt/aurum/app
runuser -u aurum -- python3 scripts/generate_dashboard.py \
  --env-file /opt/aurum/.env \
  --data-dir /opt/aurum/data/paper_duel \
  --output-dir /opt/aurum/public/dashboard
```

Run one strategy review:

```bash
systemctl start aurum-strategy-review.service
journalctl -u aurum-strategy-review.service -n 80 --no-pager
```

Check timers:

```bash
systemctl list-timers --all | grep aurum
```

## Current stage

The trading loop remains paper-only. The website and 5h review improve transparency and iteration speed, but they do not add live wallets or real order placement.
