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
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import agent_duel as duel
import strategy_rules

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


def proposal_decisions_path(data_dir: pathlib.Path) -> pathlib.Path:
    return lanes_root(data_dir) / "proposal_decisions.jsonl"


def proposal_decision_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return lanes_root(data_dir) / "proposal_decisions"


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(json_safe(record), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def _read_jsonl(path: pathlib.Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except Exception:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except Exception:
        return []
    return rows


SENSITIVE_EVIDENCE_KEY_PARTS = (
    "api",
    "authorization",
    "bearer",
    "connection",
    "credential",
    "env",
    "file",
    "host",
    "hostname",
    "ip",
    "key",
    "password",
    "path",
    "private",
    "remote",
    "root",
    "secret",
    "server",
    "ssh",
    "token",
)
SENSITIVE_EVIDENCE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"(?:ssh|postgres|mysql|mongodb|redis)://[^\s\"']+", re.I),
    re.compile(r"(?:^|[\s\"'])/(?:Users|home|root|opt|etc|var)/[^\s\"']+"),
)


def redact_evidence(value: Any, key: Any = "") -> Any:
    key_text = str(key or "").lower()
    if any(part in key_text for part in SENSITIVE_EVIDENCE_KEY_PARTS) and isinstance(value, (str, int, float, bool)):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): redact_evidence(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_evidence(item, key) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in SENSITIVE_EVIDENCE_PATTERNS:
            redacted = pattern.sub("[redacted]", redacted)
        return redacted
    return value


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


PROPOSAL_SUFFIXES = {
    "_rules.json": "rules_json",
    "_rules.md": "rules_markdown",
    "_bot_script.json": "bot_script_json",
}


def _parse_proposal_artifact(path: pathlib.Path) -> Optional[Dict[str, str]]:
    name = path.name
    for agent_id in DEFAULT_LANE_DEFS:
        for suffix, kind in PROPOSAL_SUFFIXES.items():
            marker = f"_{agent_id}{suffix}"
            if name.endswith(marker):
                review_id = name[: -len(marker)]
                if review_id:
                    return {"review_id": review_id, "agent_id": agent_id, "artifact_kind": kind}
    return None


def _proposal_id(path: pathlib.Path) -> str:
    return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]


def _proposal_decision_index(data_dir: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    decisions: Dict[str, Dict[str, Any]] = {}
    for row in _read_jsonl(proposal_decisions_path(pathlib.Path(data_dir))):
        proposal_id = str(row.get("proposal_id") or "")
        if proposal_id:
            decisions[proposal_id] = row
    return decisions


def _review_gate_for_proposal(data_dir: pathlib.Path, review_id: str, agent_id: str) -> Dict[str, Any]:
    review = _read_json(pathlib.Path(data_dir) / "strategy_reviews" / f"{review_id}.json", {})
    if isinstance(review, dict):
        gates = review.get("promotion_gates") if isinstance(review.get("promotion_gates"), dict) else {}
        gate = gates.get(agent_id) if isinstance(gates, dict) else None
        if isinstance(gate, dict):
            return gate
    return {"status": "missing", "executable": False, "reasons": ["promotion_gate_missing"]}


def list_strategy_proposals(data_dir: pathlib.Path, *, include_decided: bool = True) -> List[Dict[str, Any]]:
    data_dir = pathlib.Path(data_dir)
    root = strategy_rules.proposal_dir(data_dir)
    if not root.exists():
        return []
    decisions = _proposal_decision_index(data_dir)
    proposals: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*")):
        if not path.is_file():
            continue
        parsed = _parse_proposal_artifact(path)
        if not parsed:
            continue
        proposal_id = _proposal_id(path)
        decision = decisions.get(proposal_id)
        if decision and not include_decided:
            continue
        gate = _review_gate_for_proposal(data_dir, parsed["review_id"], parsed["agent_id"])
        can_approve = gate.get("status") == "pass" and gate.get("executable") is True
        proposals.append(
            {
                "proposal_id": proposal_id,
                "artifact": path.name,
                "review_id": parsed["review_id"],
                "agent_id": parsed["agent_id"],
                "artifact_kind": parsed["artifact_kind"],
                "status": str((decision or {}).get("status") or "pending"),
                "gate_status": gate.get("status"),
                "gate_executable": gate.get("executable") is True,
                "gate_reasons": gate.get("reasons", []) if isinstance(gate.get("reasons"), list) else [],
                "can_approve": can_approve,
                "decision": decision or {},
            }
        )
    return proposals


def resolve_strategy_proposal(data_dir: pathlib.Path, proposal: str) -> Dict[str, Any]:
    wanted = str(proposal or "").strip()
    if not wanted:
        raise duel.DuelError("proposal id or artifact is required")
    for item in list_strategy_proposals(data_dir, include_decided=True):
        if wanted in {item["proposal_id"], item["artifact"]}:
            return item
    raise duel.DuelError(f"unknown strategy proposal: {wanted}")


def record_proposal_decision(
    data_dir: pathlib.Path,
    proposal: str,
    action: str,
    *,
    operator: str = "local_operator",
    reason: str = "",
) -> Dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise duel.DuelError("proposal action must be approve or reject")
    item = resolve_strategy_proposal(pathlib.Path(data_dir), proposal)
    if item.get("status") in {"approved", "rejected"}:
        raise duel.DuelError(f"proposal already {item.get('status')}: {item.get('proposal_id')}")
    if action == "approve" and not item.get("can_approve"):
        raise duel.DuelError(
            "proposal cannot be approved before schema/replay/holdout/baseline gate passes: "
            + ",".join(str(reason) for reason in item.get("gate_reasons", []))
        )
    record = {
        "ts": utc_now(),
        "event": "strategy_proposal_decision",
        "proposal_id": item["proposal_id"],
        "artifact": item["artifact"],
        "review_id": item["review_id"],
        "agent_id": item["agent_id"],
        "artifact_kind": item["artifact_kind"],
        "status": "approved" if action == "approve" else "rejected",
        "operator": str(operator or "local_operator")[:120],
        "reason": str(reason or "")[:500],
        "gate_status": item.get("gate_status"),
        "gate_executable": item.get("gate_executable") is True,
        "gate_reasons": item.get("gate_reasons", []),
        "paper_only": True,
        "promotion_is_separate_step": True,
    }
    atomic_write_json(proposal_decision_dir(pathlib.Path(data_dir)) / f"{item['proposal_id']}.json", record)
    append_jsonl(proposal_decisions_path(pathlib.Path(data_dir)), record)
    return record


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


def protective_flow(
    lane_id: str,
    reasons: List[str],
    *,
    evidence: Optional[Dict[str, Any]] = None,
    protective_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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
        "evidence": redact_evidence(evidence or {}),
        "protective_result": json_safe(protective_result or {}),
    }


def _outcome_price(market: Dict[str, Any], outcome_name: str) -> Optional[float]:
    for outcome in market.get("outcomes", []) or []:
        if not isinstance(outcome, dict):
            continue
        if str(outcome.get("name", "")).lower() != str(outcome_name or "").lower():
            continue
        try:
            parsed = float(outcome.get("price"))
        except Exception:
            return None
        return parsed if math.isfinite(parsed) and 0.0 < parsed < 1.0 else None
    return None


def protective_close_orders(state: Dict[str, Any], lane_id: str, reasons: List[str], *, close_fraction: float = 1.0) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    accounts = state.get("accounts") if isinstance(state.get("accounts"), dict) else {}
    account = accounts.get(lane_id) if isinstance(accounts, dict) else {}
    positions = account.get("positions") if isinstance(account, dict) and isinstance(account.get("positions"), dict) else {}
    markets = state.get("last_markets") if isinstance(state.get("last_markets"), list) else []
    market_by_id = {str(market.get("market_id")): market for market in markets if isinstance(market, dict)}
    orders: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    fraction = max(0.0, min(1.0, float(close_fraction or 1.0)))
    reason_text = ",".join(reasons) or "black_swan_protection"
    for pos_key, position in positions.items():
        if not isinstance(position, dict):
            continue
        market_id = str(position.get("market_id") or "")
        outcome_name = str(position.get("outcome") or "")
        market = market_by_id.get(market_id)
        if not market:
            skipped.append({"position": str(pos_key), "market_id": market_id, "reason": "latest_market_missing"})
            continue
        price = _outcome_price(market, outcome_name)
        if price is None:
            skipped.append({"position": str(pos_key), "market_id": market_id, "outcome": outcome_name, "reason": "latest_outcome_price_missing"})
            continue
        try:
            shares = float(position.get("shares") or 0.0) * fraction
        except Exception:
            shares = 0.0
        if not math.isfinite(shares) or shares <= 0:
            skipped.append({"position": str(pos_key), "market_id": market_id, "outcome": outcome_name, "reason": "no_positive_shares"})
            continue
        orders.append(
            {
                "market_id": market_id,
                "outcome": outcome_name,
                "side": "sell",
                "shares": round(shares, 6),
                "limit_price": round(max(0.01, price - duel.SLIPPAGE_BPS / 10000.0), 4),
                "rationale": "deterministic black-swan protective close before model review: " + reason_text[:320],
            }
        )
    return orders, skipped


def execute_deterministic_protection(data_dir: pathlib.Path, lane_id: str, reasons: List[str]) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    try:
        state = duel.init_state(data_dir, reset=False)
    except Exception:
        state = duel.init_state(data_dir, reset=False)
    account_before = duel.account_digest(state.get("accounts", {}).get(lane_id, {})) if lane_id in state.get("accounts", {}) else {}
    orders, skipped = protective_close_orders(state, lane_id, reasons)
    markets = state.get("last_markets") if isinstance(state.get("last_markets"), list) else []
    decision = {
        "agent_id": lane_id,
        "source": "deterministic_black_swan_protection",
        "orders": orders,
        "notes": "cancel simulated orders; close/reduce paper exposure if latest market prices are available; freeze lane before model review",
    }
    if orders:
        result = duel.validate_and_apply(
            data_dir,
            state,
            lane_id,
            decision,
            markets,
            apply=True,
            max_orders=max(1, len(orders)),
            max_notional_per_order=duel.STARTING_EQUITY,
            execution_context={"source": "black_swan_protection", "black_swan_reasons": reasons},
        )
        state_after = duel.load_state(data_dir)
    else:
        result = {"fills": [], "rejections": [], "applied": True, "reason": "no_closeable_open_positions"}
        state_after = state
    account_after = duel.account_digest(state_after.get("accounts", {}).get(lane_id, {})) if lane_id in state_after.get("accounts", {}) else {}
    status = "closed_or_reduced" if result.get("fills") else "attempted_no_fills" if orders else "nothing_to_close"
    record = {
        "status": status,
        "protective_action_order": 1,
        "orders_attempted": len(orders),
        "fills": len(result.get("fills", []) or []),
        "rejections": len(result.get("rejections", []) or []),
        "skipped_positions": skipped,
        "decision": decision,
        "result": result,
        "account_delta": {"before": account_before, "after": account_after},
    }
    duel.append_risk_ledger(
        data_dir,
        {
            "ts": utc_now(),
            "event": "black_swan_deterministic_protection",
            "agent_id": lane_id,
            "reasons": reasons,
            "protective_result": record,
            "account_delta": record["account_delta"],
        },
    )
    return record


def apply_black_swan_protection(
    data_dir: pathlib.Path,
    lane_id: str,
    reasons: List[str],
    *,
    evidence: Optional[Dict[str, Any]] = None,
    cooldown_until: str = "",
) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    protective_result = execute_deterministic_protection(data_dir, lane_id, reasons)
    control = set_lane_control(
        data_dir,
        lane_id,
        "frozen",
        reason=",".join(reasons) or "black_swan_protection",
        operator="deterministic_protective_flow",
        cooldown_until=cooldown_until,
    )
    flow = protective_flow(lane_id, reasons, evidence=evidence, protective_result=protective_result)
    record = {"ts": utc_now(), "event": "black_swan_protection", "flow": flow, "control": control}
    append_jsonl(lane_events_path(data_dir), record)
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
    evidence = _read_json(pathlib.Path(args.evidence), {}) if args.evidence else {}
    payload = apply_black_swan_protection(pathlib.Path(args.data_dir), args.lane, args.reason, evidence=evidence if isinstance(evidence, dict) else {}, cooldown_until=args.cooldown_until)
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def command_proposals(args: argparse.Namespace) -> None:
    payload = {"ok": True, "proposals": list_strategy_proposals(pathlib.Path(args.data_dir), include_decided=args.include_decided)}
    print(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def command_proposal_decision(args: argparse.Namespace) -> None:
    payload = record_proposal_decision(
        pathlib.Path(args.data_dir),
        args.proposal,
        args.action,
        operator=args.operator,
        reason=args.reason,
    )
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
    p_protect = sub.add_parser("protect", help="execute deterministic black-swan protection, then freeze a lane")
    p_protect.add_argument("--lane", choices=tuple(DEFAULT_LANE_DEFS), required=True)
    p_protect.add_argument("--reason", action="append", default=[])
    p_protect.add_argument("--cooldown-until", default="")
    p_protect.add_argument("--evidence", default="")
    p_protect.set_defaults(func=command_protect)
    p_proposals = sub.add_parser("proposals", help="list pending/reviewed strategy proposal artifacts and gate status")
    p_proposals.add_argument("--include-decided", action="store_true")
    p_proposals.set_defaults(func=command_proposals)
    p_proposal_decision = sub.add_parser("proposal-decision", help="approve or reject a proposal after gate validation")
    p_proposal_decision.add_argument("action", choices=("approve", "reject"))
    p_proposal_decision.add_argument("--proposal", required=True)
    p_proposal_decision.add_argument("--operator", default="local_operator")
    p_proposal_decision.add_argument("--reason", default="")
    p_proposal_decision.set_defaults(func=command_proposal_decision)
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
