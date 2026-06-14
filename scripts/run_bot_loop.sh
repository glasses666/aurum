#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aurum/app}"
DATA_DIR="${AURUM_DATA_DIR:-/opt/aurum/data/paper_duel}"
ENV_FILE="${AURUM_ENV_FILE:-/opt/aurum/.env}"
RUN_DIR="${AURUM_RUN_DIR:-/opt/aurum/run}"
LOG_DIR="${AURUM_LOG_DIR:-/opt/aurum/logs}"
MODE="${AURUM_DUEL_MODE:-review_only}"
LIMIT="${AURUM_DUEL_LIMIT:-12}"
MIN_VOLUME="${AURUM_DUEL_MIN_VOLUME:-0}"
MAX_ORDERS="${AURUM_DUEL_MAX_ORDERS:-2}"

DASHBOARD_DIR="${AURUM_PUBLIC_DASHBOARD_DIR:-/opt/aurum/public/dashboard}"

mkdir -p "$RUN_DIR" "$LOG_DIR" "$DATA_DIR" "$DASHBOARD_DIR"
cd "$APP_DIR"

exec flock -n "$RUN_DIR/duel.lock" \
  python3 scripts/agent_bot_loop.py \
    --env-file "$ENV_FILE" \
    --data-dir "$DATA_DIR" \
    --mode "$MODE" \
    --limit "$LIMIT" \
    --min-volume "$MIN_VOLUME" \
    --max-orders "$MAX_ORDERS" \
    --dashboard-dir "$DASHBOARD_DIR"
