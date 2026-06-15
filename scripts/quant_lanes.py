#!/usr/bin/env python3
"""Quant-lane governance helpers for Aurum's paper-only model duel.

This module is deliberately artifact-first and stdlib-only.  It does not call
models, touch live wallets, or place orders; it records lane metadata, explicit
operator controls, baseline comparisons, and deterministic protective actions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import agent_duel as duel

LANE_SCHEMA_VERSION = 1
CONTROL_SCHEMA_VERSION = 1
PROMOTION_GATE_VERSION = 1
BLACK_SWAN_FLOW_VERSION = 1

ACTIVE_STATUSES = {"active", "review_only"}
HOLD_STATUSES = {"paused", "hold_only", "frozen"}
REVIEW_OUTCOMES = ("KEEP_CURRENT_STRATEGY", "PROPOSE_UPDATE", "REQUEST_HOLD_ONLY")

DEFAULT_REVIEW_CADENCE_SECONDS = 30 * 60
MIN_REVIEW_CADENCE_SECONDS = 5 * 60
MAX_REVIEW_CADENCE_SECONDS = 24 * 60 * 60

BASELINE_NAMES = (
    "no_trade",
    "buy_and_hold",
    "simple_momentum",
    "simple_mean_reversion",
    "random_safe",
)

DEFAULT_LANE_DEFS = {
    "superwing": {"label": "SuperWing", "model_family": "superwing", "kind": "deterministic_baseline"},
    "deepseek": {"label": "DeepSeek", "model_family": "deepseek", "kind": "model_lane"},
    "gpt": {"label": "GPT", "model_family": "gpt", "kind": "future_model_lane"},
    "claude": {"label": "Claude", "model_family": "claude", "kind": "future_model_lane"},
    "manual": {"label": "Manual", "model_family": "manual", "kind": "operator_lane"},
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(json_safe(value)).encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else "[non-finite-number]"
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def lanes_root(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "quant_lanes"


def lane_registry_path(data_dir: pathlib.Path) -> pathlib.Path:
    return lanes_root(data_dir) / "registry.json"


def lane_controls_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return lanes_root(data_dir) / "controls"


def lane_control_path(data_dir: pathlib.Path, lane_id: str) -> pathlib.Path:
    return lane_controls_dir(data_dir) / f"{lane_id}.json"


def lane_events_path(data_dir: pathlib.Path) -> pathlib.Path:
    return lanes_root(data_dir) / "events.jsonl"


def baseline_report_path(data_dir: pathlib.Path) -> pathlib.Path:
    return lanes_root(data_dir) / "baseline_report.json"


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(json_safe(record), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def review_cadence_seconds(env: Optional[Dict[str, str]] = None) -> int:
    values = env if env is not None else __import__("os").environ
    raw = values.get("AURUM_REVIEW_CADENCE_SECONDS") or values.get("AURUM_REVIEW_INTERVAL_SECONDS")
    if raw is None and values.get("AURUM_REVIEW_INTERVAL_MINUTES"):
        try:
            raw = str(int(float(values["AURUM_REVIEW_INTERVAL_MINUTES"]) * 60))
        except Exception:
            raw = None
    if raw is None and values.get("AURUM_REVIEW_INTERVAL_HOURS"):
        try:
            raw = str(int(float(values["AURUM_REVIEW_INTERVAL_HOURS"]) * 3600))
        except Exception:
            raw = None
    try:
        parsed = int(float(raw)) if raw is not None else DEFAULT_REVIEW_CADENCE_SECONDS
    except Exception:
        parsed = DEFAULT_REVIEW_CADENCE_SECONDS
    return max(MIN_REVIEW_CADENCE_SECONDS, min(MAX_REVIEW_CADENCE_SECONDS, parsed))


def _read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if value is not None else default
    except Exception:
        return default


def _state_accounts(data_dir: pathlib.Path) -> Dict[str, Any]:
    try:
        state = duel.load_state(data_dir)
    except Exception:
        return {}
    accounts = state.get("accounts")
    return accounts if isinstance(accounts, dict) else {}


def default_lane_entry(lane_id: str, *, existing_account: bool = False) -> Dict[str, Any]:
    lane_def = DEFAULT_LANE_DEFS[lane_id]
    active_now = lane_id in duel.AGENTS
    status = "active" if active_now and lane_id == "superwing" else "review_only" if active_now else "dormant"
    return {
        "lane_id": lane_id,
        "label": lane_def["label"],
        "model_family": lane_def["model_family"],
        "kind": lane_def["kind"],
        "paper_only": True,
        "status": status,
        "account_state": "present" if existing_account else "not_created",
        "strategy_slot": f"bot_scripts/current/{lane_id}.json" if active_now else "",
        "review_record_scope": "own_ledger_plus_public_aggregate_scoreboard",
        "public_scope": "coarse_aggregate_only",
        "raw_peer_ledger_allowed": False,
        "created_for_future_migration": not active_now,
    }


def load_lane_registry(data_dir: pathlib.Path) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    recorded = _read_json(lane_registry_path(data_dir), {})
    accounts = _state_accounts(data_dir)
    lanes: Dict[str, Dict[str, Any]] = {}
    old_lanes = recorded.get("lanes") if isinstance(recorded, dict) and isinstance(recorded.get("lanes"), dict) else {}
    for lane_id in DEFAULT_LANE_DEFS:
        entry = default_lane_entry(lane_id, existing_account=isinstance(accounts.get(lane_id), dict))
        old_entry = old_lanes.get(lane_id) if isinstance(old_lanes, dict) else None
        if isinstance(old_entry, dict):
            merged = {**entry, **{k: v for k, v in old_entry.items() if k in entry or k.startswith("operator_")}}
            merged["paper_only"] = True
            merged["raw_peer_ledger_allowed"] = False
            lanes[lane_id] = merged
        else:
            lanes[lane_id] = entry
    payload = {
        "schema_version": LANE_SCHEMA_VERSION,
        "paper_only": True,
        "lane_order": list(DEFAULT_LANE_DEFS),
        "lanes": lanes,
        "hash_fields": ["lane_id", "model_family", "status", "strategy_slot", "public_scope", "review_record_scope"],
    }
    payload["registry_sha256"] = sha256_json({k: v for k, v in payload.items() if k != "registry_sha256"})
    return payload


def write_lane_registry(data_dir: pathlib.Path) -> pathlib.Path:
    payload = load_lane_registry(data_dir)
    path = lane_registry_path(pathlib.Path(data_dir))
    atomic_write_json(path, payload)
    return path


def load_lane_control(data_dir: pathlib.Path, lane_id: str) -> Dict[str, Any]:
    if lane_id not in DEFAULT_LANE_DEFS:
        raise duel.DuelError(f"unknown lane_id: {lane_id}")
    payload = _read_json(lane_control_path(pathlib.Path(data_dir), lane_id), {})
    if not isinstance(payload, dict):
        payload = {}
    default_status = "active" if lane_id in duel.AGENTS else "hold_only"
    status = str(payload.get("status") or default_status)
    if status not in {"active", "paused", "hold_only", "frozen"}:
        status = "hold_only"
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "lane_id": lane_id,
        "status": status,
        "reason": str(payload.get("reason") or ""),
        "operator": str(payload.get("operator") or "local_operator")[:120],
        "updated_at": str(payload.get("updated_at") or ""),
        "cooldown_until": str(payload.get("cooldown_until") or ""),
        "resume_requires_gate": bool(payload.get("resume_requires_gate", status in {"frozen", "hold_only"})),
    }


def set_lane_control(
    data_dir: pathlib.Path,
    lane_id: str,
    status: str,
    *,
    reason: str = "",
    operator: str = "local_operator",
    cooldown_until: str = "",
) -> Dict[str, Any]:
    if status not in {"active", "paused", "hold_only", "frozen"}:
        raise duel.DuelError("lane control status must be active, paused, hold_only, or frozen")
    payload = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "lane_id": lane_id,
        "status": status,
        "reason": str(reason or "")[:500],
        "operator": str(operator or "local_operator")[:120],
        "updated_at": utc_now(),
        "cooldown_until": cooldown_until,
        "resume_requires_gate": status in {"hold_only", "frozen"},
    }
    atomic_write_json(lane_control_path(pathlib.Path(data_dir), lane_id), payload)
    append_jsonl(lane_events_path(pathlib.Path(data_dir)), {"ts": utc_now(), "event": "lane_control_set", **payload})
    return payload


def lane_trade_allowed(data_dir: pathlib.Path, lane_id: str, *, script_tradable: bool, gate_ok: bool = True) -> Tuple[bool, List[str]]:
    control = load_lane_control(pathlib.Path(data_dir), lane_id)
    reasons: List[str] = []
    if not gate_ok:
        reasons.append("data_gate_not_trade_allowed")
    if not script_tradable:
        reasons.append("script_not_tradable")
    if control["status"] in HOLD_STATUSES:
        reasons.append("lane_control_" + control["status"])
    return not reasons, reasons


def can_resume_lane(control: Dict[str, Any], gate_result: Dict[str, Any], *, now: Optional[str] = None) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    cooldown_until = str(control.get("cooldown_until") or "")
    if cooldown_until:
        try:
            target = dt.datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
            current = dt.datetime.fromisoformat((now or utc_now()).replace("Z", "+00:00"))
            if current < target:
                reasons.append("cooldown_active")
        except Exception:
            reasons.append("cooldown_unparseable")
    if control.get("resume_requires_gate", True):
        if gate_result.get("status") != "pass" or gate_result.get("executable") is not True:
            reasons.append("resume_gate_not_passed")
    return not reasons, reasons


def lane_operator_rows(
    data_dir: pathlib.Path,
    state: Dict[str, Any],
    scores: Iterable[Dict[str, Any]],
    reviews: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    scores_by_agent = {str(row.get("agent_id")): row for row in scores if isinstance(row, dict)}
    reviews_list = [row for row in reviews if isinstance(row, dict)]
    accounts = state.get("accounts") if isinstance(state.get("accounts"), dict) else {}
    registry = load_lane_registry(data_dir)
    rows: List[Dict[str, Any]] = []
    for lane_id, entry in registry["lanes"].items():
        account = accounts.get(lane_id) if isinstance(accounts, dict) else None
        score = scores_by_agent.get(lane_id, {})
        latest_review = next((row for row in reversed(reviews_list) if lane_id in str(row.get("review_model", "")) or row.get("target_agent_id") == lane_id), {})
        rows.append(
            {
                "lane_id": lane_id,
                "label": entry["label"],
                "model_family": entry["model_family"],
                "status": entry["status"],
                "control": load_lane_control(data_dir, lane_id),
                "strategy_slot": entry["strategy_slot"],
                "last_review_id": latest_review.get("review_id", ""),
                "review_status": latest_review.get("review_status", ""),
                "roi": score.get("roi"),
                "fees_paid": score.get("fees_paid"),
                "drawdown": score.get("drawdown"),
                "trade_count": score.get("trade_count"),
                "risk_rejects": score.get("risk_event_count"),
                "own_ledger": duel.compact_account(account) if isinstance(account, dict) else {},
                "peer_context": "public_aggregate_scoreboard_only",
            }
        )
    return rows


def _extract_price_series(frames: Iterable[Dict[str, Any]]) -> List[float]:
    values: List[float] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        if "value" in frame:
            raw = frame.get("value")
        elif "price" in frame:
            raw = frame.get("price")
        else:
            raw = None
            markets = frame.get("markets") if isinstance(frame.get("markets"), list) else []
            for market in markets:
                if not isinstance(market, dict):
                    continue
                for outcome in market.get("outcomes", []) or []:
                    if isinstance(outcome, dict) and str(outcome.get("name", "")).lower() in {"yes", "up", "higher", "above"}:
                        raw = outcome.get("price")
                        break
                if raw is not None:
                    break
        try:
            parsed = float(raw)
        except Exception:
            continue
        if math.isfinite(parsed) and 0.0 < parsed < 1.0:
            values.append(parsed)
    return values


def _fee_drag(notional_turnover: float, avg_price: float = 0.5) -> float:
    fee = notional_turnover * duel.TAKER_FEE_RATES["crypto"] * avg_price * (1.0 - avg_price)
    return fee / duel.STARTING_EQUITY


def _max_drawdown(values: List[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return round(max_dd, 6)


def _metric(name: str, roi: float, drawdown: float, trade_count: int, exposure: float, churn: float) -> Dict[str, Any]:
    return {
        "name": name,
        "after_fee_roi": round(roi, 6),
        "max_drawdown": round(drawdown, 6),
        "trade_count": int(trade_count),
        "churn": round(churn, 6),
        "exposure_concentration": round(exposure, 6),
        "fee_model": "polymarket_crypto_taker_fee",
    }


def evaluate_baselines(frames: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    prices = _extract_price_series(frames)
    if len(prices) < 2:
        metrics = {name: _metric(name, 0.0, 0.0, 0, 0.0, 0.0) for name in BASELINE_NAMES}
        return {"ok": False, "reason": "insufficient_price_series", "price_points": len(prices), "baselines": metrics}
    first = prices[0]
    last = prices[-1]
    price_roi = (last - first) / first if first else 0.0
    buy_hold_turnover = duel.STARTING_EQUITY * 0.35
    buy_hold_roi = price_roi * 0.35 - _fee_drag(buy_hold_turnover)
    momentum_trades = max(1, min(12, len(prices) // 4))
    momentum_signal = 1.0 if prices[-1] >= prices[max(0, len(prices) // 2)] else -0.25
    momentum_roi = price_roi * 0.18 * momentum_signal - _fee_drag(momentum_trades * 30.0)
    mean_price = sum(prices) / len(prices)
    reversion_signal = -1.0 if last > mean_price else 1.0
    reversion_roi = price_roi * 0.12 * reversion_signal - _fee_drag(4 * 25.0)
    random_steps = [1 if hashlib.sha256(f"{idx}:{value}".encode("utf-8")).digest()[0] % 2 == 0 else -1 for idx, value in enumerate(prices[1:], 1)]
    random_edge = sum(random_steps) / max(1, len(random_steps)) * abs(price_roi) * 0.05
    metrics = {
        "no_trade": _metric("no_trade", 0.0, 0.0, 0, 0.0, 0.0),
        "buy_and_hold": _metric("buy_and_hold", buy_hold_roi, _max_drawdown(prices), 1, 0.35, 0.35),
        "simple_momentum": _metric("simple_momentum", momentum_roi, _max_drawdown(prices), momentum_trades, 0.18, momentum_trades * 30.0 / duel.STARTING_EQUITY),
        "simple_mean_reversion": _metric("simple_mean_reversion", reversion_roi, _max_drawdown(prices), 4, 0.12, 100.0 / duel.STARTING_EQUITY),
        "random_safe": _metric("random_safe", random_edge - _fee_drag(5 * 10.0), _max_drawdown(prices) * 0.25, 5, 0.05, 50.0 / duel.STARTING_EQUITY),
    }
    return {"ok": True, "price_points": len(prices), "baselines": metrics}


def best_baseline_roi(baselines: Dict[str, Any]) -> float:
    rows = baselines.get("baselines") if isinstance(baselines.get("baselines"), dict) else {}
    best = 0.0
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        try:
            best = max(best, float(row.get("after_fee_roi", 0.0) or 0.0))
        except Exception:
            continue
    return best


def evaluate_promotion_gate(
    proposal_metrics: Dict[str, Any],
    baseline_results: Dict[str, Any],
    *,
    min_edge: float = 0.001,
    max_drawdown: float = 0.08,
    max_churn: float = 0.60,
    max_exposure_concentration: float = 0.35,
    min_trade_count: int = 1,
) -> Dict[str, Any]:
    reasons: List[str] = []
    if proposal_metrics.get("schema_valid") is not True:
        reasons.append("schema_not_valid")
    if proposal_metrics.get("replay_ok") is not True:
        reasons.append("replay_not_ok")
    if proposal_metrics.get("holdout_ok") is not True:
        reasons.append("holdout_not_ok")
    if baseline_results.get("ok") is not True:
        reasons.append("baselines_not_ok")
    roi = float(proposal_metrics.get("after_fee_roi", 0.0) or 0.0)
    baseline_roi = best_baseline_roi(baseline_results)
    if roi < baseline_roi + min_edge:
        reasons.append("does_not_beat_best_baseline_after_fees")
    drawdown = float(proposal_metrics.get("max_drawdown", 0.0) or 0.0)
    if drawdown > max_drawdown:
        reasons.append("drawdown_too_high")
    trade_count = int(proposal_metrics.get("trade_count", 0) or 0)
    if trade_count < min_trade_count:
        reasons.append("trade_count_too_low")
    churn = float(proposal_metrics.get("churn", 0.0) or 0.0)
    if churn > max_churn:
        reasons.append("churn_too_high")
    exposure = float(proposal_metrics.get("exposure_concentration", 0.0) or 0.0)
    if exposure > max_exposure_concentration:
        reasons.append("exposure_concentration_too_high")
    status = "pass" if not reasons else "rejected"
    return {
        "version": PROMOTION_GATE_VERSION,
        "status": status,
        "executable": status == "pass",
        "reasons": reasons,
        "proposal_after_fee_roi": round(roi, 6),
        "best_baseline_after_fee_roi": round(baseline_roi, 6),
        "baseline_names": list(BASELINE_NAMES),
        "public_summary": "beat_baselines_after_fees" if status == "pass" else "draft_or_rejected_not_executable",
    }


def detect_extreme_events(
    *,
    market: Optional[Dict[str, Any]] = None,
    account: Optional[Dict[str, Any]] = None,
    system: Optional[Dict[str, Any]] = None,
) -> List[str]:
    reasons: List[str] = []
    market = market or {}
    account = account or {}
    system = system or {}
    if abs(float(market.get("price_jump_abs", 0.0) or 0.0)) >= 0.18:
        reasons.append("market_price_jump")
    if float(market.get("spread", 0.0) or 0.0) >= 0.15 or float(market.get("depth_ok_ratio", 1.0) or 1.0) < 0.5:
        reasons.append("spread_or_depth_collapse")
    if bool(market.get("recorder_stale")):
        reasons.append("recorder_stale")
    if market.get("manifest_ok") is False:
        reasons.append("manifest_discontinuity")
    if market.get("api_agreement_ok") is False:
        reasons.append("api_disagreement")
    if str(market.get("market_status") or "").lower() in {"closed", "resolved", "paused", "halted"}:
        reasons.append("market_closed_resolved_or_paused")
    if float(account.get("drawdown", 0.0) or 0.0) >= 0.08:
        reasons.append("drawdown_breach")
    if float(account.get("fee_to_equity", 0.0) or 0.0) >= 0.02 or abs(float(account.get("equity_spike_abs", 0.0) or 0.0)) >= 0.10:
        reasons.append("fee_or_equity_spike")
    if int(account.get("repeated_stop_loss_or_rejects", 0) or 0) >= 3:
        reasons.append("repeated_stop_loss_or_rejects")
    if float(account.get("exposure_concentration", 0.0) or 0.0) >= 0.35:
        reasons.append("concentration_breach")
    if int(system.get("restart_count", 0) or 0) >= 3:
        reasons.append("service_restart_spike")
    for key in ("replay_ok", "backup_ok", "status_report_ok"):
        if system.get(key) is False:
            reasons.append(key.replace("_ok", "_inconsistent"))
    return reasons


def protective_flow(lane_id: str, reasons: List[str], *, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "version": BLACK_SWAN_FLOW_VERSION,
        "lane_id": lane_id,
        "reasons": list(reasons),
        "sequence": [
            "deterministic_protective_action",
            "freeze_lane",
            "snapshot_redacted_evidence",
            "trigger_slow_model_review_for_resume_update_or_retire",
        ],
        "protective_actions": [
            "cancel_simulated_orders",
            "reduce_or_close_paper_exposure_when_safe",
            "set_hold_only",
        ],
        "model_may_trade_in_hot_path": False,
        "evidence": json_safe(evidence or {}),
    }


def apply_black_swan_protection(
    data_dir: pathlib.Path,
    lane_id: str,
    reasons: List[str],
    *,
    evidence: Optional[Dict[str, Any]] = None,
    cooldown_until: str = "",
) -> Dict[str, Any]:
    flow = protective_flow(lane_id, reasons, evidence=evidence)
    control = set_lane_control(
        pathlib.Path(data_dir),
        lane_id,
        "frozen",
        reason=",".join(reasons) or "black_swan_protection",
        operator="deterministic_protective_flow",
        cooldown_until=cooldown_until,
    )
    record = {"ts": utc_now(), "event": "black_swan_protection", "flow": flow, "control": control}
    append_jsonl(lane_events_path(pathlib.Path(data_dir)), record)
    return record


def command_registry(args: argparse.Namespace) -> None:
    payload = load_lane_registry(pathlib.Path(args.data_dir))
    if args.write:
        write_lane_registry(pathlib.Path(args.data_dir))
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def command_control(args: argparse.Namespace) -> None:
    payload = set_lane_control(
        pathlib.Path(args.data_dir),
        args.lane,
        args.status,
        reason=args.reason,
        operator=args.operator,
        cooldown_until=args.cooldown_until,
    )
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def command_baselines(args: argparse.Namespace) -> None:
    frames = _read_json(pathlib.Path(args.frames), []) if args.frames else []
    payload = evaluate_baselines(frames if isinstance(frames, list) else [])
    if args.write:
        atomic_write_json(baseline_report_path(pathlib.Path(args.data_dir)), payload)
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def command_gate(args: argparse.Namespace) -> None:
    baselines = _read_json(pathlib.Path(args.baselines), {}) if args.baselines else _read_json(baseline_report_path(pathlib.Path(args.data_dir)), {})
    metrics = _read_json(pathlib.Path(args.metrics), {}) if args.metrics else {}
    payload = evaluate_promotion_gate(metrics if isinstance(metrics, dict) else {}, baselines if isinstance(baselines, dict) else {})
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def command_protect(args: argparse.Namespace) -> None:
    payload = apply_black_swan_protection(pathlib.Path(args.data_dir), args.lane, args.reason, cooldown_until=args.cooldown_until)
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Aurum paper-only quant lanes")
    parser.add_argument("--data-dir", default="data/paper_duel")
    sub = parser.add_subparsers(dest="command", required=True)
    p_registry = sub.add_parser("registry", help="print or write the lane registry")
    p_registry.add_argument("--write", action="store_true")
    p_registry.set_defaults(func=command_registry)
    p_control = sub.add_parser("control", help="set explicit lane control status")
    p_control.add_argument("--lane", choices=tuple(DEFAULT_LANE_DEFS), required=True)
    p_control.add_argument("--status", choices=("active", "paused", "hold_only", "frozen"), required=True)
    p_control.add_argument("--reason", default="")
    p_control.add_argument("--operator", default="local_operator")
    p_control.add_argument("--cooldown-until", default="")
    p_control.set_defaults(func=command_control)
    p_baselines = sub.add_parser("baselines", help="evaluate baseline metrics from a JSON frame list")
    p_baselines.add_argument("--frames", default="")
    p_baselines.add_argument("--write", action="store_true")
    p_baselines.set_defaults(func=command_baselines)
    p_gate = sub.add_parser("gate", help="evaluate a promotion gate from JSON metrics")
    p_gate.add_argument("--metrics", default="")
    p_gate.add_argument("--baselines", default="")
    p_gate.set_defaults(func=command_gate)
    p_protect = sub.add_parser("protect", help="freeze a lane after deterministic black-swan protection")
    p_protect.add_argument("--lane", choices=tuple(DEFAULT_LANE_DEFS), required=True)
    p_protect.add_argument("--reason", action="append", default=[])
    p_protect.add_argument("--cooldown-until", default="")
    p_protect.set_defaults(func=command_protect)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
