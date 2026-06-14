#!/usr/bin/env python3
"""Mechanical bot scripts for the Aurum paper duel.

This is the bot layer Queen Glasser asked for: agents may write/review these
versioned scripts, while the hot-path bot loop executes them mechanically without
asking an LLM on every tick.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any, Dict, List, Optional

import agent_duel as duel
import strategy_rules

SCRIPT_VERSION = 1
DEFAULT_INTERVAL_SEC = 15
HARD_MIN_INTERVAL_SEC = 5


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


def default_bot_script(agent_id: str) -> Dict[str, Any]:
    base_rules = dict(strategy_rules.DEFAULT_SUPERWING_RULES)
    max_notional = 30.0 if agent_id == "superwing" else 10.0
    return {
        "version": SCRIPT_VERSION,
        "agent_id": agent_id,
        "execution_mode": "mechanical_script",
        "generated_by": "default_agent_layer",
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
        },
        "sell_when": {
            "take_profit_pct": 0.12,
            "stop_loss_pct": 0.08,
            "max_hold_seconds": 0,
            "exit_fraction": 1.0,
            "limit_buffer": 0.015,
        },
        "hold_when": {
            "reserve_cash": duel.RESERVE_CASH,
            "respect_runner_risk_caps": True,
            "no_trade_if_no_matching_rule": True,
        },
    }


def ensure_default_bot_scripts(data_dir: pathlib.Path) -> None:
    current_dir(data_dir).mkdir(parents=True, exist_ok=True)
    history_dir(data_dir).mkdir(parents=True, exist_ok=True)
    for agent_id in duel.AGENTS:
        path = script_path(data_dir, agent_id)
        if not path.exists():
            path.write_text(json.dumps(default_bot_script(agent_id), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_bot_script(raw: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    script = default_bot_script(agent_id)
    script.update({k: v for k, v in raw.items() if k in script})
    script["agent_id"] = agent_id
    script["execution_mode"] = "mechanical_script"
    script["allowed_sides"] = [side for side in script.get("allowed_sides", []) if side in {"buy", "sell"}] or ["buy", "sell"]
    script["interval_sec"] = int(max(HARD_MIN_INTERVAL_SEC, min(3600, int(script.get("interval_sec", DEFAULT_INTERVAL_SEC)))))
    script["min_interval_sec"] = int(max(1, min(HARD_MIN_INTERVAL_SEC, int(script.get("min_interval_sec", HARD_MIN_INTERVAL_SEC)))))
    script["max_orders_per_tick"] = int(max(0, min(8, int(script.get("max_orders_per_tick", 2)))))
    return script


def load_bot_script(data_dir: pathlib.Path, agent_id: str) -> Dict[str, Any]:
    ensure_default_bot_scripts(data_dir)
    path = script_path(data_dir, agent_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return normalize_bot_script(raw, agent_id)
    except Exception:
        pass
    return default_bot_script(agent_id)


def write_bot_script(data_dir: pathlib.Path, agent_id: str, script: Dict[str, Any], source: str) -> pathlib.Path:
    ensure_default_bot_scripts(data_dir)
    normalized = normalize_bot_script({**script, "updated_at": utc_now(), "generated_by": source}, agent_id)
    dest = script_path(data_dir, agent_id)
    if dest.exists():
        hist = history_dir(data_dir) / f"{utc_now().replace(':', '').replace('+00:00', 'Z')}_{agent_id}.json"
        hist.write_text(dest.read_text(encoding="utf-8"), encoding="utf-8")
    dest.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


def script_from_superwing_rules(current: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    script = dict(current)
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
