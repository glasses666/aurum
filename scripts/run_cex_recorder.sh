#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aurum/app}"
DATA_DIR="${AURUM_CEX_DATA_DIR:-/opt/aurum/data/cex_arena}"
RUN_DIR="${AURUM_RUN_DIR:-/opt/aurum/run}"
LOG_DIR="${AURUM_LOG_DIR:-/opt/aurum/logs}"
MARKETS="${AURUM_CEX_MARKETS:-okx:BTC-USDT-SWAP,okx:ETH-USDT-SWAP}"
INTERVAL="${AURUM_CEX_RECORDER_INTERVAL_SEC:-30}"
TIMEOUT="${AURUM_CEX_TIMEOUT:-12}"

export AURUM_CEX_ENABLE_LIVE_TRADING="${AURUM_CEX_ENABLE_LIVE_TRADING:-false}"

mkdir -p "$RUN_DIR" "$LOG_DIR" "$DATA_DIR"
cd "$APP_DIR"

exec flock -n "$RUN_DIR/cex-recorder.lock" \
  python3 scripts/cex_arena.py recorder-loop \
    --data-dir "$DATA_DIR" \
    --markets "$MARKETS" \
    --interval-sec "$INTERVAL" \
    --timeout "$TIMEOUT"
