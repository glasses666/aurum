#!/usr/bin/env python3
"""Resident mechanical bot loop for Aurum paper trading.

Unlike the legacy hourly LLM tick, this process stays alive and executes the
versioned bot scripts mechanically on a short interval. Agents write/review the
scripts; bots execute them.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import time
from typing import Any, Dict, List, Optional

import agent_duel as duel
import bot_scripts
import generate_dashboard
import data_quality_gate
import market_recorder
import strategy_rules

_STOP = False

def _handle_stop(signum: int, frame: Any) -> None:  # pragma: no cover - signal wiring
    global _STOP
    _STOP = True


def tick_path(data_dir: pathlib.Path) -> pathlib.Path:
    # Keep the public dashboard/history path unchanged; resident-loop ticks carry
    # runner="resident_mechanical_bot_loop" inside the record.
    return data_dir / "ticks.jsonl"


def snapshots_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "snapshots"


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def effective_interval(script: Dict[str, Any]) -> int:
    interval = int(script.get("interval_sec") or int(os.environ.get("AURUM_BOT_DEFAULT_INTERVAL_SEC", "15")))
    minimum = int(script.get("min_interval_sec") or int(os.environ.get("AURUM_BOT_MIN_INTERVAL_SEC", "5")))
    return max(minimum, interval)


def loop_interval(data_dir: pathlib.Path) -> int:
    scripts = [bot_scripts.load_bot_script(data_dir, agent, write_manifest=False) for agent in duel.AGENTS]
    return min(effective_interval(script) for script in scripts)


def forbidden_live_env_present() -> List[str]:
    blocked = []
    for key, value in os.environ.items():
        if not value:
            continue
        upper = key.upper()
        if any(token in upper for token in duel_tick_forbidden_tokens()):
            blocked.append(key)
    return sorted(blocked)


def duel_tick_forbidden_tokens() -> tuple[str, ...]:
    return (
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


def recorder_data_root(data_dir: pathlib.Path) -> pathlib.Path:
    configured = os.environ.get("AURUM_RECORDER_DATA_DIR", "").strip()
    if configured:
        return pathlib.Path(configured)
    if data_dir.name == "paper_duel":
        return data_dir.parent
    return data_dir


def filter_recorded_markets_for_tick(markets: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    min_volume = float(args.min_volume or 0.0)
    limit = int(args.limit or 0)
    filtered = [market for market in markets if float(market.get("volume") or 0.0) >= min_volume]
    if limit > 0:
        filtered = filtered[:limit]
    return filtered


def load_markets_for_tick(data_dir: pathlib.Path, args: argparse.Namespace) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    max_stale = int(os.environ.get("AURUM_RECORDER_MAX_STALE_SECONDS", "180"))
    root = recorder_data_root(data_dir)
    allow_fallback = duel.env_bool("AURUM_ALLOW_UNAUDITED_FALLBACK", False)
    gate = data_quality_gate.evaluate_data_quality_gate(
        root,
        max_stale_seconds=max_stale,
        allow_unaudited_fallback=allow_fallback,
    )
    if gate["decision"] != data_quality_gate.TRADE_ALLOWED:
        if gate.get("fallback_allowed"):
            markets = duel.fetch_markets(args.limit, args.min_volume, args.mock_markets, args.allow_proxy)
            return markets, {
                "source": "unaudited_direct_fetch_fallback",
                "reason": "explicit_AURUM_ALLOW_UNAUDITED_FALLBACK",
                "data_quality_gate": gate,
                "market_count": len(markets),
            }
        source = dict(gate)
        source["source"] = "data_quality_gate"
        return [], source

    recorded = market_recorder.load_latest_markets(root, max_stale_seconds=max_stale)
    markets = filter_recorded_markets_for_tick(recorded["markets"], args)
    if not markets:
        return [], {
            "source": "data_quality_gate",
            "decision": data_quality_gate.HOLD_ONLY,
            "trade_allowed": False,
            "hold_only": True,
            "stop_service": False,
            "reason_codes": ["recorder latest_markets empty after tick filters"],
            "market_source": recorded.get("source", "market_recorder"),
            "ts": recorded.get("ts"),
            "max_stale_seconds": max_stale,
            "health_ok": True,
            "market_count": 0,
        }
    return markets, {
        "source": recorded.get("source", "market_recorder"),
        "ts": recorded.get("ts"),
        "max_stale_seconds": max_stale,
        "health_ok": True,
        "data_quality_gate": gate,
    }


def hold_only_decision_for_agent(agent_id: str, script: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    reasons = [str(reason) for reason in (gate.get("reason_codes", []) or [])]
    if script.get("risk_reason"):
        reasons.append(str(script.get("risk_reason")))
    return {
        "agent_id": agent_id,
        "source": "data_quality_gate_hold",
        "script_version": script.get("version"),
        "script_updated_at": script.get("updated_at"),
        "orders": [],
        "notes": "hold-only; market data quality gate blocked trading: " + ",".join(reasons),
    }


def run_mechanical_tick(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = pathlib.Path(args.data_dir)
    duel.load_env_file(pathlib.Path(args.env_file) if args.env_file else None)

    blocked = forbidden_live_env_present()
    if blocked:
        raise duel.DuelError("live-trading/wallet-like env vars are present; refusing mechanical bot tick: " + ", ".join(blocked))

    raw_mode = args.mode if args.mode is not None else os.environ.get("AURUM_DUEL_MODE")
    mode = (str(raw_mode or "review_only").strip().lower() or "review_only")
    if mode not in {"review_only", "paper_apply"}:
        raise duel.DuelError(f"unsupported mode={mode!r}")
    apply_paper = mode == "paper_apply"

    snapshot_id = duel.utc_now().replace(":", "").replace("+00:00", "Z")
    markets, market_source = load_markets_for_tick(data_dir, args)
    gate = market_source if market_source.get("source") == "data_quality_gate" else market_source.get("data_quality_gate")
    if gate and gate.get("decision") == data_quality_gate.STOP_SERVICE:
        raise duel.DuelError("data quality gate STOP_SERVICE: " + ",".join(str(reason) for reason in gate.get("reason_codes", [])))
    hold_only = bool(gate and gate.get("decision") == data_quality_gate.HOLD_ONLY)
    if not markets and not hold_only:
        raise duel.DuelError("no eligible markets returned for mechanical bot tick")
    effective_apply = apply_paper and not hold_only
    if effective_apply:
        controls = duel.deepseek_controls(args.max_orders)
        duel.require_deepseek_apply_authorized(controls)

    duel.ensure_data_dir(data_dir)
    bot_scripts.ensure_default_bot_scripts(data_dir, write_manifest=False)
    bot_registry = bot_scripts.verify_bot_registry_manifest(data_dir)
    if not bot_registry.get("ok"):
        registry_reasons = ["bot_script_manifest:" + str(reason) for reason in bot_registry.get("errors", [])]
        if apply_paper:
            hold_only = True
            gate = dict(gate or {})
            gate.update(
                {
                    "decision": data_quality_gate.HOLD_ONLY,
                    "trade_allowed": False,
                    "hold_only": True,
                    "stop_service": False,
                    "reason_codes": list(gate.get("reason_codes", []) or []) + registry_reasons,
                }
            )
            effective_apply = apply_paper and not hold_only
        market_source = {**market_source, "data_quality_gate": gate, "bot_script_manifest": bot_registry}
    state = duel.init_state(data_dir, reset=False)

    snapshot_record = {
        "snapshot_id": snapshot_id,
        "ts": duel.utc_now(),
        "mode": mode,
        "effective_mode": "hold_only" if hold_only else mode,
        "runner": "resident_mechanical_bot_loop",
        "limit": args.limit,
        "min_volume": args.min_volume,
        "market_count": len(markets),
        "market_source": market_source,
        "data_quality_gate": gate,
        "bot_script_manifest": bot_registry,
        "markets": markets,
    }
    snapshot_file = snapshots_dir(data_dir) / f"{snapshot_id}.json"
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(json.dumps(snapshot_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    agent_records: Dict[str, Any] = {}
    for agent_id in duel.AGENTS:
        state = duel.load_state(data_dir)
        script = bot_scripts.load_bot_script(data_dir, agent_id, write_manifest=False)
        if hold_only:
            decision = hold_only_decision_for_agent(agent_id, script, gate or {})
        else:
            decision = bot_scripts.mechanical_decision_for_agent(state["accounts"][agent_id], markets, script)
        result = duel.validate_and_apply(
            data_dir,
            state,
            agent_id,
            decision,
            markets,
            apply=effective_apply,
            max_orders=int(script.get("max_orders_per_tick", args.max_orders)),
            max_notional_per_order=duel.STARTING_EQUITY * duel.MAX_TRADE_FRACTION,
        )
        agent_records[agent_id] = {"script": script, "decision": decision, "result": result}

    final_state = duel.load_state(data_dir)
    prices = duel.market_price_map(markets)
    scores = [duel.portfolio_value(final_state["accounts"][agent], prices) for agent in duel.AGENTS]
    scores.sort(key=lambda row: row["score"], reverse=True)

    tick = {
        "ok": True,
        "tick_id": snapshot_id,
        "ts": duel.utc_now(),
        "mode": mode,
        "effective_mode": "hold_only" if hold_only else mode,
        "applied": effective_apply,
        "runner": "resident_mechanical_bot_loop",
        "loop_interval_sec": loop_interval(data_dir),
        "snapshot_file": str(snapshot_file),
        "market_count": len(markets),
        "market_source": market_source,
        "data_quality_gate": gate,
        "bot_script_manifest": bot_registry,
        "shared_snapshot": True,
        "agents": agent_records,
        "scores": scores,
        "strategy_rules": strategy_rules.summarize_rules(data_dir),
    }
    append_jsonl(tick_path(data_dir), tick)
    dashboard_dir = args.dashboard_dir or os.environ.get("AURUM_PUBLIC_DASHBOARD_DIR", "")
    if dashboard_dir:
        dash_args = argparse.Namespace(data_dir=str(data_dir), env_file=str(args.env_file or ""), output_dir=dashboard_dir)
        tick["dashboard_path"] = str(generate_dashboard.render(dash_args))
        # Rewrite the just-appended tick is deliberately avoided; dashboard_path is
        # printed to journald and the dashboard itself is the persistent artifact.
    return tick


def run_loop(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    loops = 0
    while not _STOP:
        started = time.monotonic()
        tick = run_mechanical_tick(args)
        print(json.dumps(tick, ensure_ascii=False, sort_keys=True), flush=True)
        loops += 1
        if args.once or (args.max_loops and loops >= args.max_loops):
            return 0
        interval = loop_interval(pathlib.Path(args.data_dir))
        elapsed = time.monotonic() - started
        sleep_for = max(0.0, interval - elapsed)
        end = time.monotonic() + sleep_for
        while not _STOP and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run resident mechanical Aurum bot loop")
    parser.add_argument("--data-dir", default="data/paper_duel")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--mode", choices=("review_only", "paper_apply"), default=None)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--min-volume", type=float, default=0.0)
    parser.add_argument("--max-orders", type=int, default=2)
    parser.add_argument("--mock-markets", action="store_true")
    parser.add_argument("--allow-proxy", action="store_true")
    parser.add_argument("--dashboard-dir", default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-loops", type=int, default=0)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_loop(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
