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
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import strategy_rules

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit={limit}"
GAMMA_SEARCH_URL = "https://gamma-api.polymarket.com/public-search?q={query}"

STARTING_EQUITY = 1500.0
RESERVE_CASH = 300.0
MAX_TRADE_FRACTION = 0.03
MAX_MARKET_FRACTION = 0.08
MAX_TOTAL_RISK_FRACTION = 0.35
SLIPPAGE_BPS = 50  # 0.50 percentage points in probability terms.
POLYMARKET_FEE_DOC_URL = "https://docs.polymarket.com/trading/fees"
FEE_PRECISION_PLACES = 5
MIN_PAPER_ORDER_USDC = 5.0
FEE_CHURN_FREE_FRACTION = 0.002
DRAWDOWN_FREE_FRACTION = 0.02
YOLO_EXPOSURE_FREE_FRACTION = MAX_TOTAL_RISK_FRACTION * 0.50
VICTORY_ROI_THRESHOLD = 0.05
TAKER_FEE_RATES = {
    "crypto": 0.07,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "general": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
}

AGENTS = ("superwing", "deepseek")
BITCOIN_TERMS = ("bitcoin", "btc", "satoshi")


class DuelError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def ensure_data_dir(data_dir: pathlib.Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)


def state_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "state.json"


def decisions_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "decisions.jsonl"


def events_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "events.jsonl"


def risk_ledger_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "risk_ledger.jsonl"


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def append_risk_ledger(data_dir: pathlib.Path, record: Dict[str, Any]) -> None:
    append_jsonl(risk_ledger_path(data_dir), record)


def account_digest(account: Dict[str, Any]) -> Dict[str, Any]:
    positions = account.get("positions", {}) if isinstance(account.get("positions"), dict) else {}
    trades = account.get("trades", []) if isinstance(account.get("trades"), list) else []
    risk_events = account.get("risk_events", []) if isinstance(account.get("risk_events"), list) else []
    return {
        "cash": round(float(account.get("cash", 0.0) or 0.0), 5),
        "positions": len(positions),
        "trades": len(trades),
        "risk_events": len(risk_events),
        "state_hash": sha256_json(
            {
                "cash": round(float(account.get("cash", 0.0) or 0.0), 8),
                "positions": positions,
                "trades": trades,
                "risk_events": risk_events,
            }
        ),
    }


def risk_ledger_context(execution_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(execution_context, dict):
        return {}
    recorder_public = execution_context.get("recorder_public")
    if not recorder_public and isinstance(execution_context.get("recorder"), dict):
        recorder = execution_context["recorder"]
        recorder_public = {
            "source": recorder.get("source"),
            "capture_id": recorder.get("capture_id"),
            "ts": recorder.get("ts"),
            "manifest": recorder.get("manifest"),
            "orderable_feed_sha256": recorder.get("orderable_feed_sha256"),
            "book_coverage": recorder.get("book_coverage"),
            "orderable_market_count": recorder.get("orderable_market_count"),
            "source_refs": recorder.get("source_refs"),
        }
    return {
        "source": execution_context.get("source"),
        "recorder": recorder_public or {},
        "bot_script_hash": execution_context.get("bot_script_hash"),
        "bot_script_status": execution_context.get("bot_script_status"),
    }


def recorded_book_walk(
    execution_context: Optional[Dict[str, Any]],
    *,
    market: Dict[str, Any],
    outcome_name: str,
    side: str,
    limit_price: float,
    notional: float = 0.0,
    shares: float = 0.0,
) -> Optional[Dict[str, Any]]:
    if not isinstance(execution_context, dict) or not isinstance(execution_context.get("recorder"), dict):
        return None
    # Imported lazily to keep agent_duel importable by recorder_replay.
    import recorder_replay

    return recorder_replay.book_walk_fill(
        market=market,
        outcome_name=outcome_name,
        side=side,
        limit_price=limit_price,
        notional=notional,
        shares=shares,
        context=execution_context["recorder"],
    )


def state_runtime_risk() -> Dict[str, Any]:
    rules = paper_execution_rules()
    return {
        "max_trade_fraction": MAX_TRADE_FRACTION,
        "max_market_fraction": MAX_MARKET_FRACTION,
        "max_total_risk_fraction": MAX_TOTAL_RISK_FRACTION,
        "slippage_bps": SLIPPAGE_BPS,
        "paper_role": rules["paper_fill_role"],
        "min_order_usdc": MIN_PAPER_ORDER_USDC,
        "fee_formula": rules["taker_fee_formula"],
        "fee_precision_places": FEE_PRECISION_PLACES,
        "taker_fee_rates": TAKER_FEE_RATES,
        "fee_source": POLYMARKET_FEE_DOC_URL,
    }


def new_account(agent_id: str, *, now_fn: Callable[[], str] = utc_now) -> Dict[str, Any]:
    now = now_fn()
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
        "created_at": now,
        "updated_at": now,
    }


def init_state(data_dir: pathlib.Path, reset: bool = False, *, now_fn: Callable[[], str] = utc_now) -> Dict[str, Any]:
    ensure_data_dir(data_dir)
    strategy_rules.ensure_default_rules(data_dir)
    p = state_path(data_dir)
    if p.exists() and not reset:
        return load_state(data_dir)
    now = now_fn()
    state = {
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "risk": state_runtime_risk(),
        "accounts": {agent: new_account(agent, now_fn=now_fn) for agent in AGENTS},
        "last_markets": [],
    }
    save_state(data_dir, state, now_fn=now_fn)
    append_jsonl(events_path(data_dir), {"ts": now_fn(), "event": "init", "reset": reset})
    return state


def load_state(data_dir: pathlib.Path) -> Dict[str, Any]:
    p = state_path(data_dir)
    if not p.exists():
        raise DuelError(f"state does not exist; run init first: {p}")
    state = json.loads(p.read_text(encoding="utf-8"))
    state["risk"] = state_runtime_risk()
    return state


def save_state(data_dir: pathlib.Path, state: Dict[str, Any], *, now_fn: Callable[[], str] = utc_now) -> None:
    state["updated_at"] = now_fn()
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
        "thinking": os.environ.get("AURUM_DEEPSEEK_THINKING", "disabled").strip().lower() or "disabled",
        "reasoning_effort": os.environ.get("AURUM_DEEPSEEK_REASONING_EFFORT", "high").strip().lower() or "high",
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


def optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def fee_category_for_market(market: Dict[str, Any]) -> str:
    """Classify a market into the Polymarket public fee categories.

    The official trading-fees page states fees are market/category-specific and
    markets may expose feesEnabled. Gamma/search records do not always carry the
    exact CLOB fee descriptor, so this paper engine uses explicit market flags
    when present and otherwise maps visible category/text conservatively.
    """
    if optional_bool(market.get("fees_enabled")) is False:
        return "disabled"
    text = " ".join(
        str(market.get(k, ""))
        for k in ("fee_category", "category", "question", "slug", "description", "market_id", "condition_id")
    ).lower()
    if any(term in text for term in BITCOIN_TERMS) or "crypto" in text or "ethereum" in text or "solana" in text:
        return "crypto"
    if any(term in text for term in ("sport", "nba", "nfl", "mlb", "nhl", "soccer", "tennis", "ufc")):
        return "sports"
    if any(term in text for term in ("geopolitic", "world event", "war", "ceasefire", "ukraine", "russia", "israel", "iran")):
        return "geopolitics"
    if any(term in text for term in ("finance", "stock", "equity", "fed", "rate", "treasury")):
        return "finance"
    if "politic" in text or "election" in text:
        return "politics"
    if "econom" in text or "inflation" in text or "gdp" in text:
        return "economics"
    if "weather" in text or "temperature" in text or "hurricane" in text:
        return "weather"
    if "mention" in text:
        return "mentions"
    if "tech" in text or "artificial intelligence" in text or "software" in text:
        return "tech"
    if "culture" in text or "music" in text or "movie" in text or "celebrity" in text:
        return "culture"
    return "other"


def taker_fee_rate_for_market(market: Dict[str, Any]) -> float:
    if optional_bool(market.get("fees_enabled")) is False:
        return 0.0
    category = fee_category_for_market(market)
    if category == "disabled":
        return 0.0
    return float(TAKER_FEE_RATES.get(category, TAKER_FEE_RATES["other"]))


def estimate_taker_fee(shares: float, price: float, market: Dict[str, Any]) -> Tuple[float, float, str]:
    category = fee_category_for_market(market)
    rate = 0.0 if category == "disabled" else float(TAKER_FEE_RATES.get(category, TAKER_FEE_RATES["other"]))
    raw_fee = max(0.0, shares * rate * price * (1.0 - price))
    return round(raw_fee, FEE_PRECISION_PLACES), rate, category


def paper_execution_rules() -> Dict[str, Any]:
    return {
        "source": POLYMARKET_FEE_DOC_URL,
        "paper_fill_role": "taker",
        "maker_fee_rate": 0.0,
        "maker_rebates_credited": False,
        "taker_fee_formula": "fee = shares * fee_rate * price * (1 - price)",
        "fee_precision_places": FEE_PRECISION_PLACES,
        "fee_note": "Fees are protocol/category-specific and applied at match time; paper fills model immediate taker fills unless a future maker/queue simulator is added.",
        "taker_fee_rates": TAKER_FEE_RATES,
        "deposit_withdraw_polymarket_fee": 0.0,
        "intermediary_fees_modeled": False,
        "slippage_bps": SLIPPAGE_BPS,
        "min_order_usdc": MIN_PAPER_ORDER_USDC,
        "allowed_side": "buy and sell",
    }


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


def duel_universe() -> str:
    return os.environ.get("AURUM_DUEL_UNIVERSE", "").strip().lower()


def market_matches_universe(market: Dict[str, Any], universe: str) -> bool:
    if not universe or universe in {"all", "any", "general"}:
        return True
    text = " ".join(
        str(market.get(k, ""))
        for k in ("question", "slug", "category", "description", "market_id", "condition_id")
    ).lower()
    if universe in {"bitcoin", "btc"}:
        return any(term in text for term in BITCOIN_TERMS)
    return universe in text


def extract_search_market_items(raw: Any) -> List[Dict[str, Any]]:
    """Flatten Gamma public-search responses into market-like raw records."""
    items: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if not isinstance(raw, dict):
        return items
    for key in ("markets", "results"):
        value = raw.get(key)
        if isinstance(value, list):
            items.extend(x for x in value if isinstance(x, dict))
    events = raw.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            event_title = event.get("title") or event.get("question") or ""
            event_slug = event.get("slug") or ""
            for market in event.get("markets", []) or []:
                if isinstance(market, dict):
                    merged = dict(market)
                    merged.setdefault("event_title", event_title)
                    merged.setdefault("event_slug", event_slug)
                    items.append(merged)
    return items


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
        "condition_id": str(raw.get("conditionId") or raw.get("condition_id") or ""),
        "question": question[:280],
        "slug": raw.get("slug") or raw.get("market_slug") or "",
        "category": raw.get("category") or raw.get("event_title") or raw.get("event_slug") or "",
        "fee_category": raw.get("feeCategory") or raw.get("fee_category") or raw.get("category") or "",
        "fees_enabled": optional_bool(raw.get("feesEnabled") if "feesEnabled" in raw else raw.get("fees_enabled")),
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
            "category": "Politics",
            "volume": 250000.0,
            "liquidity": 50000.0,
            "end_date": "2026-12-31T00:00:00Z",
            "outcomes": [{"name": "Yes", "price": 0.57, "token_id": "mock-yes"}, {"name": "No", "price": 0.43, "token_id": "mock-no"}],
        },
        {
            "market_id": "mock-fed-1",
            "question": "Mock: Will the Fed cut rates by September?",
            "slug": "mock-fed-1",
            "category": "Finance",
            "volume": 140000.0,
            "liquidity": 22000.0,
            "end_date": "2026-09-30T00:00:00Z",
            "outcomes": [{"name": "Yes", "price": 0.34, "token_id": "mock-fed-yes"}, {"name": "No", "price": 0.66, "token_id": "mock-fed-no"}],
        },
        {
            "market_id": "mock-crypto-1",
            "question": "Mock: Will BTC close above 150k this year?",
            "slug": "mock-crypto-1",
            "category": "Crypto",
            "fee_category": "Crypto",
            "fees_enabled": True,
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
        universe = duel_universe()
        search_query = os.environ.get("AURUM_DUEL_SEARCH_QUERY", "").strip()
        if universe in {"bitcoin", "btc"} and not search_query:
            search_query = "bitcoin"
        raw_items: List[Dict[str, Any]] = []
        if search_query:
            try:
                raw_search = fetch_json(
                    GAMMA_SEARCH_URL.format(query=urllib.parse.quote(search_query)),
                    allow_proxy=allow_proxy,
                )
                raw_items = extract_search_market_items(raw_search)
            except Exception:
                raw_items = []
        if not raw_items:
            raw = fetch_json(GAMMA_MARKETS_URL.format(limit=max(limit * 8, limit)), allow_proxy=allow_proxy)
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
    universe = duel_universe()
    if universe and universe not in {"all", "any", "general"}:
        markets = [m for m in markets if market_matches_universe(m, universe)]
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


def _iso_to_datetime(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def account_total_fees(account: Dict[str, Any]) -> float:
    total = 0.0
    trades = account.get("trades", []) if isinstance(account.get("trades"), list) else []
    for trade in trades:
        if isinstance(trade, dict):
            total += max(0.0, to_float(trade.get("fee"), 0.0))
    return total


def score_penalties(account: Dict[str, Any], *, portfolio_total: float, starting: float) -> Dict[str, float]:
    if starting <= 0:
        return {"fee_churn": 0.0, "drawdown": 0.0, "yolo_exposure": 0.0, "total": 0.0}
    fee_ratio = account_total_fees(account) / starting
    drawdown_ratio = max(0.0, (starting - portfolio_total) / starting)
    exposure_ratio = account_exposure(account) / starting
    penalties = {
        "fee_churn": max(0.0, fee_ratio - FEE_CHURN_FREE_FRACTION) * 100.0,
        "drawdown": max(0.0, drawdown_ratio - DRAWDOWN_FREE_FRACTION) * 100.0,
        "yolo_exposure": max(0.0, exposure_ratio - YOLO_EXPOSURE_FREE_FRACTION) * 100.0,
    }
    penalties["total"] = sum(penalties.values())
    return {key: round(value, 6) for key, value in penalties.items()}


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
    raw_roi_score = roi * 100.0
    penalties = score_penalties(account, portfolio_total=total, starting=starting)
    exposure = account_exposure(account)
    trades = account.get("trades", []) if isinstance(account.get("trades"), list) else []
    risk_events = account.get("risk_events", []) if isinstance(account.get("risk_events"), list) else []
    return {
        "agent_id": account.get("agent_id"),
        "cash": round(cash, 4),
        "position_value": round(pos_value, 4),
        "portfolio_value": round(total, 4),
        "starting_equity": round(starting, 4),
        "roi": round(roi, 6),
        "raw_roi_score": round(raw_roi_score, 4),
        "score": round(raw_roi_score - penalties["total"], 4),
        "penalties": penalties,
        "drawdown": round(max(0.0, (starting - total) / starting) if starting else 0.0, 6),
        "cash_available": round(cash, 4),
        "exposure": round(exposure, 4),
        "trade_count": len(trades),
        "order_count": len(trades) + len(risk_events),
        "risk_event_count": len(risk_events),
        "fees_paid": round(account_total_fees(account), FEE_PRECISION_PLACES),
        "open_positions": len(account.get("positions", {})),
        "details": details,
    }


def _recent_trades(account: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    trades = account.get("trades", []) if isinstance(account.get("trades"), list) else []
    out: List[Dict[str, Any]] = []
    for trade in trades[-limit:]:
        if not isinstance(trade, dict):
            continue
        out.append(
            {
                "ts": trade.get("ts"),
                "market_id": trade.get("market_id"),
                "question": str(trade.get("question") or "")[:180],
                "outcome": trade.get("outcome"),
                "side": trade.get("side"),
                "notional": trade.get("notional", trade.get("gross_proceeds")),
                "fill_price": trade.get("fill_price"),
                "shares": trade.get("shares"),
                "fee": trade.get("fee"),
                "fee_category": trade.get("fee_category"),
                "rationale": str(trade.get("rationale") or "")[:240],
            }
        )
    return out


def _recent_risk_events(account: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    events = account.get("risk_events", []) if isinstance(account.get("risk_events"), list) else []
    out: List[Dict[str, Any]] = []
    for event in events[-limit:]:
        if not isinstance(event, dict):
            continue
        order = event.get("order") if isinstance(event.get("order"), dict) else {}
        out.append(
            {
                "ts": event.get("ts"),
                "reason": str(event.get("reason") or event.get("rejection_reason") or "")[:240],
                "market_id": order.get("market_id") if isinstance(order, dict) else None,
                "outcome": order.get("outcome") if isinstance(order, dict) else None,
                "side": order.get("side") if isinstance(order, dict) else None,
            }
        )
    return out


def compact_account(account: Dict[str, Any]) -> Dict[str, Any]:
    trades = account.get("trades", []) if isinstance(account.get("trades"), list) else []
    risk_events = account.get("risk_events", []) if isinstance(account.get("risk_events"), list) else []
    return {
        "agent_id": account.get("agent_id"),
        "cash": round(float(account.get("cash", 0.0)), 4),
        "starting_equity": account.get("starting_equity"),
        "reserve_cash": account.get("reserve_cash"),
        "open_positions": len(account.get("positions", {})),
        "open_risk_cost_basis": round(account_exposure(account), 4),
        "trade_count": len(trades),
        "risk_event_count": len(risk_events),
        "fees_paid": round(account_total_fees(account), FEE_PRECISION_PLACES),
        "recent_trades": _recent_trades(account),
        "recent_risk_events": _recent_risk_events(account),
    }


def competition_context(*, now: Optional[str] = None, competition_ends_at: Optional[str] = None) -> Dict[str, Any]:
    current = _iso_to_datetime(now or utc_now())
    end_raw = competition_ends_at or os.environ.get("AURUM_COMPETITION_ENDS_AT", "").strip() or os.environ.get("AURUM_STABILITY_WINDOW_END", "").strip()
    end = _iso_to_datetime(end_raw)
    time_remaining = None
    if current and end:
        time_remaining = max(0, int((end - current).total_seconds()))
    return {
        "objective": "rank_1_within_stability_window",
        "paper_only": True,
        "goal": "Maximize risk-adjusted paper score/rank while obeying data-quality, reviewed-script, fee, drawdown, exposure, and paper-only gates.",
        "victory_requires": "rank_1_and_roi_gt_5pct_after_fees",
        "victory_roi_threshold": VICTORY_ROI_THRESHOLD,
        "competition_ends_at": end_raw or None,
        "time_remaining_seconds": time_remaining,
        "score_formula": "raw_roi_score - fee_churn - drawdown - yolo_exposure penalties",
        "anti_yolo_acceptance": [
            "fee_churn",
            "drawdown",
            "yolo_exposure",
            "paper_only_and_reviewed_data_quality_gates",
        ],
        "anti_yolo_notes": [
            "fee_churn penalties reduce score when cumulative fees exceed the free fraction",
            "drawdown penalties reduce score after the drawdown free fraction",
            "yolo_exposure penalties reduce score before hard risk caps are reached",
            "paper-only and reviewed-script/data-quality gates override rank chasing",
        ],
    }


def _scoreboard_row(row: Dict[str, Any], rank: int) -> Dict[str, Any]:
    return {
        "agent_id": row.get("agent_id"),
        "rank": rank,
        "score": row.get("score"),
        "raw_roi_score": row.get("raw_roi_score"),
        "roi": row.get("roi"),
        "drawdown": row.get("drawdown"),
        "cash": row.get("cash"),
        "cash_available": row.get("cash_available"),
        "exposure": row.get("exposure"),
        "open_positions": row.get("open_positions"),
        "trade_count": row.get("trade_count"),
        "order_count": row.get("order_count"),
        "risk_event_count": row.get("risk_event_count"),
        "fees_paid": row.get("fees_paid"),
        "penalties": row.get("penalties"),
    }


def victory_status(ranked: List[Dict[str, Any]], *, threshold: float = VICTORY_ROI_THRESHOLD) -> Dict[str, Any]:
    if not ranked:
        return {
            "valid_victory": False,
            "winner_agent_id": None,
            "leader_agent_id": None,
            "threshold": threshold,
            "reason": "no_scoreboard",
        }
    leader = ranked[0]
    leader_roi = to_float(leader.get("roi"), 0.0)
    valid = leader_roi > threshold
    return {
        "valid_victory": valid,
        "winner_agent_id": leader.get("agent_id") if valid else None,
        "leader_agent_id": leader.get("agent_id"),
        "leader_rank": leader.get("rank"),
        "leader_roi": round(leader_roi, 6),
        "threshold": threshold,
        "reason": "valid_victory" if valid else "rank_1_roi_below_threshold",
    }


def scoreboard_context(
    state: Dict[str, Any],
    prices: Dict[Tuple[str, str], float],
    *,
    viewer_agent_id: Optional[str] = None,
    now: Optional[str] = None,
    competition_ends_at: Optional[str] = None,
) -> Dict[str, Any]:
    accounts = state.get("accounts", {}) if isinstance(state.get("accounts"), dict) else {}
    rows = []
    for agent_id, account in accounts.items():
        if isinstance(account, dict):
            rows.append(portfolio_value(account, prices))
    rows.sort(key=lambda row: float(row.get("score", 0.0) or 0.0), reverse=True)
    ranked = [_scoreboard_row(row, rank) for rank, row in enumerate(rows, 1)]
    own = next((row for row in ranked if row.get("agent_id") == viewer_agent_id), None)
    peers = [row for row in ranked if row.get("agent_id") != viewer_agent_id]
    return {
        "competition": competition_context(now=now, competition_ends_at=competition_ends_at),
        "own_scoreboard": own,
        "peer_scoreboard": peers,
        "scoreboard": ranked,
        "victory": victory_status(ranked),
    }


def superwing_decision(account: Dict[str, Any], markets: List[Dict[str, Any]], max_orders: int = 2, data_dir: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Conservative deterministic baseline with versioned, visible rules.

    The review job may update the JSON rule file, but this code still enforces
    safe ranges and paper-only buy orders.
    """
    rules = strategy_rules.load_superwing_rules(data_dir) if data_dir else dict(strategy_rules.DEFAULT_SUPERWING_RULES)
    orders = []
    max_notional = min(float(rules.get("max_notional", 30.0)), STARTING_EQUITY * MAX_TRADE_FRACTION)
    price_min = float(rules.get("price_min", 0.25))
    price_max = float(rules.get("price_max", 0.48))
    limit_buffer = float(rules.get("limit_buffer", 0.015))
    min_volume = float(rules.get("min_volume", 0.0))
    for m in markets:
        if float(m.get("volume", 0.0)) < min_volume:
            continue
        candidates = sorted(m["outcomes"], key=lambda o: o["price"])
        chosen = None
        for outcome in candidates:
            p = float(outcome["price"])
            if price_min <= p <= price_max:
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
                "limit_price": round(min(0.99, float(chosen["price"]) + limit_buffer), 4),
                "confidence": float(rules.get("confidence", 0.51)),
                "rationale": str(rules.get("selection", "Liquid underdog/near-even paper probe; small capped risk."))[:500],
            }
        )
        if len(orders) >= max_orders:
            break
    return {
        "agent_id": "superwing",
        "orders": orders,
        "notes": f"{rules.get('name', 'Conservative baseline')}: {rules.get('notes', 'small capped buys only; hold if no setup exists.')}",
        "rules_version": rules.get("version"),
        "rules_updated_at": rules.get("updated_at"),
    }


def deepseek_prompt(
    account: Dict[str, Any],
    markets: List[Dict[str, Any]],
    controls: Dict[str, Any],
    strategy_text: str = "",
    scoreboard: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    scoreboard = scoreboard or {"competition": competition_context(), "own_scoreboard": None, "peer_scoreboard": []}
    system = (
        "You are DeepSeek running a PAPER-ONLY Polymarket strategy test for Aurum. "
        "The tournament objective is to rank #1 in the stability window, but only by risk-adjusted paper score. "
        "You cannot place real orders. You cannot request wallets, private keys, USDC, account logins, or geoblock bypass. "
        f"You must choose at most {controls['max_orders']} small buy orders or hold. "
        "Avoid YOLO rank chasing: fee churn, drawdown, and excessive exposure reduce score before hard gates reject orders. "
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
                "fee_category": fee_category_for_market(m),
                "taker_fee_rate": taker_fee_rate_for_market(m),
                "outcomes": [{"name": o["name"], "price": o["price"]} for o in m.get("outcomes", [])],
            }
        )
    user = json.dumps(
        {
            "task": "Pick paper trades for the deepseek account. Rank #1 is the objective, but if edge is unclear or penalties/risk gates dominate, return no orders.",
            "competition": scoreboard.get("competition"),
            "own_account": compact_account(account),
            "own_scoreboard": scoreboard.get("own_scoreboard"),
            "peer_scoreboard": scoreboard.get("peer_scoreboard", []),
            "victory_status": scoreboard.get("victory"),
            "risk_limits": {
                "max_orders": controls["max_orders"],
                "max_notional_per_order": round(float(controls["max_notional_per_order"]), 2),
                "reserve_cash": RESERVE_CASH,
                "allowed_side": "buy and sell",
                "limit_price_required": True,
            },
            "paper_execution_rules": paper_execution_rules(),
            "current_strategy_rules": strategy_text[:4500],
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
        parsed = json.loads(text)
    except Exception as exc:
        raise DuelError("DeepSeek response must be exactly one valid JSON object") from exc
    if not isinstance(parsed, dict):
        raise DuelError("DeepSeek response JSON root must be an object")
    return parsed


def deepseek_payload(
    model: str,
    system: str,
    user: str,
    controls: Dict[str, Any],
    *,
    structured_retry: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    if structured_retry:
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = controls["temperature"]
        payload["messages"] = [
            {"role": "system", "content": system + " Return the final answer in the content field as one JSON object."},
            {"role": "user", "content": user + "\n\nThe previous response was not parseable JSON. Retry with exactly one valid JSON object."},
        ]
        return payload
    if controls.get("thinking") in {"enabled", "disabled"}:
        payload["thinking"] = {"type": controls["thinking"]}
    if controls.get("thinking") == "enabled":
        if controls.get("reasoning_effort") in {"high", "max"}:
            payload["reasoning_effort"] = controls["reasoning_effort"]
    else:
        payload["temperature"] = controls["temperature"]
    return payload


def call_deepseek_chat(endpoint: str, api_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
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
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(800).decode("utf-8", "replace") if exc.fp else ""
        raise DuelError(f"DeepSeek HTTP {exc.code}: {detail[:400]}") from exc


def extract_deepseek_decision_from_response(data: Dict[str, Any]) -> Dict[str, Any]:
    message = data.get("choices", [{}])[0].get("message", {}) or {}
    candidates = [
        str(message.get("content") or ""),
        str(message.get("reasoning_content") or ""),
    ]
    last_error: Optional[Exception] = None
    for content in candidates:
        if not content.strip():
            continue
        try:
            return extract_json_object(content)
        except Exception as exc:
            last_error = exc
    raise DuelError(
        "DeepSeek response did not contain a JSON object "
        f"(message_keys={sorted(message.keys())}, content_len={len(candidates[0])}, reasoning_len={len(candidates[1])})"
    ) from last_error


def deepseek_decision(
    account: Dict[str, Any],
    markets: List[Dict[str, Any]],
    controls: Dict[str, Any],
    data_dir: Optional[pathlib.Path] = None,
    scoreboard: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DuelError("DEEPSEEK_API_KEY is not set; cannot ask DeepSeek to self-decide")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    endpoint = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions").strip()
    if endpoint.rstrip("/").endswith("/v1"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"
    strategy_text = strategy_rules.load_deepseek_rules(data_dir) if data_dir else ""
    system, user = deepseek_prompt(account, markets, controls, strategy_text=strategy_text, scoreboard=scoreboard)
    first_payload = deepseek_payload(model, system, user, controls)
    data = call_deepseek_chat(endpoint, api_key, first_payload)
    retry_used = False
    try:
        decision = extract_deepseek_decision_from_response(data)
    except Exception as first_exc:
        if env_bool("AURUM_DEEPSEEK_STRUCTURED_RETRY", True):
            retry_payload = deepseek_payload(model, system, user, controls, structured_retry=True)
            retry_data = call_deepseek_chat(endpoint, api_key, retry_payload)
            try:
                decision = extract_deepseek_decision_from_response(retry_data)
                retry_used = True
            except Exception as retry_exc:
                raise DuelError(f"DeepSeek JSON parse failed after structured retry: {retry_exc}") from first_exc
        else:
            raise
    decision.setdefault("agent_id", "deepseek")
    decision.setdefault("orders", [])
    decision.setdefault("notes", "")
    decision["raw_model"] = model
    decision["structured_retry_used"] = retry_used
    if data_dir:
        decision["strategy_rules_length"] = len(strategy_text)
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
    execution_context: Optional[Dict[str, Any]] = None,
    now_fn: Callable[[], str] = utc_now,
) -> Dict[str, Any]:
    if agent_id not in state["accounts"]:
        raise DuelError(f"unknown agent: {agent_id}")
    account = state["accounts"][agent_id]
    market_by_id = {m["market_id"]: m for m in markets}
    fills = []
    rejections = []
    decision_id = sha256_json({"agent_id": agent_id, "decision": decision})
    decision_record = {
        "ts": now_fn(),
        "agent_id": agent_id,
        "decision_id": decision_id,
        "decision": decision,
        "applied": bool(apply),
        "execution_context": risk_ledger_context(execution_context),
        "controls": {
            "max_orders": max_orders,
            "max_notional_per_order": round(max_notional_per_order, 4),
            "paper_execution_rules": paper_execution_rules(),
        },
    }
    append_jsonl(decisions_path(data_dir), decision_record)
    orders = decision.get("orders", []) or []
    if not isinstance(orders, list):
        orders = []
    decision_account = account_digest(account)
    append_risk_ledger(
        data_dir,
        {
            "ts": now_fn(),
            "event": "decision_recorded",
            "agent_id": agent_id,
            "decision_id": decision_id,
            "applied": bool(apply),
            "execution_context": risk_ledger_context(execution_context),
            "order_count": len(orders),
            "account_delta": {"before": decision_account, "after": decision_account},
        },
    )
    for idx, order in enumerate(orders):
        order_id = f"{decision_id}:{idx}"
        account_before = account_digest(account)
        if idx >= max_orders:
            rejection = {
                "ts": now_fn(),
                "agent_id": agent_id,
                "order": order,
                "reason": f"order count exceeds configured max_orders={max_orders}",
            }
            rejections.append(rejection)
            append_risk_ledger(
                data_dir,
                {
                    "ts": now_fn(),
                    "event": "order_rejected",
                    "agent_id": agent_id,
                    "decision_id": decision_id,
                    "order_id": order_id,
                    "applied": bool(apply),
                    "execution_context": risk_ledger_context(execution_context),
                    "order": order,
                    "rejection_reason": rejection["reason"],
                    "account_delta": {"before": account_before, "after": account_digest(account)},
                },
            )
            continue
        try:
            market_id = str(order.get("market_id", ""))
            market = market_by_id.get(market_id)
            if not market:
                raise DuelError("unknown market_id")
            side = str(order.get("side", "")).lower()
            if side not in {"buy", "sell"}:
                raise DuelError("only buy or sell side is allowed in paper mode")
            outcome_name = str(order.get("outcome", ""))
            outcome = None
            for o in market.get("outcomes", []):
                if str(o["name"]).lower() == outcome_name.lower():
                    outcome = o
                    break
            if not outcome:
                raise DuelError("unknown outcome")
            observed_price = float(outcome["price"])
            limit_price = to_float(order.get("limit_price"), 0.0)
            if not (0.01 <= limit_price <= 0.99):
                raise DuelError("valid limit_price is required")
            pos_key = market_id + "::" + str(outcome["name"])

            if side == "buy":
                requested_notional = to_float(order.get("notional"), 0.0)
                if requested_notional > max_notional_per_order:
                    raise DuelError(f"notional {requested_notional:.4f} exceeds configured cap {max_notional_per_order:.4f}")
                notional = requested_notional
                if notional < MIN_PAPER_ORDER_USDC:
                    raise DuelError(f"notional must be at least Polymarket paper minimum {MIN_PAPER_ORDER_USDC:.2f} USDC")
                book_walk = recorded_book_walk(
                    execution_context,
                    market=market,
                    outcome_name=outcome_name,
                    side="buy",
                    limit_price=limit_price,
                    notional=notional,
                )
                if book_walk is not None:
                    if not book_walk.get("ok"):
                        raise DuelError(str(book_walk.get("reason") or "recorded_book_walk_rejected"))
                    fill_price = float(book_walk["fill_price"])
                    shares = float(book_walk["shares"])
                    notional = float(book_walk["notional"])
                    fill_source = "recorded_orderbook_depth"
                else:
                    book_walk = {}
                    fill_price = min(0.99, observed_price + SLIPPAGE_BPS / 10000.0)
                    shares = notional / fill_price
                    fill_source = "legacy_top_price_slippage"
                if fill_price > limit_price:
                    raise DuelError(f"fill_price {fill_price:.4f} exceeds limit_price {limit_price:.4f}")
                fee, fee_rate, fee_category = estimate_taker_fee(shares, fill_price, market)
                gross_cost = notional + fee
                if float(account.get("cash", 0.0)) - gross_cost < RESERVE_CASH:
                    raise DuelError("reserve cash would be breached after Polymarket taker fee")
                if account_exposure(account) + gross_cost > STARTING_EQUITY * MAX_TOTAL_RISK_FRACTION:
                    raise DuelError("total open-risk cap would be breached after Polymarket taker fee")
                if market_exposure(account, market_id) + gross_cost > STARTING_EQUITY * MAX_MARKET_FRACTION:
                    raise DuelError("per-market exposure cap would be breached after Polymarket taker fee")
                fill = {
                    "ts": now_fn(),
                    "agent_id": agent_id,
                    "decision_id": decision_id,
                    "order_id": order_id,
                    "market_id": market_id,
                    "question": market["question"],
                    "outcome": outcome["name"],
                    "side": "buy",
                    "notional": round(notional, 4),
                    "observed_price": round(observed_price, 4),
                    "fill_price": round(fill_price, 4),
                    "shares": round(shares, 6),
                    "fee": round(fee, FEE_PRECISION_PLACES),
                    "fee_rate": fee_rate,
                    "fee_category": fee_category,
                    "gross_cost": round(gross_cost, 5),
                    "paper_execution_role": "recorded_taker_book_walk" if fill_source == "recorded_orderbook_depth" else "taker",
                    "fill_source": fill_source,
                    "book_walk": book_walk,
                    "fee_formula": "shares * fee_rate * price * (1 - price)",
                    "rationale": str(order.get("rationale", ""))[:500],
                }
                fills.append(fill)
                if apply:
                    existing = account["positions"].get(pos_key)
                    if existing:
                        old_shares = float(existing.get("shares", 0.0))
                        old_cost = float(existing.get("cost_basis", 0.0))
                        new_shares = old_shares + shares
                        new_cost = old_cost + gross_cost
                        existing.update(
                            {
                                "shares": new_shares,
                                "cost_basis": new_cost,
                                "avg_price": new_cost / new_shares if new_shares else fill_price,
                                "last_price": observed_price,
                                "fees_paid": round(float(existing.get("fees_paid", 0.0)) + fee, FEE_PRECISION_PLACES),
                                "updated_at": now_fn(),
                            }
                        )
                    else:
                        account["positions"][pos_key] = {
                            "market_id": market_id,
                            "question": market["question"],
                            "outcome": outcome["name"],
                            "shares": shares,
                            "cost_basis": gross_cost,
                            "avg_price": gross_cost / shares if shares else fill_price,
                            "last_price": observed_price,
                            "fees_paid": fee,
                            "fee_category": fee_category,
                            "created_at": now_fn(),
                            "updated_at": now_fn(),
                        }
                    account["cash"] = float(account.get("cash", 0.0)) - gross_cost
                    account.setdefault("trades", []).append(fill)
                    account["updated_at"] = now_fn()
                append_risk_ledger(
                    data_dir,
                    {
                        "ts": now_fn(),
                        "event": "order_filled",
                        "agent_id": agent_id,
                        "decision_id": decision_id,
                        "order_id": order_id,
                        "applied": bool(apply),
                        "execution_context": risk_ledger_context(execution_context),
                        "order": order,
                        "order_simulation": fill,
                        "fee_assumptions": {"fee_rate": fee_rate, "fee_category": fee_category, "fee_formula": fill["fee_formula"]},
                        "account_delta": {"before": account_before, "after": account_digest(account)},
                    },
                )
            else:
                existing = account["positions"].get(pos_key)
                if not existing:
                    raise DuelError("cannot sell without an open position")
                held_shares = float(existing.get("shares", 0.0))
                requested_shares = to_float(order.get("shares"), 0.0)
                requested_notional = to_float(order.get("notional"), 0.0)
                if requested_shares <= 0 and requested_notional > 0:
                    requested_shares = requested_notional / max(0.01, observed_price)
                shares = min(held_shares, requested_shares)
                if shares <= 0:
                    raise DuelError("sell order requires positive shares or notional")
                book_walk = recorded_book_walk(
                    execution_context,
                    market=market,
                    outcome_name=outcome_name,
                    side="sell",
                    limit_price=limit_price,
                    shares=shares,
                )
                if book_walk is not None:
                    if not book_walk.get("ok"):
                        raise DuelError(str(book_walk.get("reason") or "recorded_book_walk_rejected"))
                    fill_price = float(book_walk["fill_price"])
                    shares = float(book_walk["shares"])
                    fill_source = "recorded_orderbook_depth"
                else:
                    book_walk = {}
                    fill_price = max(0.01, observed_price - SLIPPAGE_BPS / 10000.0)
                    fill_source = "legacy_top_price_slippage"
                if fill_price < limit_price:
                    raise DuelError(f"fill_price {fill_price:.4f} is below limit_price {limit_price:.4f}")
                fee, fee_rate, fee_category = estimate_taker_fee(shares, fill_price, market)
                gross_proceeds = shares * fill_price
                net_proceeds = gross_proceeds - fee
                if net_proceeds <= 0:
                    raise DuelError("sell proceeds would be non-positive after fee")
                fill = {
                    "ts": now_fn(),
                    "agent_id": agent_id,
                    "decision_id": decision_id,
                    "order_id": order_id,
                    "market_id": market_id,
                    "question": market["question"],
                    "outcome": outcome["name"],
                    "side": "sell",
                    "notional": round(gross_proceeds, 4),
                    "observed_price": round(observed_price, 4),
                    "fill_price": round(fill_price, 4),
                    "shares": round(shares, 6),
                    "fee": round(fee, FEE_PRECISION_PLACES),
                    "fee_rate": fee_rate,
                    "fee_category": fee_category,
                    "gross_proceeds": round(gross_proceeds, 5),
                    "net_proceeds": round(net_proceeds, 5),
                    "paper_execution_role": "recorded_taker_book_walk" if fill_source == "recorded_orderbook_depth" else "taker",
                    "fill_source": fill_source,
                    "book_walk": book_walk,
                    "fee_formula": "shares * fee_rate * price * (1 - price)",
                    "rationale": str(order.get("rationale", ""))[:500],
                }
                fills.append(fill)
                if apply:
                    remaining_shares = held_shares - shares
                    old_cost = float(existing.get("cost_basis", 0.0))
                    cost_reduction = old_cost * (shares / held_shares) if held_shares else 0.0
                    if remaining_shares <= 1e-9:
                        account["positions"].pop(pos_key, None)
                    else:
                        new_cost = max(0.0, old_cost - cost_reduction)
                        existing.update(
                            {
                                "shares": remaining_shares,
                                "cost_basis": new_cost,
                                "avg_price": (new_cost / remaining_shares) if remaining_shares else fill_price,
                                "last_price": observed_price,
                                "fees_paid": round(float(existing.get("fees_paid", 0.0)) + fee, FEE_PRECISION_PLACES),
                                "updated_at": now_fn(),
                            }
                        )
                    account["cash"] = float(account.get("cash", 0.0)) + net_proceeds
                    account.setdefault("trades", []).append(fill)
                    account["updated_at"] = now_fn()
                append_risk_ledger(
                    data_dir,
                    {
                        "ts": now_fn(),
                        "event": "order_filled",
                        "agent_id": agent_id,
                        "decision_id": decision_id,
                        "order_id": order_id,
                        "applied": bool(apply),
                        "execution_context": risk_ledger_context(execution_context),
                        "order": order,
                        "order_simulation": fill,
                        "fee_assumptions": {"fee_rate": fee_rate, "fee_category": fee_category, "fee_formula": fill["fee_formula"]},
                        "account_delta": {"before": account_before, "after": account_digest(account)},
                    },
                )
        except Exception as exc:
            rejection = {"ts": now_fn(), "agent_id": agent_id, "order": order, "reason": str(exc)}
            rejections.append(rejection)
            if apply:
                account.setdefault("risk_events", []).append(rejection)
            append_risk_ledger(
                data_dir,
                {
                    "ts": now_fn(),
                    "event": "order_rejected",
                    "agent_id": agent_id,
                    "decision_id": decision_id,
                    "order_id": order_id,
                    "applied": bool(apply),
                    "execution_context": risk_ledger_context(execution_context),
                    "order": order,
                    "rejection_reason": str(exc),
                    "account_delta": {"before": account_before, "after": account_digest(account)},
                },
            )
    state["last_markets"] = markets
    if apply:
        save_state(data_dir, state, now_fn=now_fn)
    event = {"ts": now_fn(), "event": "decision_result", "agent_id": agent_id, "decision_id": decision_id, "fills": fills, "rejections": rejections, "applied": bool(apply)}
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
        decision = superwing_decision(state["accounts"]["superwing"], markets, max_orders=args.max_orders, data_dir=data_dir)
    elif args.agent == "deepseek":
        controls = deepseek_controls(args.max_orders)
        validation_max_orders = int(controls["max_orders"])
        validation_max_notional = float(controls["max_notional_per_order"])
        if not args.no_apply:
            require_deepseek_apply_authorized(controls)
        scoreboard = scoreboard_context(state, market_price_map(markets), viewer_agent_id="deepseek")
        decision = deepseek_decision(state["accounts"]["deepseek"], markets, controls, data_dir=data_dir, scoreboard=scoreboard)
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
        load_env_file(pathlib.Path(args.env_file) if getattr(args, "env_file", None) else None)
        args.func(args)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
