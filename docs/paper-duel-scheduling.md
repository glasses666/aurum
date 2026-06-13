# Paper Duel Scheduling

Aurum's corrected hot path is a **resident mechanical bot loop** on the VPS, not an hourly LLM suggestion timer.

## Current target stage

- Mode: `paper_apply` for pre-contest paper stability trading.
- Execution host: Aurum VPS.
- Runtime user: `aurum`.
- App path: `/opt/aurum/app`.
- Env path: `/opt/aurum/.env` (`aurum:aurum`, mode `600`).
- Data path: `/opt/aurum/data/paper_duel`.
- Public dashboard path: `/opt/aurum/public/dashboard`.
- Lock path: `/opt/aurum/run/duel.lock`.
- Resident unit: `aurum-bot-loop.service`.
- Legacy one-shot tick unit: `aurum-paper-duel-tick.service` for manual fallback/smoke only.

## Corrected architecture

```text
agent/review layer
  -> writes versioned mechanical bot scripts
     /opt/aurum/data/paper_duel/bot_scripts/current/{superwing,deepseek}.json
  -> resident bot loop reads those scripts
  -> one shared Bitcoin market snapshot per loop
  -> each bot mechanically emits buy/sell/hold orders
  -> runner validates risk, fills paper orders, fees, ledger, dashboard
```

The bot layer does **not** ask an LLM every tick. DeepSeek/SuperWing rules may be generated or reviewed by agents, but the hot loop executes JSON scripts mechanically.

## Timing

The resident loop clamps timing with:

```text
AURUM_BOT_MIN_INTERVAL_SEC=5
AURUM_BOT_DEFAULT_INTERVAL_SEC=15
```

Default behavior:

- hard minimum interval: `5s`;
- default script interval: `15s`;
- the loop sleeps after each tick so it stays resident instead of spawning a new process every few seconds.

The old hourly timer remains only as a fallback/manual smoke path unless explicitly re-enabled:

```text
aurum-paper-duel-tick.timer
```

## Fairness rule

Do not schedule separate jobs for SuperWing and DeepSeek. One resident-loop tick must:

1. acquire the duel lock;
2. fetch one Polymarket Bitcoin market snapshot;
3. feed the same snapshot to both mechanical bot scripts;
4. run local validation/risk checks for both;
5. write one tick record and score snapshot;
6. regenerate the static public dashboard.

## Entrypoints

Resident loop wrapper:

```bash
/opt/aurum/app/scripts/run_bot_loop.sh
```

One local smoke tick without starting a daemon:

```bash
cd /opt/aurum/app
runuser -u aurum -- python3 scripts/agent_bot_loop.py \
  --env-file /opt/aurum/.env \
  --data-dir /opt/aurum/data/paper_duel \
  --mode paper_apply \
  --limit 12 \
  --once
```

Systemd:

```bash
systemctl enable --now aurum-bot-loop.service
systemctl status aurum-bot-loop.service
journalctl -u aurum-bot-loop.service -n 80 --no-pager
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

## Mechanical script semantics

Each bot script contains:

- `interval_sec` / `min_interval_sec`;
- `allowed_sides: ["buy", "sell"]`;
- `buy_when`: price band, volume floor, notional, limit buffer;
- `sell_when`: take-profit, stop-loss, max-hold, exit fraction, limit buffer;
- `hold_when`: reserve/risk safety constraints.

The paper runner still enforces global caps regardless of script content.
