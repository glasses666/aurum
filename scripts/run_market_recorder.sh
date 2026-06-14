#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aurum/app}"
DATA_DIR="${AURUM_RECORDER_DATA_DIR:-/opt/aurum/data}"
RUN_DIR="${AURUM_RUN_DIR:-/opt/aurum/run}"
LOG_DIR="${AURUM_LOG_DIR:-/opt/aurum/logs}"
INTERVAL="${AURUM_RECORDER_INTERVAL_SEC:-30}"
MAX_BOOKS="${AURUM_RECORDER_MAX_BOOKS:-20}"
TIMEOUT="${AURUM_RECORDER_TIMEOUT:-12}"
UNIVERSE="${AURUM_DUEL_UNIVERSE:-bitcoin}"

mkdir -p "$RUN_DIR" "$LOG_DIR" "$DATA_DIR"
cd "$APP_DIR"

exec flock -n "$RUN_DIR/market-recorder.lock" \
  python3 scripts/market_recorder.py \
    --data-dir "$DATA_DIR" \
    --interval-sec "$INTERVAL" \
    --max-books "$MAX_BOOKS" \
    --timeout "$TIMEOUT" \
    --universe "$UNIVERSE"
