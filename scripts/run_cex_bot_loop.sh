#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aurum/app}"
CEX_DATA_DIR="${AURUM_CEX_DATA_DIR:-/opt/aurum/data/cex_arena}"
PAPER_DATA_DIR="${AURUM_DATA_DIR:-/opt/aurum/data/paper_duel}"
ENV_FILE="${AURUM_ENV_FILE:-/opt/aurum/.env}"
RUN_DIR="${AURUM_RUN_DIR:-/opt/aurum/run}"
LOG_DIR="${AURUM_LOG_DIR:-/opt/aurum/logs}"
DASHBOARD_DIR="${AURUM_PUBLIC_DASHBOARD_DIR:-/opt/aurum/public/dashboard}"
STRATEGIES="${AURUM_CEX_STRATEGIES:-momentum,mean_reversion,volatility_breakout,funding_filter,grid_shadow}"
NOTIONAL="${AURUM_CEX_NOTIONAL_USDT:-50}"
MAX_ORDERS="${AURUM_CEX_MAX_ORDERS:-1}"
MAX_STALE="${AURUM_CEX_MAX_STALE_SECONDS:-420}"
INTERVAL="${AURUM_CEX_BOT_INTERVAL_SEC:-30}"

export AURUM_CEX_ENABLE_LIVE_TRADING="${AURUM_CEX_ENABLE_LIVE_TRADING:-false}"
export AURUM_CEX_DATA_DIR="$CEX_DATA_DIR"
export CEX_DATA_DIR PAPER_DATA_DIR ENV_FILE DASHBOARD_DIR STRATEGIES NOTIONAL MAX_ORDERS MAX_STALE INTERVAL

mkdir -p "$RUN_DIR" "$LOG_DIR" "$CEX_DATA_DIR" "$PAPER_DATA_DIR" "$DASHBOARD_DIR"
cd "$APP_DIR"

exec flock -n "$RUN_DIR/cex-bot-loop.lock" bash -c '
  while true; do
    python3 scripts/cex_arena.py tick \
      --data-dir "$CEX_DATA_DIR" \
      --strategies "$STRATEGIES" \
      --notional "$NOTIONAL" \
      --max-orders "$MAX_ORDERS" \
      --max-stale-seconds "$MAX_STALE" || true
    python3 scripts/generate_dashboard.py \
      --env-file "$ENV_FILE" \
      --data-dir "$PAPER_DATA_DIR" \
      --output-dir "$DASHBOARD_DIR" || true
    sleep "$INTERVAL"
  done
'
