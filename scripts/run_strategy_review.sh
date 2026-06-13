#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aurum/app}"
DATA_DIR="${AURUM_DATA_DIR:-/opt/aurum/data/paper_duel}"
ENV_FILE="${AURUM_ENV_FILE:-/opt/aurum/.env}"
RUN_DIR="${AURUM_RUN_DIR:-/opt/aurum/run}"
LOG_DIR="${AURUM_LOG_DIR:-/opt/aurum/logs}"
DASHBOARD_DIR="${AURUM_PUBLIC_DASHBOARD_DIR:-/opt/aurum/public/dashboard}"
LIMIT_TICKS="${AURUM_REVIEW_LIMIT_TICKS:-24}"

mkdir -p "$RUN_DIR" "$LOG_DIR" "$DATA_DIR" "$DASHBOARD_DIR"
cd "$APP_DIR"

flock -n "$RUN_DIR/strategy-review.lock" \
  python3 scripts/strategy_review.py \
    --env-file "$ENV_FILE" \
    --data-dir "$DATA_DIR" \
    --dashboard-dir "$DASHBOARD_DIR" \
    --limit-ticks "$LIMIT_TICKS"
