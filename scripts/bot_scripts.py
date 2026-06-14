#!/usr/bin/env python3
"""Mechanical bot scripts for the Aurum paper duel.

This is the bot layer Queen Glasser asked for: agents may write/review these
versioned scripts, while the hot-path bot loop executes them mechanically without
asking an LLM on every tick.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import math
import pathlib
from typing import Any, Dict, List, Optional

import agent_duel as duel
import strategy_rules

SCRIPT_VERSION = 1
REGISTRY_SCHEMA_VERSION = 2
DEFAULT_INTERVAL_SEC = 15
HARD_MIN_INTERVAL_SEC = 5
MAX_INTERVAL_SEC = 3600
MAX_ORDERS_PER_TICK = 8
MAX_LIMIT_BUFFER = 0.10
MAX_RULE_SECONDS = 365 * 24 * 3600
TRADABLE_REVIEW_STATUSES = {"reviewed", "promoted"}
REVIEW_STATUSES = {"draft", "reviewed", "promoted", "revoked"}

TOP_LEVEL_REQUIRED_FIELDS = {
    "version",
    "agent_id",
    "execution_mode",
    "generated_by",
    "status",
    "hold_only",
    "updated_at",
    "interval_sec",
    "min_interval_sec",
    "max_orders_per_tick",
    "allowed_sides",
    "buy_when",
    "sell_when",
    "hold_when",
    "review",
}
TOP_LEVEL_FIELDS = TOP_LEVEL_REQUIRED_FIELDS | {"risk_reason"}
BUY_WHEN_FIELDS = {
    "selection",
    "price_min",
    "price_max",
    "min_volume",
    "max_notional",
    "limit_buffer",
    "skip_if_market_exposure_usdc_at_least",
    "market_cooldown_seconds",
    "enabled",
}
SELL_WHEN_FIELDS = {
    "take_profit_pct",
    "stop_loss_pct",
    "max_hold_seconds",
    "exit_fraction",
    "limit_buffer",
    "enabled",
}
HOLD_WHEN_FIELDS = {
    "reserve_cash",
    "respect_runner_risk_caps",
    "no_trade_if_no_matching_rule",
}
REVIEW_FIELDS = {"status", "reviewed_by", "reviewed_at", "promotion_id"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def scripts_root(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "bot_scripts"


def current_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return scripts_root(data_dir) / "current"


def history_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return scripts_root(data_dir) / "history"


def script_path(data_dir: pathlib.Path, agent_id: str) -> pathlib.Path:
    return current_dir(data_dir) / f"{agent_id}.json"


def manifest_path(data_dir: pathlib.Path) -> pathlib.Path:
    return scripts_root(data_dir) / "manifest.json"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def reviewed_metadata(source: str, *, status: str = "promoted") -> Dict[str, Any]:
    now = utc_now()
    source_label = str(source or "unknown")[:120]
    return {
        "status": status,
        "reviewed_by": source_label,
        "reviewed_at": now,
        "promotion_id": hashlib.sha256(f"{source_label}:{now}".encode("utf-8")).hexdigest()[:16],
    }


def draft_review_metadata() -> Dict[str, Any]:
    return {"status": "draft", "reviewed_by": "", "reviewed_at": "", "promotion_id": ""}


def default_bot_script(agent_id: str) -> Dict[str, Any]:
    base_rules = dict(strategy_rules.DEFAULT_SUPERWING_RULES)
    max_notional = 30.0 if agent_id == "superwing" else 10.0
    script = {
        "version": SCRIPT_VERSION,
        "agent_id": agent_id,
        "execution_mode": "mechanical_script",
        "generated_by": "default_agent_layer",
        "status": "active_baseline",
        "hold_only": False,
        "review": reviewed_metadata("default_agent_layer"),
        "updated_at": utc_now(),
        "interval_sec": DEFAULT_INTERVAL_SEC,
        "min_interval_sec": HARD_MIN_INTERVAL_SEC,
        "max_orders_per_tick": 2,
        "allowed_sides": ["buy", "sell"],
        "buy_when": {
            "selection": base_rules["selection"],
            "price_min": base_rules["price_min"],
            "price_max": base_rules["price_max"],
            "min_volume": base_rules["min_volume"],
            "max_notional": max_notional,
            "limit_buffer": base_rules["limit_buffer"],
            "skip_if_market_exposure_usdc_at_least": duel.STARTING_EQUITY * duel.MAX_MARKET_FRACTION,
            "market_cooldown_seconds": 300,
            "enabled": True,
        },
        "sell_when": {
            "take_profit_pct": 0.12,
            "stop_loss_pct": 0.08,
            "max_hold_seconds": 0,
            "exit_fraction": 1.0,
            "limit_buffer": 0.015,
            "enabled": True,
        },
        "hold_when": {
            "reserve_cash": duel.RESERVE_CASH,
            "respect_runner_risk_caps": True,
            "no_trade_if_no_matching_rule": True,
        },
    }
    if agent_id == "deepseek":
        script.update(
            {
                "status": "awaiting_validated_strategy",
                "hold_only": True,
                "review": draft_review_metadata(),
                "max_orders_per_tick": 0,
                "allowed_sides": [],
            }
        )
        script["buy_when"] = {**script["buy_when"], "enabled": False, "max_notional": 0.0, "selection": "Hold until DeepSeek has a validated independent strategy."}
        script["sell_when"] = {**script["sell_when"], "enabled": False}
    return script


def ensure_default_bot_scripts(data_dir: pathlib.Path, *, write_manifest: bool = True) -> None:
    current_dir(data_dir).mkdir(parents=True, exist_ok=True)
    history_dir(data_dir).mkdir(parents=True, exist_ok=True)
    changed = False
    for agent_id in duel.AGENTS:
        path = script_path(data_dir, agent_id)
        if not path.exists():
            atomic_write_json(path, default_bot_script(agent_id))
            changed = True
    if write_manifest and (changed or not manifest_path(data_dir).exists()):
        write_bot_registry_manifest(data_dir)


def hold_only_safe_script(agent_id: str, reason: str) -> Dict[str, Any]:
    script = default_bot_script(agent_id)
    script.update(
        {
            "status": "script_invalid",
            "hold_only": True,
            "risk_reason": reason,
            "review": {**draft_review_metadata(), "status": "revoked"},
            "allowed_sides": [],
            "max_orders_per_tick": 0,
        }
    )
    script["buy_when"] = {**(script.get("buy_when", {}) or {}), "enabled": False, "max_notional": 0.0}
    script["sell_when"] = {**(script.get("sell_when", {}) or {}), "enabled": False}
    return script


def _schema_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _schema_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nested_rule_schema_error(field: str, value: Dict[str, Any]) -> Optional[str]:
    allowed_fields = {
        "buy_when": BUY_WHEN_FIELDS,
        "sell_when": SELL_WHEN_FIELDS,
        "hold_when": HOLD_WHEN_FIELDS,
    }[field]
    unexpected = sorted(set(value) - allowed_fields)
    if unexpected:
        return f"schema_{field}_unknown_field:{unexpected[0]}"
    numeric_fields = {
        "buy_when": (
            "price_min",
            "price_max",
            "min_volume",
            "max_notional",
            "limit_buffer",
            "skip_if_market_exposure_usdc_at_least",
            "market_cooldown_seconds",
        ),
        "sell_when": ("take_profit_pct", "stop_loss_pct", "max_hold_seconds", "exit_fraction", "limit_buffer"),
        "hold_when": ("reserve_cash",),
    }
    boolean_fields = {
        "buy_when": ("enabled",),
        "sell_when": ("enabled",),
        "hold_when": ("respect_runner_risk_caps", "no_trade_if_no_matching_rule"),
    }
    for nested in numeric_fields.get(field, ()):  # explicit malformed numbers must not reach float()
        if nested in value and not _schema_number(value.get(nested)):
            return f"schema_{field}_{nested}_not_number"
    for nested in boolean_fields.get(field, ()):
        if nested in value and not isinstance(value.get(nested), bool):
            return f"schema_{field}_{nested}_not_bool"
    missing = sorted(allowed_fields - set(value))
    if missing:
        return f"schema_{field}_missing_field:{missing[0]}"
    if field == "buy_when":
        price_min = float(value["price_min"])
        price_max = float(value["price_max"])
        if not (0.01 <= price_min <= 0.99):
            return "schema_buy_when_price_min_out_of_range"
        if not (0.01 <= price_max <= 0.99):
            return "schema_buy_when_price_max_out_of_range"
        if price_min > price_max:
            return "schema_buy_when_price_range_inverted"
        if float(value["min_volume"]) < 0:
            return "schema_buy_when_min_volume_negative"
        max_notional = float(value["max_notional"])
        if not (0.0 <= max_notional <= duel.STARTING_EQUITY * duel.MAX_TRADE_FRACTION):
            return "schema_buy_when_max_notional_out_of_range"
        if not (0.0 <= float(value["limit_buffer"]) <= MAX_LIMIT_BUFFER):
            return "schema_buy_when_limit_buffer_out_of_range"
        exposure = float(value["skip_if_market_exposure_usdc_at_least"])
        if not (0.0 <= exposure <= duel.STARTING_EQUITY):
            return "schema_buy_when_exposure_cap_out_of_range"
        cooldown = float(value["market_cooldown_seconds"])
        if not (0.0 <= cooldown <= MAX_RULE_SECONDS):
            return "schema_buy_when_market_cooldown_out_of_range"
        if not isinstance(value.get("selection"), str) or not value.get("selection").strip():
            return "schema_buy_when_selection_not_string"
    elif field == "sell_when":
        for nested in ("take_profit_pct", "stop_loss_pct", "exit_fraction"):
            parsed = float(value[nested])
            if not (0.0 <= parsed <= 1.0):
                return f"schema_sell_when_{nested}_out_of_range"
        if not (0.0 <= float(value["limit_buffer"]) <= MAX_LIMIT_BUFFER):
            return "schema_sell_when_limit_buffer_out_of_range"
        if not (0.0 <= float(value["max_hold_seconds"]) <= MAX_RULE_SECONDS):
            return "schema_sell_when_max_hold_seconds_out_of_range"
    elif field == "hold_when":
        reserve = float(value["reserve_cash"])
        if not (0.0 <= reserve <= duel.STARTING_EQUITY):
            return "schema_hold_when_reserve_cash_out_of_range"
    return None


def review_status(script: Dict[str, Any]) -> str:
    review = script.get("review") if isinstance(script.get("review"), dict) else {}
    return str(review.get("status") or "")


def script_tradable(script: Dict[str, Any]) -> bool:
    return (
        script.get("status") != "script_invalid"
        and not bool(script.get("hold_only"))
        and review_status(script) in TRADABLE_REVIEW_STATUSES
        and int(script.get("max_orders_per_tick") or 0) > 0
        and bool(script.get("allowed_sides"))
    )


def bot_script_schema_error(raw: Dict[str, Any], agent_id: Optional[str] = None) -> Optional[str]:
    unexpected = sorted(set(raw) - TOP_LEVEL_FIELDS)
    if unexpected:
        return f"schema_unknown_field:{unexpected[0]}"
    if "allowed_sides" in raw:
        sides = raw.get("allowed_sides")
        if not isinstance(sides, list):
            return "schema_allowed_sides_not_list"
        invalid_sides = [side for side in sides if not isinstance(side, str) or side not in {"buy", "sell"}]
        if invalid_sides:
            return "schema_allowed_sides_invalid"
        if len(sides) != len(set(sides)):
            return "schema_allowed_sides_duplicate"
        if not sides and raw.get("hold_only") is not True:
            return "schema_allowed_sides_empty_without_hold_only"
        if sides and raw.get("hold_only") is True:
            return "schema_allowed_sides_not_empty_for_hold_only"
    for field in ("buy_when", "sell_when", "hold_when"):
        if field in raw and not isinstance(raw.get(field), dict):
            return f"schema_{field}_not_object"
        if field in raw:
            nested_error = _nested_rule_schema_error(field, raw.get(field) or {})
            if nested_error:
                return nested_error
    for field in ("interval_sec", "min_interval_sec", "max_orders_per_tick"):
        if field in raw and not _schema_integer(raw.get(field)):
            return f"schema_{field}_not_integer"
    if "version" in raw and not _schema_integer(raw.get("version")):
        return "schema_version_not_integer"
    if "hold_only" in raw and not isinstance(raw.get("hold_only"), bool):
        return "schema_hold_only_not_bool"
    missing = sorted(TOP_LEVEL_REQUIRED_FIELDS - set(raw))
    if missing:
        return f"schema_missing_field:{missing[0]}"
    if int(raw.get("version")) != SCRIPT_VERSION:
        return "schema_version_unsupported"
    if agent_id is not None and str(raw.get("agent_id")) != str(agent_id):
        return "schema_agent_id_mismatch"
    for field in ("agent_id", "execution_mode", "generated_by", "status", "updated_at"):
        if not isinstance(raw.get(field), str) or not raw.get(field):
            return f"schema_{field}_not_string"
    if raw.get("execution_mode") != "mechanical_script":
        return "schema_execution_mode_unsupported"
    if "risk_reason" in raw and not isinstance(raw.get("risk_reason"), str):
        return "schema_risk_reason_not_string"
    interval = int(raw["interval_sec"])
    if not (HARD_MIN_INTERVAL_SEC <= interval <= MAX_INTERVAL_SEC):
        return "schema_interval_sec_out_of_range"
    minimum = int(raw["min_interval_sec"])
    if not (1 <= minimum <= HARD_MIN_INTERVAL_SEC):
        return "schema_min_interval_sec_out_of_range"
    max_orders = int(raw["max_orders_per_tick"])
    if not (0 <= max_orders <= MAX_ORDERS_PER_TICK):
        return "schema_max_orders_per_tick_out_of_range"
    if raw["hold_only"] and max_orders != 0:
        return "schema_hold_only_max_orders_nonzero"
    if not raw["hold_only"] and max_orders <= 0:
        return "schema_active_max_orders_zero"
    review = raw.get("review")
    if not isinstance(review, dict):
        return "schema_review_not_object"
    review_unexpected = sorted(set(review) - REVIEW_FIELDS)
    if review_unexpected:
        return f"schema_review_unknown_field:{review_unexpected[0]}"
    review_missing = sorted(REVIEW_FIELDS - set(review))
    if review_missing:
        return f"schema_review_missing_field:{review_missing[0]}"
    if review.get("status") not in REVIEW_STATUSES:
        return "schema_review_status_unsupported"
    for field in ("reviewed_by", "reviewed_at", "promotion_id"):
        if not isinstance(review.get(field), str):
            return f"schema_review_{field}_not_string"
    if not raw["hold_only"] and review.get("status") not in TRADABLE_REVIEW_STATUSES:
        return "schema_review_status_not_promoted"
    if not raw["hold_only"] and (not review.get("reviewed_by") or not review.get("reviewed_at")):
        return "schema_review_missing_reviewer"
    if review.get("status") == "promoted" and not review.get("promotion_id"):
        return "schema_review_missing_promotion_id"
    if raw["buy_when"].get("enabled") and "buy" not in raw["allowed_sides"]:
        return "schema_buy_enabled_without_buy_side"
    if raw["sell_when"].get("enabled") and "sell" not in raw["allowed_sides"]:
        return "schema_sell_enabled_without_sell_side"
    return None


def normalize_bot_script(raw: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    schema_error = bot_script_schema_error(raw, agent_id)
    if schema_error:
        return hold_only_safe_script(agent_id, "script_invalid:schema:" + schema_error)
    payload = {key: raw[key] for key in TOP_LEVEL_REQUIRED_FIELDS if key in raw}
    if "risk_reason" in raw:
        payload["risk_reason"] = raw["risk_reason"]
    script = json.loads(canonical_json(payload))
    script["version"] = int(script["version"])
    script["interval_sec"] = int(script["interval_sec"])
    script["min_interval_sec"] = int(script["min_interval_sec"])
    script["max_orders_per_tick"] = int(script["max_orders_per_tick"])
    for field in ("buy_when", "sell_when", "hold_when"):
        for key, value in list((script.get(field) or {}).items()):
            if _schema_number(value):
                script[field][key] = float(value)
    return script


def registry_hash_payload(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "registry_sha256"}


def script_body_hash_payload(script: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.loads(canonical_json(script))
    payload.pop("updated_at", None)
    payload.pop("generated_by", None)
    payload.pop("risk_reason", None)
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    payload["review"] = {"status": review.get("status", "")}
    return payload


def previous_registry_hash(data_dir: pathlib.Path) -> str:
    path = manifest_path(data_dir)
    if not path.exists():
        return ""
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    existing_hash = str(recorded.get("registry_sha256") or "")
    if existing_hash and sha256_json(registry_hash_payload(recorded)) == existing_hash:
        return existing_hash
    return ""


def bot_script_registry(data_dir: pathlib.Path) -> Dict[str, Any]:
    entries: Dict[str, Any] = {}
    errors: List[str] = []
    for agent_id in duel.AGENTS:
        path = script_path(data_dir, agent_id)
        if not path.exists():
            entries[agent_id] = {"exists": False, "ok": False, "error": "missing_script"}
            errors.append(f"missing_script:{agent_id}")
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("script_not_object")
            normalized = normalize_bot_script(raw, agent_id)
            script_hash = sha256_json(normalized)
            ok = normalized.get("status") != "script_invalid"
            if not ok:
                errors.append(f"script_invalid:{agent_id}:{normalized.get('risk_reason')}")
            entries[agent_id] = {
                "exists": True,
                "ok": bool(ok),
                "agent_id": agent_id,
                "status": normalized.get("status"),
                "review_status": review_status(normalized),
                "tradable": script_tradable(normalized),
                "hold_only": normalized.get("hold_only"),
                "version": normalized.get("version"),
                "updated_at": normalized.get("updated_at"),
                "sha256": script_hash,
                "script_body_sha256": sha256_json(script_body_hash_payload(normalized)),
                "risk_reason": normalized.get("risk_reason", ""),
            }
        except Exception as exc:
            entries[agent_id] = {"exists": True, "ok": False, "error": type(exc).__name__}
            errors.append(f"script_parse:{agent_id}:{type(exc).__name__}")
    manifest = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "ok": not errors,
        "ts": utc_now(),
        "previous_registry_sha256": previous_registry_hash(data_dir),
        "agents": entries,
        "errors": errors,
    }
    manifest["registry_sha256"] = sha256_json(registry_hash_payload(manifest))
    return manifest


def write_bot_registry_manifest(data_dir: pathlib.Path) -> pathlib.Path:
    manifest = bot_script_registry(data_dir)
    path = manifest_path(data_dir)
    atomic_write_json(path, manifest)
    return path


def verify_bot_registry_manifest(data_dir: pathlib.Path) -> Dict[str, Any]:
    path = manifest_path(data_dir)
    if not path.exists():
        return {"ok": False, "errors": ["missing_bot_registry_manifest"]}
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "errors": ["bot_registry_parse:" + type(exc).__name__]}
    current = bot_script_registry(data_dir)
    errors: List[str] = []
    if recorded.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        errors.append("bot_registry_schema_version_mismatch")
    recorded_hash = str(recorded.get("registry_sha256") or "")
    if not recorded_hash:
        errors.append("bot_registry_hash_missing")
    elif sha256_json(registry_hash_payload(recorded)) != recorded_hash:
        errors.append("bot_registry_hash_mismatch")
    if not recorded.get("ok"):
        errors.append("recorded_manifest_not_ok")
    if not current.get("ok"):
        errors.extend(current.get("errors", []))
    for agent_id in duel.AGENTS:
        old_entry = ((recorded.get("agents") or {}).get(agent_id) or {})
        new_entry = ((current.get("agents") or {}).get(agent_id) or {})
        old_hash = old_entry.get("sha256")
        new_hash = new_entry.get("sha256")
        if not old_hash or not new_hash or old_hash != new_hash:
            errors.append(f"bot_script_manifest_hash_mismatch:{agent_id}")
        for field in ("review_status", "tradable"):
            if old_entry.get(field) != new_entry.get(field):
                errors.append(f"bot_script_manifest_{field}_mismatch:{agent_id}")
    return {"ok": not errors, "errors": errors, "recorded": recorded, "current": current}


def load_bot_script(data_dir: pathlib.Path, agent_id: str, *, write_manifest: bool = True) -> Dict[str, Any]:
    ensure_default_bot_scripts(data_dir, write_manifest=write_manifest)
    path = script_path(data_dir, agent_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return normalize_bot_script(raw, agent_id)
        return hold_only_safe_script(agent_id, "script_invalid:not_object")
    except Exception as exc:
        return hold_only_safe_script(agent_id, "script_invalid:" + type(exc).__name__)


def write_bot_script(data_dir: pathlib.Path, agent_id: str, script: Dict[str, Any], source: str) -> pathlib.Path:
    ensure_default_bot_scripts(data_dir)
    base = load_bot_script(data_dir, agent_id)
    if base.get("status") == "script_invalid":
        base = default_bot_script(agent_id)
    candidate = merge_script_update(base, script)
    candidate["version"] = SCRIPT_VERSION
    candidate["agent_id"] = agent_id
    candidate["execution_mode"] = "mechanical_script"
    candidate["updated_at"] = utc_now()
    candidate["generated_by"] = source
    if candidate.get("hold_only") is False:
        review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
        if review.get("status") not in TRADABLE_REVIEW_STATUSES:
            candidate["review"] = reviewed_metadata(source)
    else:
        review = candidate.get("review") if isinstance(candidate.get("review"), dict) else {}
        if not review:
            candidate["review"] = draft_review_metadata()
    normalized = normalize_bot_script(candidate, agent_id)
    dest = script_path(data_dir, agent_id)
    if dest.exists():
        hist = history_dir(data_dir) / f"{utc_now().replace(':', '').replace('+00:00', 'Z')}_{agent_id}.json"
        atomic_write_text(hist, dest.read_text(encoding="utf-8"))
    atomic_write_json(dest, normalized)
    write_bot_registry_manifest(data_dir)
    return dest


def merge_script_update(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(canonical_json(base))
    for key, value in update.items():
        if key in {"buy_when", "sell_when", "hold_when", "review"} and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def script_from_superwing_rules(current: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    script = json.loads(canonical_json(current))
    buy_when = dict(script.get("buy_when", {}) or {})
    for src, dest in (
        ("selection", "selection"),
        ("price_min", "price_min"),
        ("price_max", "price_max"),
        ("min_volume", "min_volume"),
        ("max_notional", "max_notional"),
        ("limit_buffer", "limit_buffer"),
    ):
        if src in rules:
            buy_when[dest] = rules[src]
    script["buy_when"] = buy_when
    script["updated_at"] = utc_now()
    return script


def _market_by_id(markets: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(m.get("market_id")): m for m in markets}


def _outcome_price(market: Dict[str, Any], outcome_name: str) -> Optional[float]:
    for outcome in market.get("outcomes", []):
        if str(outcome.get("name", "")).lower() == outcome_name.lower():
            return float(outcome.get("price", 0.0))
    return None


def _position_age_seconds(pos: Dict[str, Any]) -> float:
    raw = str(pos.get("created_at") or "")
    if not raw:
        return 0.0
    try:
        created = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - created).total_seconds())
    except Exception:
        return 0.0


def _sell_orders(account: Dict[str, Any], markets: List[Dict[str, Any]], script: Dict[str, Any]) -> List[Dict[str, Any]]:
    if "sell" not in script.get("allowed_sides", []):
        return []
    market_lookup = _market_by_id(markets)
    sell_rules = script.get("sell_when", {}) or {}
    take_profit = float(sell_rules.get("take_profit_pct", 0.12))
    stop_loss = float(sell_rules.get("stop_loss_pct", 0.08))
    max_hold_seconds = float(sell_rules.get("max_hold_seconds", 0) or 0)
    exit_fraction = max(0.0, min(1.0, float(sell_rules.get("exit_fraction", 1.0))))
    limit_buffer = float(sell_rules.get("limit_buffer", 0.015))
    orders: List[Dict[str, Any]] = []
    for pos in account.get("positions", {}).values():
        market = market_lookup.get(str(pos.get("market_id")))
        if not market:
            continue
        price = _outcome_price(market, str(pos.get("outcome", "")))
        if price is None:
            continue
        avg = float(pos.get("avg_price", 0.0) or 0.0)
        reason = ""
        if avg > 0 and price >= avg * (1.0 + take_profit):
            reason = "take_profit"
        elif avg > 0 and price <= avg * (1.0 - stop_loss):
            reason = "stop_loss"
        elif max_hold_seconds > 0 and _position_age_seconds(pos) >= max_hold_seconds:
            reason = "max_hold"
        if not reason:
            continue
        shares = round(float(pos.get("shares", 0.0)) * exit_fraction, 6)
        if shares <= 0:
            continue
        orders.append(
            {
                "market_id": str(pos.get("market_id")),
                "outcome": str(pos.get("outcome")),
                "side": "sell",
                "shares": shares,
                "limit_price": round(max(0.01, price - limit_buffer), 4),
                "confidence": 0.5,
                "reason": reason,
                "rationale": f"Mechanical {reason}: current={price:.4f}, avg={avg:.4f}.",
            }
        )
    return orders


def _recent_market_trade_age_seconds(account: Dict[str, Any], market_id: str) -> Optional[float]:
    newest: Optional[dt.datetime] = None
    for trade in account.get("trades", []) or []:
        if str(trade.get("market_id")) != str(market_id):
            continue
        raw = str(trade.get("ts") or "")
        if not raw:
            continue
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        if newest is None or parsed > newest:
            newest = parsed
    if newest is None:
        return None
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - newest).total_seconds())


def _buy_orders(account: Dict[str, Any], markets: List[Dict[str, Any]], script: Dict[str, Any], remaining_slots: int) -> List[Dict[str, Any]]:
    if remaining_slots <= 0 or "buy" not in script.get("allowed_sides", []):
        return []
    buy_rules = script.get("buy_when", {}) or {}
    price_min = float(buy_rules.get("price_min", 0.25))
    price_max = float(buy_rules.get("price_max", 0.48))
    min_volume = float(buy_rules.get("min_volume", 0.0))
    max_notional = min(float(buy_rules.get("max_notional", 10.0)), duel.STARTING_EQUITY * duel.MAX_TRADE_FRACTION)
    limit_buffer = float(buy_rules.get("limit_buffer", 0.015))
    exposure_skip = float(buy_rules.get("skip_if_market_exposure_usdc_at_least", duel.STARTING_EQUITY * duel.MAX_MARKET_FRACTION))
    market_cooldown_seconds = max(0.0, float(buy_rules.get("market_cooldown_seconds", 300)))
    orders: List[Dict[str, Any]] = []
    for market in markets:
        market_id = str(market.get("market_id"))
        recent_age = _recent_market_trade_age_seconds(account, market_id)
        if recent_age is not None and recent_age < market_cooldown_seconds:
            continue
        current_exposure = duel.market_exposure(account, market_id)
        if current_exposure >= exposure_skip:
            continue
        if float(market.get("volume", 0.0) or 0.0) < min_volume:
            continue
        candidates = sorted(market.get("outcomes", []), key=lambda outcome: float(outcome.get("price", 1.0)))
        chosen = None
        for outcome in candidates:
            price = float(outcome.get("price", 0.0))
            if price_min <= price <= price_max:
                chosen = outcome
                break
        if not chosen:
            continue
        price = float(chosen.get("price", 0.0))
        expected_fill_price = min(0.99, price + duel.SLIPPAGE_BPS / 10000.0)
        expected_shares = max_notional / expected_fill_price if expected_fill_price else 0.0
        expected_fee, _, _ = duel.estimate_taker_fee(expected_shares, expected_fill_price, market)
        expected_gross_cost = max_notional + expected_fee
        if current_exposure + expected_gross_cost > exposure_skip:
            continue
        orders.append(
            {
                "market_id": market_id,
                "outcome": str(chosen.get("name")),
                "side": "buy",
                "notional": max_notional,
                "limit_price": round(min(0.99, price + limit_buffer), 4),
                "confidence": 0.51,
                "reason": "buy_rule_match",
                "rationale": str(buy_rules.get("selection", "Mechanical buy rule matched."))[:500],
            }
        )
        if len(orders) >= remaining_slots:
            break
    return orders


def mechanical_decision_for_agent(account: Dict[str, Any], markets: List[Dict[str, Any]], script: Dict[str, Any]) -> Dict[str, Any]:
    account_agent = str(account.get("agent_id") or "")
    script_agent = str(script.get("agent_id") or "")
    if account_agent and script_agent and account_agent != script_agent:
        return {
            "agent_id": account_agent,
            "source": "mechanical_script",
            "script_version": script.get("version"),
            "script_updated_at": script.get("updated_at"),
            "orders": [],
            "notes": f"hold-only script/account agent mismatch: account={account_agent}, script={script_agent}",
        }
    if script.get("hold_only"):
        return {
            "agent_id": str(script.get("agent_id") or account.get("agent_id")),
            "source": "mechanical_script",
            "script_version": script.get("version"),
            "script_updated_at": script.get("updated_at"),
            "orders": [],
            "notes": "hold-only script: " + str(script.get("status") or "hold_only"),
        }
    if not script_tradable(script):
        return {
            "agent_id": str(script.get("agent_id") or account.get("agent_id")),
            "source": "mechanical_script",
            "script_version": script.get("version"),
            "script_updated_at": script.get("updated_at"),
            "orders": [],
            "notes": "hold-only non-tradable script: review_status=" + review_status(script),
        }
    max_orders = int(script.get("max_orders_per_tick", 2))
    sell_orders = _sell_orders(account, markets, script)[:max_orders]
    if sell_orders:
        orders = sell_orders
    else:
        orders = _buy_orders(account, markets, script, max_orders)
    return {
        "agent_id": str(script.get("agent_id") or account.get("agent_id")),
        "source": "mechanical_script",
        "script_version": script.get("version"),
        "script_updated_at": script.get("updated_at"),
        "orders": orders,
        "notes": "mechanical script executed; no per-tick LLM call",
    }
