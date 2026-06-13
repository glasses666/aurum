#!/usr/bin/env python3
"""Unified Aurum paper-duel tick runner.

One tick fetches a single Polymarket market snapshot, gives the same snapshot to
SuperWing and DeepSeek, validates both through the local paper-ledger rules, and
writes one auditable tick record.  It never connects a live wallet or sends live
orders.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional

import agent_duel as duel
import strategy_rules

FORBIDDEN_LIVE_ENV_SUBSTRINGS = (
    "PRIVATE_KEY",
    "WALLET_PRIVATE",
    "POLYMARKET_FUNDER",
    "POLYMARKET_SIGNATURE",
    "POLYMARKET_PROXY_WALLET",
    "CLOB_API_KEY",
    "CLOB_SECRET",
    "CLOB_PASS_PHRASE",
    "LIVE_TRADING",
    "REAL_ORDER",
)

MODES = ("review_only", "paper_apply")


def tick_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "ticks.jsonl"


def snapshots_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "snapshots"


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def forbidden_live_env_present() -> List[str]:
    hits = []
    for key, value in os.environ.items():
        if not value:
            continue
        upper = key.upper()
        if any(token in upper for token in FORBIDDEN_LIVE_ENV_SUBSTRINGS):
            hits.append(key)
    return sorted(hits)


def safe_controls_for_log(controls: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "allow_paper_apply": bool(controls.get("allow_paper_apply")),
        "max_orders": int(controls.get("max_orders", 0)),
        "max_notional_per_order": round(float(controls.get("max_notional_per_order", 0.0)), 4),
        "thinking": controls.get("thinking"),
        "reasoning_effort": controls.get("reasoning_effort"),
    }


def run_tick(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = pathlib.Path(args.data_dir)
    duel.ensure_data_dir(data_dir)
    duel.load_env_file(pathlib.Path(args.env_file) if args.env_file else None)

    blocked = forbidden_live_env_present()
    if blocked:
        raise duel.DuelError("live-trading/wallet-like env vars are present; refusing paper tick: " + ", ".join(blocked))

    mode = args.mode or os.environ.get("AURUM_DUEL_MODE", "review_only")
    mode = mode.strip().lower()
    if mode not in MODES:
        raise duel.DuelError(f"unsupported AURUM_DUEL_MODE={mode!r}; expected one of {MODES}")

    snapshot_id = duel.utc_now().replace(":", "").replace("+00:00", "Z")
    state = duel.init_state(data_dir, reset=False)
    markets = duel.fetch_markets(args.limit, args.min_volume, args.mock_markets, args.allow_proxy)
    if not markets:
        raise duel.DuelError("no eligible markets returned for tick")

    snapshot_record = {
        "snapshot_id": snapshot_id,
        "ts": duel.utc_now(),
        "mode": mode,
        "limit": args.limit,
        "min_volume": args.min_volume,
        "market_count": len(markets),
        "markets": markets,
    }
    snapshot_file = snapshots_dir(data_dir) / f"{snapshot_id}.json"
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(json.dumps(snapshot_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    apply_paper = mode == "paper_apply"

    superwing_decision = duel.superwing_decision(state["accounts"]["superwing"], markets, max_orders=args.max_orders, data_dir=data_dir)
    superwing_result = duel.validate_and_apply(
        data_dir,
        state,
        "superwing",
        superwing_decision,
        markets,
        apply=apply_paper,
        max_orders=args.max_orders,
        max_notional_per_order=duel.STARTING_EQUITY * duel.MAX_TRADE_FRACTION,
    )

    # Reload state after SuperWing paper application so DeepSeek sees the same ledger facts.
    if apply_paper:
        state = duel.load_state(data_dir)

    deepseek_controls = duel.deepseek_controls(args.max_orders)
    if apply_paper:
        duel.require_deepseek_apply_authorized(deepseek_controls)

    try:
        deepseek_decision = duel.deepseek_decision(state["accounts"]["deepseek"], markets, deepseek_controls, data_dir=data_dir)
    except Exception as exc:
        deepseek_decision = {
            "agent_id": "deepseek",
            "orders": [],
            "notes": f"DeepSeek decision error; held safely: {exc}",
            "error": str(exc),
        }
    deepseek_result = duel.validate_and_apply(
        data_dir,
        state,
        "deepseek",
        deepseek_decision,
        markets,
        apply=apply_paper,
        max_orders=int(deepseek_controls["max_orders"]),
        max_notional_per_order=float(deepseek_controls["max_notional_per_order"]),
    )

    final_state = duel.load_state(data_dir)
    prices = duel.market_price_map(markets)
    scores = [duel.portfolio_value(final_state["accounts"][agent], prices) for agent in duel.AGENTS]
    scores.sort(key=lambda row: row["score"], reverse=True)

    tick = {
        "ok": True,
        "tick_id": snapshot_id,
        "ts": duel.utc_now(),
        "mode": mode,
        "applied": apply_paper,
        "snapshot_file": str(snapshot_file),
        "market_count": len(markets),
        "shared_snapshot": True,
        "agents": {
            "superwing": {
                "decision": superwing_decision,
                "result": superwing_result,
            },
            "deepseek": {
                "decision": deepseek_decision,
                "result": deepseek_result,
                "controls": safe_controls_for_log(deepseek_controls),
            },
        },
        "scores": scores,
        "strategy_rules": strategy_rules.summarize_rules(data_dir),
    }
    append_jsonl(tick_path(data_dir), tick)
    return tick


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one unified Aurum paper-duel tick")
    parser.add_argument("--data-dir", default="data/paper_duel")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--mode", choices=MODES, default=None, help="override AURUM_DUEL_MODE; default review_only")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--min-volume", type=float, default=0.0)
    parser.add_argument("--max-orders", type=int, default=2)
    parser.add_argument("--mock-markets", action="store_true")
    parser.add_argument("--allow-proxy", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        tick = run_tick(args)
        print(json.dumps(tick, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
