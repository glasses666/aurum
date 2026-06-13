# Paper Duel Scheduling

Aurum's DeepSeek/SuperWing duel is scheduled on the VPS with systemd timers, not Hermes cron.

## Current stage

- Mode: `review_only`
- Frequency: hourly, at minute `:07` with up to 90s randomized delay
- Execution host: Aurum VPS
- Runtime user: `aurum`
- App path: `/opt/aurum/app`
- Env path: `/opt/aurum/.env` (`aurum:aurum`, mode `600`)
- Data path: `/opt/aurum/data/paper_duel`
- Lock path: `/opt/aurum/run/duel.lock`
- Unit: `aurum-paper-duel-tick.service`
- Timer: `aurum-paper-duel-tick.timer`

## Why systemd instead of Hermes cron

The trading loop should not depend on Telegram, the local Mac, Hermes gateway uptime, or main-model quota. Hermes cron can audit/report later, but the VPS owns the deterministic paper-duel tick.

## Fairness rule

Do not schedule separate jobs for SuperWing and DeepSeek. One tick must:

1. acquire the duel lock;
2. fetch one Polymarket market snapshot;
3. feed the same snapshot to SuperWing and DeepSeek;
4. run local validation/risk checks for both;
5. write one tick record and one score snapshot.

The shared tick entrypoint is:

```bash
python3 scripts/agent_duel_tick.py \
  --env-file /opt/aurum/.env \
  --data-dir /opt/aurum/data/paper_duel \
  --mode review_only \
  --limit 12
```

The systemd service calls the wrapper:

```bash
/opt/aurum/app/scripts/run_paper_duel_tick.sh
```

## Safety gates

- `review_only` never mutates virtual wallet state.
- `paper_apply` writes only local virtual wallet state.
- Any live wallet / private-key / CLOB credential-like environment variable causes the tick to fail closed.
- DeepSeek cannot apply paper fills unless both local env gates are set:

```text
AURUM_DEEPSEEK_ALLOW_PAPER_APPLY=true
AURUM_DEEPSEEK_OPERATOR_CONFIRM=ALLOW_DEEPSEEK_PAPER_APPLY
```

Current env keeps:

```text
AURUM_DUEL_MODE=review_only
AURUM_DEEPSEEK_ALLOW_PAPER_APPLY=false
```

## Operational commands

Check timer:

```bash
systemctl status aurum-paper-duel-tick.timer
systemctl list-timers --all | grep aurum
```

Run one immediate tick:

```bash
systemctl start aurum-paper-duel-tick.service
```

Read logs:

```bash
journalctl -u aurum-paper-duel-tick.service -n 80 --no-pager
```

Inspect latest data:

```bash
ls -lt /opt/aurum/data/paper_duel/snapshots | head
 tail -n 1 /opt/aurum/data/paper_duel/ticks.jsonl
```

## Stage promotion

Recommended path:

1. Run review-only hourly for at least 24h.
2. Inspect DS decisions, rejections, JSON stability, and cost.
3. Only then consider `paper_apply` for a finite 7-day paper contest.
4. Keep live trading out of scope unless a separate authorized design review approves it.
