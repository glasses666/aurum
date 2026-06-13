#!/usr/bin/env python3
"""Aurum DeepSeek/SuperWing paper-wallet duel.

Local-only paper trading harness over public Polymarket market data.
No wallet connection, no private keys, no live orders.

The DeepSeek lane calls DeepSeek's OpenAI-compatible chat-completions API when
DEEPSEEK_API_KEY is present. The SuperWing lane is a deterministic baseline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit={limit}"

STARTING_EQUITY = 1500.0
RESERVE_CASH = 300.0
MAX_TRADE_FRACTION = 0.03
MAX_MARKET_FRACTION = 0.08
MAX_TOTAL_RISK_FRACTION = 0.35
SLIPPAGE_BPS = 50  # 0.50 percentage points in probability terms.

AGENTS = ("superwing", "deepseek")


class DuelError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ensure_data_dir(data_dir: pathlib.Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)


def state_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "state.json"


def decisions_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "decisions.jsonl"


def events_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "events.jsonl"


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def new_account(agent_id: str) -> Dict[str, Any]:
    return {
        "agent_id": agent_id,
        "mode": "paper",
        "quote_currency": "paper_usdc",
        "starting_equity": STARTING_EQUITY,
        "reserve_cash": RESERVE_CASH,
        "cash": STARTING_EQUITY,
        "positions": {},
        "trades": [],
        "risk_events": [],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def init_state(data_dir: pathlib.Path, reset: bool = False) -> Dict[str, Any]:
    ensure_data_dir(data_dir)
    p = state_path(data_dir)
    if p.exists() and not reset:
        return load_state(data_dir)
    state = {
        "version": 1,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "risk": {
            "max_trade_fraction": MAX_TRADE_FRACTION,
            "max_market_fraction": MAX_MARKET_FRACTION,
            "max_total_risk_fraction": MAX_TOTAL_RISK_FRACTION,
            "slippage_bps": SLIPPAGE_BPS,
        },
        "accounts": {agent: new_account(agent) for agent in AGENTS},
        "last_markets": [],
    }
    save_state(data_dir, state)
    append_jsonl(events_path(data_dir), {"ts": utc_now(), "event": "init", "reset": reset})
    return state


def load_state(data_dir: pathlib.Path) -> Dict[str, Any]:
    p = state_path(data_dir)
    if not p.exists():
        raise DuelError(f"state does not exist; run init first: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(data_dir: pathlib.Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    tmp = state_path(data_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(state_path(data_dir))


def load_env_file(env_file: Optional[pathlib.Path]) -> None:
    if not env_file or not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int, low: int = 0, high: Optional[int] = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except Exception:
        value = default
    value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def env_float(name: str, default: float, low: float = 0.0, high: Optional[float] = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)).strip())
    except Exception:
        value = default
    value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def deepseek_controls(cli_max_orders: int) -> Dict[str, Any]:
    """Operator-controlled DeepSeek safety knobs.

    DeepSeek is allowed to propose paper orders when the API key exists, but
    applying those proposals to the paper wallet requires an explicit two-key
    local env gate. The model cannot alter these values from its response.
    """
    hard_notional_cap = STARTING_EQUITY * MAX_TRADE_FRACTION
    configured_max_orders = env_int("AURUM_DEEPSEEK_MAX_ORDERS", cli_max_orders, low=0, high=cli_max_orders)
    configured_max_notional = env_float("AURUM_DEEPSEEK_MAX_NOTIONAL", hard_notional_cap, low=0.0, high=hard_notional_cap)
    return {
        "allow_paper_apply": env_bool("AURUM_DEEPSEEK_ALLOW_PAPER_APPLY", False),
        "operator_confirm": os.environ.get("AURUM_DEEPSEEK_OPERATOR_CONFIRM", "").strip(),
        "max_orders": configured_max_orders,
        "max_notional_per_order": configured_max_notional,
        "temperature": env_float("AURUM_DEEPSEEK_TEMPERATURE", 0.2, low=0.0, high=1.0),
    }


def require_deepseek_apply_authorized(controls: Dict[str, Any]) -> None:
    if controls.get("allow_paper_apply") and controls.get("operator_confirm") == "ALLOW_DEEPSEEK_PAPER_APPLY":
        return
    raise DuelError(
        "DeepSeek paper apply is locked; run with --no-apply for review, or set "
        "AURUM_DEEPSEEK_ALLOW_PAPER_APPLY=true and "
        "AURUM_DEEPSEEK_OPERATOR_CONFIRM=ALLOW_DEEPSEEK_PAPER_APPLY in the local env before uploading."
    )


def jsonish(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            return json.loads(s)
        except Exception:
            return [s]
    return value


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def no_proxy_opener(allow_proxy: bool) -> urllib.request.OpenerDirector:
    if allow_proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch_json(url: str, timeout: float = 12.0, allow_proxy: bool = False) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "aurum-agent-duel/0.1 (+paper-only)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    opener = no_proxy_opener(allow_proxy)
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_market(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    outcomes = jsonish(raw.get("outcomes")) or []
    prices = jsonish(raw.get("outcomePrices")) or []
    token_ids = jsonish(raw.get("clobTokenIds")) or []
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) < 2 or len(prices) < 2:
        return None
    norm_outcomes = []
    for idx, name in enumerate(outcomes[:2]):
        price = to_float(prices[idx] if idx < len(prices) else None, -1.0)
        if price <= 0.0 or price >= 1.0:
            return None
        norm_outcomes.append(
            {
                "name": str(name),
                "price": round(price, 4),
                "token_id": str(token_ids[idx]) if idx < len(token_ids) else "",
            }
        )
    market_id = str(raw.get("id") or raw.get("conditionId") or raw.get("market_slug") or raw.get("slug") or "")
    question = str(raw.get("question") or raw.get("title") or raw.get("slug") or "").strip()
    if not market_id or not question:
        return None
    return {
        "market_id": market_id,
        "question": question[:280],
        "slug": raw.get("slug") or raw.get("market_slug") or "",
        "volume": to_float(raw.get("volume") or raw.get("volumeNum") or raw.get("volume24hr") or raw.get("volume24hrClob"), 0.0),
        "liquidity": to_float(raw.get("liquidity") or raw.get("liquidityNum"), 0.0),
        "end_date": raw.get("endDate") or raw.get("end_date") or raw.get("endDateIso") or "",
        "outcomes": norm_outcomes,
    }


def mock_markets() -> List[Dict[str, Any]]:
    return [
        {
            "market_id": "mock-election-1",
            "question": "Mock: Will Candidate A win the election?",
            "slug": "mock-election-1",
            "volume": 250000.0,
            "liquidity": 50000.0,
            "end_date": "2026-12-31T00:00:00Z",
            "outcomes": [{"name": "Yes", "price": 0.57, "token_id": "mock-yes"}, {"name": "No", "price": 0.43, "token_id": "mock-no"}],
        },
        {
            "market_id": "mock-fed-1",
            "question": "Mock: Will the Fed cut rates by September?",
            "slug": "mock-fed-1",
            "volume": 140000.0,
            "liquidity": 22000.0,
            "end_date": "2026-09-30T00:00:00Z",
            "outcomes": [{"name": "Yes", "price": 0.34, "token_id": "mock-fed-yes"}, {"name": "No", "price": 0.66, "token_id": "mock-fed-no"}],
        },
        {
            "market_id": "mock-crypto-1",
            "question": "Mock: Will BTC close above 150k this year?",
            "slug": "mock-crypto-1",
            "volume": 180000.0,
            "liquidity": 35000.0,
            "end_date": "2026-12-31T00:00:00Z",
            "outcomes": [{"name": "Yes", "price": 0.22, "token_id": "mock-btc-yes"}, {"name": "No", "price": 0.78, "token_id": "mock-btc-no"}],
        },
    ]


def fetch_markets(limit: int, min_volume: float, mock: bool, allow_proxy: bool) -> List[Dict[str, Any]]:
    if mock:
        markets = mock_markets()
    else:
        raw = fetch_json(GAMMA_MARKETS_URL.format(limit=max(limit * 3, limit)), allow_proxy=allow_proxy)
        if isinstance(raw, dict) and "markets" in raw:
            raw_items = raw.get("markets") or []
        elif isinstance(raw, list):
            raw_items = raw
        else:
            raw_items = []
        markets = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            m = normalize_market(item)
            if m:
                markets.append(m)
    markets = [m for m in markets if m.get("volume", 0.0) >= min_volume]
    markets.sort(key=lambda m: (m.get("volume", 0.0), m.get("liquidity", 0.0)), reverse=True)
    return markets[:limit]


def market_price_map(markets: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], float]:
    out = {}
    for m in markets:
        for outcome in m.get("outcomes", []):
            out[(m["market_id"], outcome["name"].lower())] = float(outcome["price"])
    return out


def account_exposure(account: Dict[str, Any]) -> float:
    total = 0.0
    for pos in account.get("positions", {}).values():
        total += max(0.0, float(pos.get("cost_basis", 0.0)))
    return total


def market_exposure(account: Dict[str, Any], market_id: str) -> float:
    total = 0.0
    for pos in account.get("positions", {}).values():
        if pos.get("market_id") == market_id:
            total += max(0.0, float(pos.get("cost_basis", 0.0)))
    return total


def portfolio_value(account: Dict[str, Any], prices: Dict[Tuple[str, str], float]) -> Dict[str, Any]:
    cash = float(account.get("cash", 0.0))
    pos_value = 0.0
    details = []
    for key, pos in account.get("positions", {}).items():
        market_id = str(pos.get("market_id"))
        outcome = str(pos.get("outcome", "")).lower()
        shares = float(pos.get("shares", 0.0))
        mark = prices.get((market_id, outcome), float(pos.get("last_price", pos.get("avg_price", 0.0))))
        value = shares * mark
        pos_value += value
        details.append({"key": key, "shares": shares, "mark": mark, "value": value, "cost_basis": pos.get("cost_basis", 0.0)})
    starting = float(account.get("starting_equity", STARTING_EQUITY))
    total = cash + pos_value
    roi = (total - starting) / starting if starting else 0.0
    return {
        "agent_id": account.get("agent_id"),
        "cash": round(cash, 4),
        "position_value": round(pos_value, 4),
        "portfolio_value": round(total, 4),
        "starting_equity": round(starting, 4),
        "roi": round(roi, 6),
        "score": round(roi * 100.0, 4),
        "open_positions": len(account.get("positions", {})),
        "details": details,
    }


def compact_account(account: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agent_id": account.get("agent_id"),
        "cash": round(float(account.get("cash", 0.0)), 4),
        "starting_equity": account.get("starting_equity"),
        "reserve_cash": account.get("reserve_cash"),
        "open_positions": len(account.get("positions", {})),
        "open_risk_cost_basis": round(account_exposure(account), 4),
    }


def superwing_decision(account: Dict[str, Any], markets: List[Dict[str, Any]], max_orders: int = 2) -> Dict[str, Any]:
    """Conservative deterministic baseline.

    Looks for liquid binary markets with a cheaper outcome in the 0.25-0.48 band.
    It is intentionally boring: small notional, explicit limit, no forced trade.
    """
    orders = []
    max_notional = min(30.0, STARTING_EQUITY * MAX_TRADE_FRACTION)
    for m in markets:
        candidates = sorted(m["outcomes"], key=lambda o: o["price"])
        chosen = None
        for outcome in candidates:
            p = float(outcome["price"])
            if 0.25 <= p <= 0.48:
                chosen = outcome
                break
        if not chosen:
            continue
        orders.append(
            {
                "market_id": m["market_id"],
                "outcome": chosen["name"],
                "side": "buy",
                "notional": max_notional,
                "limit_price": round(min(0.99, float(chosen["price"]) + 0.015), 4),
                "confidence": 0.51,
                "rationale": "Liquid underdog/near-even paper probe; small capped risk.",
            }
        )
        if len(orders) >= max_orders:
            break
    return {
        "agent_id": "superwing",
        "orders": orders,
        "notes": "Conservative baseline: small capped buys only; hold if no liquid underdog band exists.",
    }


def deepseek_prompt(account: Dict[str, Any], markets: List[Dict[str, Any]], controls: Dict[str, Any]) -> Tuple[str, str]:
    system = (
        "You are DeepSeek running a PAPER-ONLY Polymarket strategy test for Aurum. "
        "You cannot place real orders. You cannot request wallets, private keys, USDC, account logins, or geoblock bypass. "
        f"You must choose at most {controls['max_orders']} small buy orders or hold. "
        "Operator controls are enforced by the runner and cannot be overridden by your response. "
        "Output JSON only, no markdown."
    )
    market_slate = []
    for m in markets:
        market_slate.append(
            {
                "market_id": m["market_id"],
                "question": m["question"],
                "volume": m.get("volume", 0),
                "liquidity": m.get("liquidity", 0),
                "end_date": m.get("end_date", ""),
                "outcomes": [{"name": o["name"], "price": o["price"]} for o in m.get("outcomes", [])],
            }
        )
    user = json.dumps(
        {
            "task": "Pick paper trades for the deepseek account. If edge is unclear, return no orders.",
            "account": compact_account(account),
            "risk_limits": {
                "max_orders": controls["max_orders"],
                "max_notional_per_order": round(float(controls["max_notional_per_order"]), 2),
                "reserve_cash": RESERVE_CASH,
                "allowed_side": "buy only",
                "limit_price_required": True,
            },
            "markets": market_slate,
            "required_output_schema": {
                "agent_id": "deepseek",
                "orders": [
                    {
                        "market_id": "one of the provided market_id values",
                        "outcome": "one listed outcome name",
                        "side": "buy",
                        "notional": "number <= max_notional_per_order",
                        "limit_price": "number between 0.01 and 0.99",
                        "confidence": "number 0-1",
                        "rationale": "one concise sentence",
                    }
                ],
                "notes": "brief explanation",
            },
        },
        ensure_ascii=False,
    )
    return system, user


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise DuelError("DeepSeek response did not contain a JSON object")


def deepseek_decision(account: Dict[str, Any], markets: List[Dict[str, Any]], controls: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DuelError("DEEPSEEK_API_KEY is not set; cannot ask DeepSeek to self-decide")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    endpoint = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions").strip()
    if endpoint.rstrip("/").endswith("/v1"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"
    system, user = deepseek_prompt(account, markets, controls)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": controls["temperature"],
        "max_tokens": 900,
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": "aurum-agent-duel/0.1 (+paper-only)",
        },
        method="POST",
    )
    opener = no_proxy_opener(False)
    try:
        with opener.open(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(800).decode("utf-8", "replace") if exc.fp else ""
        raise DuelError(f"DeepSeek HTTP {exc.code}: {detail[:400]}") from exc
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    decision = extract_json_object(content)
    decision.setdefault("agent_id", "deepseek")
    decision.setdefault("orders", [])
    decision.setdefault("notes", "")
    decision["raw_model"] = model
    return decision


def validate_and_apply(
    data_dir: pathlib.Path,
    state: Dict[str, Any],
    agent_id: str,
    decision: Dict[str, Any],
    markets: List[Dict[str, Any]],
    apply: bool,
    max_orders: int,
    max_notional_per_order: float,
) -> Dict[str, Any]:
    if agent_id not in state["accounts"]:
        raise DuelError(f"unknown agent: {agent_id}")
    account = state["accounts"][agent_id]
    market_by_id = {m["market_id"]: m for m in markets}
    fills = []
    rejections = []
    decision_record = {
        "ts": utc_now(),
        "agent_id": agent_id,
        "decision": decision,
        "applied": bool(apply),
        "controls": {
            "max_orders": max_orders,
            "max_notional_per_order": round(max_notional_per_order, 4),
        },
    }
    append_jsonl(decisions_path(data_dir), decision_record)
    orders = decision.get("orders", []) or []
    if not isinstance(orders, list):
        orders = []
    for idx, order in enumerate(orders):
        if idx >= max_orders:
            rejections.append({
                "ts": utc_now(),
                "agent_id": agent_id,
                "order": order,
                "reason": f"order count exceeds configured max_orders={max_orders}",
            })
            continue
        try:
            market_id = str(order.get("market_id", ""))
            market = market_by_id.get(market_id)
            if not market:
                raise DuelError("unknown market_id")
            side = str(order.get("side", "")).lower()
            if side != "buy":
                raise DuelError("only buy side is allowed in Phase 0")
            requested_notional = to_float(order.get("notional"), 0.0)
            if requested_notional > max_notional_per_order:
                raise DuelError(f"notional {requested_notional:.4f} exceeds configured cap {max_notional_per_order:.4f}")
            notional = requested_notional
            if notional <= 0:
                raise DuelError("notional must be positive")
            if float(account.get("cash", 0.0)) - notional < RESERVE_CASH:
                raise DuelError("reserve cash would be breached")
            if account_exposure(account) + notional > STARTING_EQUITY * MAX_TOTAL_RISK_FRACTION:
                raise DuelError("total open-risk cap would be breached")
            if market_exposure(account, market_id) + notional > STARTING_EQUITY * MAX_MARKET_FRACTION:
                raise DuelError("per-market exposure cap would be breached")
            outcome_name = str(order.get("outcome", ""))
            outcome = None
            for o in market.get("outcomes", []):
                if str(o["name"]).lower() == outcome_name.lower():
                    outcome = o
                    break
            if not outcome:
                raise DuelError("unknown outcome")
            observed_price = float(outcome["price"])
            fill_price = min(0.99, observed_price + SLIPPAGE_BPS / 10000.0)
            limit_price = to_float(order.get("limit_price"), 0.0)
            if not (0.01 <= limit_price <= 0.99):
                raise DuelError("valid limit_price is required")
            if fill_price > limit_price:
                raise DuelError(f"fill_price {fill_price:.4f} exceeds limit_price {limit_price:.4f}")
            shares = notional / fill_price
            pos_key = market_id + "::" + str(outcome["name"])
            fill = {
                "ts": utc_now(),
                "agent_id": agent_id,
                "market_id": market_id,
                "question": market["question"],
                "outcome": outcome["name"],
                "side": "buy",
                "notional": round(notional, 4),
                "observed_price": round(observed_price, 4),
                "fill_price": round(fill_price, 4),
                "shares": round(shares, 6),
                "rationale": str(order.get("rationale", ""))[:500],
            }
            fills.append(fill)
            if apply:
                existing = account["positions"].get(pos_key)
                if existing:
                    old_shares = float(existing.get("shares", 0.0))
                    old_cost = float(existing.get("cost_basis", 0.0))
                    new_shares = old_shares + shares
                    new_cost = old_cost + notional
                    existing.update(
                        {
                            "shares": new_shares,
                            "cost_basis": new_cost,
                            "avg_price": new_cost / new_shares if new_shares else fill_price,
                            "last_price": observed_price,
                            "updated_at": utc_now(),
                        }
                    )
                else:
                    account["positions"][pos_key] = {
                        "market_id": market_id,
                        "question": market["question"],
                        "outcome": outcome["name"],
                        "shares": shares,
                        "cost_basis": notional,
                        "avg_price": fill_price,
                        "last_price": observed_price,
                        "created_at": utc_now(),
                        "updated_at": utc_now(),
                    }
                account["cash"] = float(account.get("cash", 0.0)) - notional
                account.setdefault("trades", []).append(fill)
                account["updated_at"] = utc_now()
        except Exception as exc:
            rejection = {"ts": utc_now(), "agent_id": agent_id, "order": order, "reason": str(exc)}
            rejections.append(rejection)
            if apply:
                account.setdefault("risk_events", []).append(rejection)
    state["last_markets"] = markets
    if apply:
        save_state(data_dir, state)
    event = {"ts": utc_now(), "event": "decision_result", "agent_id": agent_id, "fills": fills, "rejections": rejections, "applied": bool(apply)}
    append_jsonl(events_path(data_dir), event)
    return event


def command_init(args: argparse.Namespace) -> None:
    state = init_state(pathlib.Path(args.data_dir), reset=args.reset)
    print(json.dumps({"ok": True, "state_path": str(state_path(pathlib.Path(args.data_dir))), "accounts": list(state["accounts"].keys())}, indent=2))


def command_markets(args: argparse.Namespace) -> None:
    markets = fetch_markets(args.limit, args.min_volume, args.mock_markets, args.allow_proxy)
    print(json.dumps({"count": len(markets), "markets": markets}, ensure_ascii=False, indent=2))


def command_score(args: argparse.Namespace) -> None:
    data_dir = pathlib.Path(args.data_dir)
    state = load_state(data_dir)
    markets = fetch_markets(args.limit, args.min_volume, args.mock_markets, args.allow_proxy) if args.refresh else state.get("last_markets", [])
    prices = market_price_map(markets)
    rows = [portfolio_value(state["accounts"][agent], prices) for agent in AGENTS]
    rows.sort(key=lambda r: r["score"], reverse=True)
    print(json.dumps({"ts": utc_now(), "scores": rows}, ensure_ascii=False, indent=2))


def command_decide(args: argparse.Namespace) -> None:
    data_dir = pathlib.Path(args.data_dir)
    load_env_file(pathlib.Path(args.env_file) if args.env_file else None)
    state = init_state(data_dir, reset=False)
    markets = fetch_markets(args.limit, args.min_volume, args.mock_markets, args.allow_proxy)
    validation_max_orders = args.max_orders
    validation_max_notional = STARTING_EQUITY * MAX_TRADE_FRACTION
    if args.agent == "superwing":
        decision = superwing_decision(state["accounts"]["superwing"], markets, max_orders=args.max_orders)
    elif args.agent == "deepseek":
        controls = deepseek_controls(args.max_orders)
        validation_max_orders = int(controls["max_orders"])
        validation_max_notional = float(controls["max_notional_per_order"])
        if not args.no_apply:
            require_deepseek_apply_authorized(controls)
        decision = deepseek_decision(state["accounts"]["deepseek"], markets, controls)
    else:
        raise DuelError(f"unknown agent: {args.agent}")
    result = validate_and_apply(
        data_dir,
        state,
        args.agent,
        decision,
        markets,
        apply=not args.no_apply,
        max_orders=validation_max_orders,
        max_notional_per_order=validation_max_notional,
    )
    print(json.dumps({"decision": decision, "result": result}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aurum paper-wallet duel runner")
    parser.add_argument("--data-dir", default="data/paper_duel", help="runtime data directory")
    parser.add_argument("--env-file", default=".env", help="optional env file for DeepSeek config")
    parser.add_argument("--mock-markets", action="store_true", help="use built-in mock markets")
    parser.add_argument("--allow-proxy", action="store_true", help="allow HTTP proxy env vars; default bypasses proxies")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--min-volume", type=float, default=0.0)
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("--reset", action="store_true")
    p_init.set_defaults(func=command_init)
    p_markets = sub.add_parser("markets")
    p_markets.set_defaults(func=command_markets)
    p_score = sub.add_parser("score")
    p_score.add_argument("--refresh", action="store_true", help="fetch fresh markets before scoring")
    p_score.set_defaults(func=command_score)
    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--agent", choices=AGENTS, required=True)
    p_decide.add_argument("--max-orders", type=int, default=2)
    p_decide.add_argument("--no-apply", action="store_true", help="log decision but do not change wallet state")
    p_decide.set_defaults(func=command_decide)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
