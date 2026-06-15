#!/usr/bin/env python3
"""CEX public-data adapter and exchange-like paper arena for Aurum.

This module is deliberately public-data/paper-only.  It normalizes exchange
REST snapshots into a small frame contract and applies local paper fills against
that frame.  It never signs, submits, cancels, or authenticates exchange orders.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

USER_AGENT = "aurum-cex-arena/0.1 (+public-data-paper-only)"
DEFAULT_FEE_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 2.0
MIN_ORDER_USD = 5.0
EXCHANGE_ENV_PREFIXES = ("OKX_", "BYBIT_", "BINANCE_", "BINANCEUSDM_", "HYPERLIQUID_", "COINBASE_")
BLOCKED_ENV_TOKENS = (
    "API_KEY",
    "API_SECRET",
    "SECRET_KEY",
    "PASSPHRASE",
    "PASS_PHRASE",
    "PRIVATE_KEY",
    "WALLET",
    "REAL_ORDER",
)


class CexArenaError(RuntimeError):
    pass


class CexSafetyError(CexArenaError):
    pass


JsonFetcher = Callable[..., Any]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return parsed if math.isfinite(parsed) else default


def require_positive(value: Any, label: str) -> float:
    parsed = to_float(value, float("nan"))
    if not math.isfinite(parsed) or parsed <= 0:
        raise CexArenaError(f"{label} must be a positive finite number")
    return parsed


def assert_public_data_only(env: Optional[Mapping[str, str]] = None) -> None:
    """Fail closed when private/live-trading exchange env vars are present.

    The CEX lane starts as public data + paper fills only.  Accidentally running
    it in a shell with exchange credentials is a configuration smell, so the
    guard rejects that environment instead of risking future live-order drift.
    """

    source = dict(os.environ if env is None else env)
    blocked = []
    for key, value in source.items():
        if not value:
            continue
        upper = key.upper()
        if upper == "AURUM_CEX_ENABLE_LIVE_TRADING":
            if str(value).strip().lower() not in {"0", "false", "no"}:
                blocked.append(key)
            continue
        exchange_scoped = upper.startswith(EXCHANGE_ENV_PREFIXES)
        if exchange_scoped and any(token in upper for token in BLOCKED_ENV_TOKENS):
            blocked.append(key)
    if blocked:
        raise CexSafetyError("private/live exchange env vars present; CEX adapter is public-data paper-only: " + ", ".join(sorted(blocked)))


def fetch_json(url: str, timeout: float = 12.0, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Any:
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _first_data(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else {}
        if isinstance(data, dict):
            return data
        result = payload.get("result")
        if isinstance(result, dict):
            if isinstance(result.get("list"), list) and result["list"]:
                return result["list"][0] if isinstance(result["list"][0], dict) else {}
            return result
    return {}


def _okx_urls(symbol: str) -> Dict[str, str]:
    quoted = urllib.parse.quote(symbol)
    return {
        "ticker": f"https://www.okx.com/api/v5/market/ticker?instId={quoted}",
        "book": f"https://www.okx.com/api/v5/market/books?instId={quoted}&sz=20",
        "candles": f"https://www.okx.com/api/v5/market/candles?instId={quoted}&bar=1m&limit=120",
        "funding": f"https://www.okx.com/api/v5/public/funding-rate?instId={quoted}",
    }


def _bybit_urls(symbol: str) -> Dict[str, str]:
    quoted = urllib.parse.quote(symbol)
    return {
        "ticker": f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={quoted}",
        "book": f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={quoted}&limit=25",
        "candles": f"https://api.bybit.com/v5/market/kline?category=linear&symbol={quoted}&interval=1&limit=120",
        "funding": f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={quoted}&limit=1",
    }


def _binance_urls(symbol: str) -> Dict[str, str]:
    quoted = urllib.parse.quote(symbol)
    return {
        "ticker": f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={quoted}",
        "book": f"https://fapi.binance.com/fapi/v1/depth?symbol={quoted}&limit=20",
        "candles": f"https://fapi.binance.com/fapi/v1/klines?symbol={quoted}&interval=1m&limit=120",
        "funding": f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={quoted}",
    }


def exchange_urls(exchange: str, symbol: str) -> Dict[str, str]:
    exchange = exchange.lower().strip()
    if exchange == "okx":
        return _okx_urls(symbol)
    if exchange == "bybit":
        return _bybit_urls(symbol)
    if exchange == "binance":
        return _binance_urls(symbol)
    raise CexArenaError(f"unsupported exchange={exchange!r}; supported: okx, bybit, binance")


def _okx_candles(payload: Any) -> List[Dict[str, float]]:
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    candles = []
    for row in rows[:120]:
        if not isinstance(row, list) or len(row) < 5:
            continue
        candles.append({"ts_ms": int(to_float(row[0])), "open": to_float(row[1]), "high": to_float(row[2]), "low": to_float(row[3]), "close": to_float(row[4]), "volume": to_float(row[5] if len(row) > 5 else 0)})
    candles.sort(key=lambda x: x["ts_ms"])
    return candles


def _bybit_candles(payload: Any) -> List[Dict[str, float]]:
    rows = (((payload or {}).get("result") or {}).get("list") or []) if isinstance(payload, dict) else []
    candles = []
    for row in rows[:120]:
        if not isinstance(row, list) or len(row) < 5:
            continue
        candles.append({"ts_ms": int(to_float(row[0])), "open": to_float(row[1]), "high": to_float(row[2]), "low": to_float(row[3]), "close": to_float(row[4]), "volume": to_float(row[5] if len(row) > 5 else 0)})
    candles.sort(key=lambda x: x["ts_ms"])
    return candles


def _binance_candles(payload: Any) -> List[Dict[str, float]]:
    candles = []
    for row in payload[:120] if isinstance(payload, list) else []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candles.append({"ts_ms": int(to_float(row[0])), "open": to_float(row[1]), "high": to_float(row[2]), "low": to_float(row[3]), "close": to_float(row[4]), "volume": to_float(row[5])})
    candles.sort(key=lambda x: x["ts_ms"])
    return candles


def normalize_snapshot(exchange: str, symbol: str, ticker: Any, book: Any, candles: Any, funding: Any, *, now: str) -> Dict[str, Any]:
    exchange = exchange.lower().strip()
    if exchange == "okx":
        t = _first_data(ticker)
        b = _first_data(book)
        best_bid = to_float((b.get("bids") or [[t.get("bidPx")]])[0][0])
        best_ask = to_float((b.get("asks") or [[t.get("askPx")]])[0][0])
        last = to_float(t.get("last"))
        fund = _first_data(funding)
        candle_rows = _okx_candles(candles)
        volume_24h = to_float(t.get("vol24h"))
        funding_rate = to_float(fund.get("fundingRate"), 0.0)
    elif exchange == "bybit":
        t = _first_data(ticker)
        b = ((book or {}).get("result") or {}) if isinstance(book, dict) else {}
        best_bid = to_float((b.get("b") or [[t.get("bid1Price")]])[0][0])
        best_ask = to_float((b.get("a") or [[t.get("ask1Price")]])[0][0])
        last = to_float(t.get("lastPrice"))
        candle_rows = _bybit_candles(candles)
        fund = _first_data(funding)
        funding_rate = to_float(t.get("fundingRate") or fund.get("fundingRate"), 0.0)
        volume_24h = to_float(t.get("volume24h"))
    elif exchange == "binance":
        t = ticker if isinstance(ticker, dict) else {}
        b = book if isinstance(book, dict) else {}
        best_bid = to_float((b.get("bids") or [[t.get("bidPrice")]])[0][0])
        best_ask = to_float((b.get("asks") or [[t.get("askPrice")]])[0][0])
        last = to_float(t.get("lastPrice"))
        candle_rows = _binance_candles(candles)
        funding_rate = to_float((funding or {}).get("lastFundingRate"), 0.0) if isinstance(funding, dict) else 0.0
        volume_24h = to_float(t.get("volume"))
    else:
        raise CexArenaError(f"unsupported exchange={exchange!r}")
    if last <= 0 and best_bid > 0 and best_ask > 0:
        last = (best_bid + best_ask) / 2
    if best_bid <= 0 or best_ask <= 0 or last <= 0:
        raise CexArenaError("ticker/book snapshot did not contain positive bid/ask/last")
    mid = (best_bid + best_ask) / 2
    return {
        "source": "cex_public_api_v0",
        "ts": now,
        "exchange": exchange,
        "symbol": symbol,
        "market_id": f"{exchange}:{symbol}",
        "question": f"{symbol} perpetual price on {exchange.upper()}",
        "market_type": "cex_perp",
        "quote_currency": "USDT",
        "last": round(last, 8),
        "best_bid": round(best_bid, 8),
        "best_ask": round(best_ask, 8),
        "mid": round(mid, 8),
        "spread_bps": round(((best_ask - best_bid) / mid) * 10000.0, 4),
        "funding_rate": funding_rate,
        "volume_24h": volume_24h,
        "candles": candle_rows,
        "fee_bps": DEFAULT_FEE_BPS,
    }


def fetch_exchange_snapshot(exchange: str, symbol: str, *, fetcher: JsonFetcher = fetch_json, now: Callable[[], str] = utc_now, timeout: float = 12.0) -> Dict[str, Any]:
    assert_public_data_only()
    urls = exchange_urls(exchange, symbol)
    payloads = {name: fetcher(url, timeout=timeout) for name, url in urls.items()}
    return normalize_snapshot(exchange, symbol, payloads["ticker"], payloads["book"], payloads["candles"], payloads["funding"], now=now())


def new_exchange_account(agent_id: str, *, starting_cash: float = 1500.0, quote_currency: str = "paper_usdt", now: Callable[[], str] = utc_now) -> Dict[str, Any]:
    ts = now()
    return {
        "agent_id": agent_id,
        "mode": "paper_cex",
        "quote_currency": quote_currency,
        "starting_cash": float(starting_cash),
        "cash": float(starting_cash),
        "positions": {},
        "trades": [],
        "risk_events": [],
        "realized_pnl": 0.0,
        "created_at": ts,
        "updated_at": ts,
    }


def _position_key(frame: Mapping[str, Any]) -> str:
    return f"{frame.get('exchange')}:{frame.get('symbol')}"


def _fee(gross: float, frame: Mapping[str, Any]) -> float:
    fee_bps = to_float(frame.get("fee_bps"), DEFAULT_FEE_BPS)
    return round(max(0.0, gross) * fee_bps / 10000.0, 8)


def apply_paper_order(
    account: Dict[str, Any],
    frame: Mapping[str, Any],
    order: Mapping[str, Any],
    *,
    allow_short: bool = False,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    min_order_usd: float = MIN_ORDER_USD,
    now: Callable[[], str] = utc_now,
) -> Dict[str, Any]:
    """Apply one exchange-like market paper order to an in-memory account.

    v1 supports long-only by default.  Shorting can be enabled by caller later,
    but the first migration path keeps it off to avoid accidental leverage-like
    behavior while wiring public CEX data.
    """

    ts = now()
    side = str(order.get("side", "")).lower().strip()
    if side not in {"buy", "sell"}:
        return {"ok": False, "reason": "side must be buy or sell", "ts": ts}
    key = _position_key(frame)
    bid = require_positive(frame.get("best_bid"), "best_bid")
    ask = require_positive(frame.get("best_ask"), "best_ask")
    notional = require_positive(order.get("notional"), "notional")
    if notional < min_order_usd:
        return {"ok": False, "reason": f"notional below paper minimum {min_order_usd:.2f}", "ts": ts}
    slip = max(0.0, float(slippage_bps)) / 10000.0
    pos = account.setdefault("positions", {}).get(key)
    if side == "buy":
        fill_price = ask * (1.0 + slip)
        qty = notional / fill_price
        fee = _fee(notional, frame)
        gross_cost = notional + fee
        if float(account.get("cash", 0.0)) < gross_cost:
            return {"ok": False, "reason": "insufficient paper cash", "ts": ts}
        if pos:
            old_qty = float(pos.get("qty", 0.0))
            old_cost = float(pos.get("cost_basis", 0.0))
            new_qty = old_qty + qty
            new_cost = old_cost + gross_cost
            pos.update({"qty": new_qty, "cost_basis": new_cost, "avg_entry": new_cost / new_qty if new_qty else fill_price, "last_price": frame.get("last"), "updated_at": ts})
        else:
            account["positions"][key] = {"exchange": frame.get("exchange"), "symbol": frame.get("symbol"), "qty": qty, "side": "long", "cost_basis": gross_cost, "avg_entry": gross_cost / qty if qty else fill_price, "last_price": frame.get("last"), "created_at": ts, "updated_at": ts}
        account["cash"] = float(account.get("cash", 0.0)) - gross_cost
        trade = {"ts": ts, "agent_id": account.get("agent_id"), "exchange": frame.get("exchange"), "symbol": frame.get("symbol"), "side": "buy", "qty": round(qty, 10), "notional": round(notional, 6), "fill_price": round(fill_price, 8), "fee": fee, "fee_bps": to_float(frame.get("fee_bps"), DEFAULT_FEE_BPS), "paper_execution_role": "cex_taker_market_paper", "source": frame.get("source"), "rationale": str(order.get("rationale", ""))[:500]}
    else:
        held = float((pos or {}).get("qty", 0.0))
        fill_price = bid * (1.0 - slip)
        requested_qty = to_float(order.get("qty"), 0.0)
        if requested_qty <= 0:
            requested_qty = notional / fill_price
        if held <= 0 and not allow_short:
            return {"ok": False, "reason": "shorting disabled and no long position exists", "ts": ts}
        if not allow_short and requested_qty > held + 1e-12:
            return {"ok": False, "reason": "sell quantity exceeds long position while shorting disabled", "ts": ts}
        qty = requested_qty if allow_short else min(held, requested_qty)
        gross = qty * fill_price
        fee = _fee(gross, frame)
        net = gross - fee
        if net <= 0:
            return {"ok": False, "reason": "non-positive proceeds after fee", "ts": ts}
        realized = 0.0
        if pos:
            old_qty = float(pos.get("qty", 0.0))
            old_cost = float(pos.get("cost_basis", 0.0))
            avg_entry = old_cost / old_qty if old_qty else fill_price
            realized = (fill_price - avg_entry) * qty - fee
            remaining = old_qty - qty
            if remaining <= 1e-12:
                account["positions"].pop(key, None)
            else:
                new_cost = max(0.0, old_cost * (remaining / old_qty)) if old_qty else 0.0
                pos.update({"qty": remaining, "cost_basis": new_cost, "avg_entry": new_cost / remaining if remaining else fill_price, "last_price": frame.get("last"), "updated_at": ts})
        account["cash"] = float(account.get("cash", 0.0)) + net
        account["realized_pnl"] = float(account.get("realized_pnl", 0.0)) + realized
        trade = {"ts": ts, "agent_id": account.get("agent_id"), "exchange": frame.get("exchange"), "symbol": frame.get("symbol"), "side": "sell", "qty": round(qty, 10), "notional": round(gross, 6), "fill_price": round(fill_price, 8), "fee": fee, "fee_bps": to_float(frame.get("fee_bps"), DEFAULT_FEE_BPS), "net_proceeds": round(net, 6), "realized_pnl": round(realized, 8), "paper_execution_role": "cex_taker_market_paper", "source": frame.get("source"), "reduce_only": bool(order.get("reduce_only")), "rationale": str(order.get("rationale", ""))[:500]}
    account.setdefault("trades", []).append(trade)
    account["updated_at"] = ts
    return {"ok": True, "trade": trade, "account_digest": {"cash": round(float(account.get("cash", 0.0)), 8), "positions": len(account.get("positions", {})), "trades": len(account.get("trades", [])), "realized_pnl": round(float(account.get("realized_pnl", 0.0)), 8)}}


def atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _day_from_ts(ts: str) -> str:
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return dt.datetime.now(dt.timezone.utc).date().isoformat()


def record_snapshot(data_dir: pathlib.Path, frame: Mapping[str, Any]) -> Dict[str, Any]:
    """Persist a normalized CEX frame using Aurum's recorder-style layout."""

    return record_frames(data_dir, [frame])


def _valid_frame(frame: Mapping[str, Any]) -> bool:
    try:
        return bool(
            str(frame.get("source") or "") == "cex_public_api_v0"
            and str(frame.get("market_type") or "") == "cex_perp"
            and require_positive(frame.get("best_bid"), "best_bid") > 0
            and require_positive(frame.get("best_ask"), "best_ask") > 0
            and require_positive(frame.get("last"), "last") > 0
        )
    except Exception:
        return False


def record_frames(data_dir: pathlib.Path, frames: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Persist one or more normalized CEX frames as the latest CEX market set."""

    data_dir = pathlib.Path(data_dir)
    frame_list = [dict(frame) for frame in frames if isinstance(frame, Mapping)]
    if not frame_list:
        raise CexArenaError("record_frames requires at least one frame")
    ts = str(frame_list[-1].get("ts") or utc_now())
    day = _day_from_ts(ts)
    raw_dir = data_dir / "raw" / "cex" / day
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "snapshots.jsonl"
    ok_frames = [frame for frame in frame_list if _valid_frame(frame)]
    raw_record = {
        "ts": ts,
        "source": "cex_public_api_v0",
        "ok": bool(ok_frames),
        "market_count": len(frame_list),
        "ok_market_count": len(ok_frames),
        "payload": {"markets": frame_list},
    }
    with raw_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(raw_record, ensure_ascii=False, sort_keys=True) + "\n")
    exchanges = sorted({str(frame.get("exchange") or "unknown") for frame in frame_list})
    symbols = [str(frame.get("symbol") or "unknown") for frame in frame_list]
    latest_payload = {
        "ts": ts,
        "source": "cex_public_api_v0",
        "market_source": "cex_public_api_v0",
        "market_type": "cex_perp",
        "exchanges": exchanges,
        "symbols": symbols,
        "market_count": len(frame_list),
        "markets": frame_list,
        "book_coverage": {"requested_tokens": len(frame_list), "ok_tokens": len(ok_frames)},
        "orderable_market_count": len(ok_frames),
    }
    health = {
        "ts": ts,
        "ok": len(ok_frames) == len(frame_list),
        "source": "cex_public_api_v0",
        "latest_markets_path": "normalized/cex/latest_markets.json",
        "raw_path": str(raw_path.relative_to(data_dir)),
        "market_count": len(frame_list),
        "ok_market_count": len(ok_frames),
        "book_coverage": latest_payload["book_coverage"],
        "orderable_market_count": len(ok_frames),
    }
    atomic_write_json(data_dir / "normalized" / "cex" / "latest_markets.json", latest_payload)
    atomic_write_json(data_dir / "reports" / "cex_recorder_health.json", health)
    return health


def read_json_file(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def append_jsonl(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True) + "\n")


def tail_jsonl(path: pathlib.Path, limit: int = 1) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    rows: List[Dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except Exception:
            continue
    return rows


def load_latest_cex_markets(data_dir: pathlib.Path) -> Dict[str, Any]:
    payload = read_json_file(pathlib.Path(data_dir) / "normalized" / "cex" / "latest_markets.json", None)
    if not isinstance(payload, dict):
        raise FileNotFoundError(str(pathlib.Path(data_dir) / "normalized" / "cex" / "latest_markets.json"))
    markets = payload.get("markets")
    if not isinstance(markets, list) or not markets:
        raise CexArenaError("CEX latest_markets is empty")
    return payload


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None


def cex_data_quality_gate(data_dir: pathlib.Path, *, max_stale_seconds: int = 180, now_value: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    reasons: List[str] = []
    stop_reasons: List[str] = []
    try:
        assert_public_data_only(env)
    except CexSafetyError as exc:
        stop_reasons.append(str(exc))
    health = read_json_file(pathlib.Path(data_dir) / "reports" / "cex_recorder_health.json", {})
    latest = read_json_file(pathlib.Path(data_dir) / "normalized" / "cex" / "latest_markets.json", {})
    markets = latest.get("markets") if isinstance(latest, dict) else None
    if not isinstance(health, dict) or not health:
        reasons.append("missing_cex_recorder_health")
    elif health.get("ok") is not True:
        reasons.append("cex_recorder_health_not_ok")
    if not isinstance(markets, list) or not markets:
        reasons.append("missing_cex_latest_markets")
        markets = []
    valid_markets = [m for m in markets if isinstance(m, dict) and _valid_frame(m)]
    if not valid_markets:
        reasons.append("no_valid_cex_markets")
    ts_value = (latest or {}).get("ts") or (health or {}).get("ts")
    ts = _parse_ts(ts_value)
    now_ts = _parse_ts(now_value or utc_now())
    age_seconds = None
    if ts and now_ts:
        age_seconds = max(0.0, (now_ts - ts).total_seconds())
        if age_seconds > max_stale_seconds:
            reasons.append("cex_recorder_stale")
    else:
        reasons.append("cex_recorder_ts_invalid")
    stop_service = bool(stop_reasons)
    trade_allowed = bool(valid_markets and not reasons and not stop_service)
    decision = "STOP_SERVICE" if stop_service else "TRADE_ALLOWED" if trade_allowed else "HOLD_ONLY"
    return {
        "ok": trade_allowed,
        "decision": decision,
        "trade_allowed": trade_allowed,
        "hold_only": not trade_allowed and not stop_service,
        "stop_service": stop_service,
        "reason_codes": reasons + stop_reasons,
        "recorder_age_seconds": None if age_seconds is None else round(age_seconds, 3),
        "market_count": len(markets),
        "orderable_market_count": len(valid_markets),
        "book_coverage": (latest or {}).get("book_coverage") if isinstance(latest, dict) else {},
        "source": "cex_public_api_v0",
    }


BASELINE_STRATEGIES = ("momentum", "mean_reversion", "volatility_breakout", "funding_filter", "grid_shadow")


def _closes(frame: Mapping[str, Any]) -> List[float]:
    values: List[float] = []
    for candle in frame.get("candles", []) if isinstance(frame.get("candles"), list) else []:
        if not isinstance(candle, Mapping):
            continue
        close = to_float(candle.get("close"), float("nan"))
        if math.isfinite(close) and close > 0:
            values.append(close)
    if not values:
        values.append(require_positive(frame.get("last"), "last"))
    return values


def _position_qty(account: Mapping[str, Any], frame: Mapping[str, Any]) -> float:
    pos = ((account.get("positions") or {}) if isinstance(account.get("positions"), dict) else {}).get(_position_key(frame), {})
    return to_float(pos.get("qty") if isinstance(pos, Mapping) else 0.0, 0.0)


def _decision(strategy: str, decision: str, frame: Mapping[str, Any], *, reason: str, notional: float, reduce_only: bool = False) -> Dict[str, Any]:
    orders: List[Dict[str, Any]] = []
    if decision == "buy":
        orders.append({"side": "buy", "notional": round(notional, 6), "strategy": strategy, "rationale": reason})
    elif decision == "sell":
        orders.append({"side": "sell", "notional": round(notional, 6), "strategy": strategy, "reduce_only": True, "rationale": reason})
    return {
        "strategy": strategy,
        "decision": decision,
        "orders": orders,
        "paper_only": True,
        "market_id": frame.get("market_id") or _position_key(frame),
        "exchange": frame.get("exchange"),
        "symbol": frame.get("symbol"),
        "reason": reason,
        "reduce_only": reduce_only,
    }


def baseline_strategy_decisions(frame: Mapping[str, Any], account: Mapping[str, Any], *, notional: float = 50.0) -> List[Dict[str, Any]]:
    closes = _closes(frame)
    last = require_positive(frame.get("last"), "last")
    held = _position_qty(account, frame)
    avg = sum(closes[-20:]) / min(len(closes), 20)
    prev = closes[-2] if len(closes) >= 2 else closes[-1]
    ret = (last - prev) / prev if prev else 0.0
    window = closes[-20:]
    vol = 0.0
    if len(window) >= 3:
        returns = [(window[i] - window[i - 1]) / window[i - 1] for i in range(1, len(window)) if window[i - 1] > 0]
        mean = sum(returns) / len(returns) if returns else 0.0
        vol = math.sqrt(sum((x - mean) ** 2 for x in returns) / len(returns)) if returns else 0.0
    decisions: List[Dict[str, Any]] = []
    decisions.append(_decision("momentum", "buy" if ret > 0.001 else "sell" if held > 0 and ret < -0.001 else "hold", frame, reason=f"1m momentum ret={ret:.5f}", notional=notional, reduce_only=held > 0))
    mean_gap = (last - avg) / avg if avg else 0.0
    decisions.append(_decision("mean_reversion", "buy" if mean_gap < -0.003 else "sell" if held > 0 and mean_gap > 0.004 else "hold", frame, reason=f"mean gap={mean_gap:.5f}", notional=notional, reduce_only=held > 0))
    prior = closes[:-1] or closes
    prior_high = max(prior)
    prior_low = min(prior)
    breakout = "buy" if last > prior_high * 1.001 else "sell" if held > 0 and last < prior_low * 0.999 else "hold"
    decisions.append(_decision("volatility_breakout", breakout, frame, reason=f"vol={vol:.5f} prior_high={prior_high:.4f} prior_low={prior_low:.4f}", notional=notional, reduce_only=held > 0))
    funding = to_float(frame.get("funding_rate"), 0.0)
    funding_decision = "buy" if funding < -0.0002 else "sell" if held > 0 and funding > 0.0005 else "hold"
    decisions.append(_decision("funding_filter", funding_decision, frame, reason=f"funding={funding:.6f}", notional=notional, reduce_only=held > 0))
    grid_step = max(0.002, min(0.01, vol * 2.0 if vol else 0.004))
    nearest_lower = avg * (1.0 - grid_step)
    nearest_upper = avg * (1.0 + grid_step)
    grid_decision = "buy" if last < nearest_lower else "sell" if held > 0 and last > nearest_upper else "hold"
    decisions.append(_decision("grid_shadow", grid_decision, frame, reason=f"grid_step={grid_step:.5f} avg={avg:.4f}", notional=notional, reduce_only=held > 0))
    return decisions


def choose_primary_frame(markets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    valid = [dict(m) for m in markets if isinstance(m, Mapping) and _valid_frame(m)]
    if not valid:
        raise CexArenaError("no valid CEX frame available")
    valid.sort(key=lambda m: (0 if "BTC" in str(m.get("symbol", "")).upper() else 1, str(m.get("symbol", ""))))
    return valid[0]


def _state_path(data_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / "cex_paper" / "state.json"


def load_cex_state(data_dir: pathlib.Path, strategies: Sequence[str]) -> Dict[str, Any]:
    path = _state_path(pathlib.Path(data_dir))
    state = read_json_file(path, None)
    if not isinstance(state, dict):
        state = {"schema_version": "cex_paper_state_v1", "source": "cex_public_api_v0", "accounts": {}, "created_at": utc_now()}
    accounts = state.setdefault("accounts", {})
    for strategy in strategies:
        if strategy not in accounts or not isinstance(accounts.get(strategy), dict):
            accounts[strategy] = new_exchange_account(strategy)
    return state


def append_risk_ledger(data_dir: pathlib.Path, event: Mapping[str, Any]) -> Dict[str, Any]:
    path = pathlib.Path(data_dir) / "cex_paper" / "risk_ledger.jsonl"
    previous = tail_jsonl(path, limit=1)
    prev_hash = str(previous[-1].get("entry_sha256") or "GENESIS") if previous else "GENESIS"
    sequence = int(previous[-1].get("sequence", 0)) + 1 if previous else 1
    payload = dict(event)
    payload.update({"sequence": sequence, "prev_ledger_sha256": prev_hash})
    entry_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    payload["entry_sha256"] = entry_hash
    append_jsonl(path, payload)
    return payload


def write_cex_runtime_status(data_dir: pathlib.Path, tick: Mapping[str, Any], gate: Mapping[str, Any]) -> Dict[str, Any]:
    state = read_json_file(_state_path(pathlib.Path(data_dir)), {})
    risk_rows = len(tail_jsonl(pathlib.Path(data_dir) / "cex_paper" / "risk_ledger.jsonl", limit=500))
    completion_state = "runtime-complete" if gate.get("decision") == "TRADE_ALLOWED" and risk_rows > 0 else "code-complete-only"
    status = {
        "ok": completion_state == "runtime-complete",
        "completion_state": completion_state,
        "source": "cex_public_api_v0",
        "runner": tick.get("runner"),
        "latest_tick_id": tick.get("tick_id"),
        "latest_tick_ts": tick.get("ts"),
        "market_count": tick.get("market_count"),
        "data_quality_gate": dict(gate),
        "paper_only": True,
        "accounts": {agent: {"cash": round(to_float(acct.get("cash")), 6), "positions": len(acct.get("positions", {}) if isinstance(acct.get("positions"), dict) else {}), "trades": len(acct.get("trades", []) if isinstance(acct.get("trades"), list) else [])} for agent, acct in ((state.get("accounts") or {}) if isinstance(state, dict) else {}).items() if isinstance(acct, dict)},
        "risk_ledger_rows_sampled": risk_rows,
    }
    atomic_write_json(pathlib.Path(data_dir) / "reports" / "cex_runtime_status.json", status)
    return status


def run_cex_tick(
    data_dir: pathlib.Path,
    *,
    strategies: Optional[Sequence[str]] = None,
    notional: float = 50.0,
    max_orders: int = 1,
    max_stale_seconds: int = 420,
    now: Callable[[], str] = utc_now,
) -> Dict[str, Any]:
    assert_public_data_only()
    data_dir = pathlib.Path(data_dir)
    strategies = tuple(str(s) for s in (strategies or BASELINE_STRATEGIES) if str(s) in BASELINE_STRATEGIES)
    if not strategies:
        strategies = BASELINE_STRATEGIES
    latest = load_latest_cex_markets(data_dir)
    markets = latest.get("markets", [])
    frame = choose_primary_frame(markets)
    ts = now()
    gate = cex_data_quality_gate(data_dir, max_stale_seconds=max_stale_seconds, now_value=ts)
    state = load_cex_state(data_dir, strategies)
    agents: Dict[str, Any] = {}
    decisions_for_log: List[Dict[str, Any]] = []
    applied = False
    for strategy in strategies:
        account = state["accounts"][strategy]
        all_decisions = baseline_strategy_decisions(frame, account, notional=notional)
        decision = next((d for d in all_decisions if d.get("strategy") == strategy), all_decisions[0])
        result = {"ok": False, "reason": "hold"}
        if gate.get("trade_allowed") is True:
            for order in decision.get("orders", [])[: max(0, int(max_orders))]:
                result = apply_paper_order(account, frame, order, now=now)
                if result.get("ok"):
                    applied = True
                    break
        else:
            decision = dict(decision)
            decision["orders"] = []
            decision["decision"] = "hold"
            result = {"ok": False, "reason": "gate_hold_only"}
        row = {"ts": ts, "agent_id": strategy, "strategy": strategy, "decision": decision, "result": result, "paper_only": True}
        append_jsonl(data_dir / "cex_paper" / "decisions.jsonl", row)
        decisions_for_log.append(row)
        agents[strategy] = {"decision": decision, "result": result}
    state["updated_at"] = ts
    state["latest_markets"] = latest
    atomic_write_json(_state_path(data_dir), state)
    tick_id = "cex-" + hashlib.sha256((ts + json.dumps(decisions_for_log, sort_keys=True, default=str)).encode("utf-8")).hexdigest()[:12]
    tick = {
        "ok": True,
        "ts": ts,
        "tick_id": tick_id,
        "runner": "cex_resident_mechanical_bot_loop",
        "source": "cex_public_api_v0",
        "mode": "paper_apply",
        "effective_mode": "paper_apply" if gate.get("trade_allowed") else "hold_only",
        "applied": applied,
        "market_count": len(markets),
        "primary_market": {"exchange": frame.get("exchange"), "symbol": frame.get("symbol"), "last": frame.get("last"), "spread_bps": frame.get("spread_bps"), "funding_rate": frame.get("funding_rate")},
        "data_quality_gate": gate,
        "agents": agents,
    }
    append_jsonl(data_dir / "cex_paper" / "ticks.jsonl", tick)
    append_risk_ledger(data_dir, {"ts": ts, "tick_id": tick_id, "event": "cex_tick", "gate_decision": gate.get("decision"), "applied": applied, "market_count": len(markets), "paper_only": True})
    write_cex_runtime_status(data_dir, tick, gate)
    return tick


def fetch_frames(exchange_symbols: Sequence[str], *, fetcher: JsonFetcher = fetch_json, now: Callable[[], str] = utc_now, timeout: float = 12.0) -> List[Dict[str, Any]]:
    frames = []
    for item in exchange_symbols:
        if ":" in item:
            exchange, symbol = item.split(":", 1)
        else:
            exchange, symbol = "okx", item
        frames.append(fetch_exchange_snapshot(exchange.strip(), symbol.strip(), fetcher=fetcher, now=now, timeout=timeout))
    return frames


def command_snapshot(args: argparse.Namespace) -> None:
    frame = fetch_exchange_snapshot(args.exchange, args.symbol)
    if args.data_dir:
        record_snapshot(pathlib.Path(args.data_dir), frame)
    if args.output:
        atomic_write_json(pathlib.Path(args.output), frame)
    print(json.dumps(frame, ensure_ascii=False, indent=2, sort_keys=True))


def _split_exchange_symbols(value: str) -> List[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def command_record_once(args: argparse.Namespace) -> None:
    symbols = _split_exchange_symbols(args.markets)
    frames = fetch_frames(symbols, timeout=args.timeout)
    health = record_frames(pathlib.Path(args.data_dir), frames)
    print(json.dumps({"ok": health.get("ok") is True, "health": health, "market_count": len(frames)}, ensure_ascii=False, indent=2, sort_keys=True))


def command_recorder_loop(args: argparse.Namespace) -> None:
    interval = max(5.0, float(args.interval_sec))
    while True:
        try:
            command_record_once(args)
        except Exception as exc:
            print(json.dumps({"ok": False, "source": "cex_public_api_v0", "error": str(exc), "ts": utc_now()}, ensure_ascii=False), file=sys.stderr, flush=True)
        if args.once:
            break
        time.sleep(interval)


def command_tick(args: argparse.Namespace) -> None:
    strategies = [item.strip() for item in str(args.strategies).split(",") if item.strip()]
    tick = run_cex_tick(pathlib.Path(args.data_dir), strategies=strategies, notional=args.notional, max_orders=args.max_orders, max_stale_seconds=args.max_stale_seconds)
    print(json.dumps(tick, ensure_ascii=False, indent=2, sort_keys=True))


def command_runtime_gate(args: argparse.Namespace) -> None:
    gate = cex_data_quality_gate(pathlib.Path(args.data_dir), max_stale_seconds=args.max_stale_seconds)
    status = write_cex_runtime_status(pathlib.Path(args.data_dir), {"runner": "cex_runtime_gate", "ts": utc_now(), "tick_id": "manual", "market_count": gate.get("market_count")}, gate)
    print(json.dumps({"gate": gate, "status": status}, ensure_ascii=False, indent=2, sort_keys=True))


def command_init(args: argparse.Namespace) -> None:
    account = new_exchange_account(args.agent, starting_cash=args.cash)
    atomic_write_json(pathlib.Path(args.output), account)
    print(json.dumps({"ok": True, "path": args.output, "agent_id": args.agent}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aurum CEX public-data paper arena")
    sub = parser.add_subparsers(dest="command", required=True)
    p_snapshot = sub.add_parser("snapshot")
    p_snapshot.add_argument("--exchange", default="okx", choices=["okx", "bybit", "binance"])
    p_snapshot.add_argument("--symbol", default="BTC-USDT-SWAP")
    p_snapshot.add_argument("--data-dir", default="", help="optional Aurum data dir for recorder-style normalized/cex output")
    p_snapshot.add_argument("--output", default="")
    p_snapshot.set_defaults(func=command_snapshot)
    p_record = sub.add_parser("record-once")
    p_record.add_argument("--markets", default="okx:BTC-USDT-SWAP,okx:ETH-USDT-SWAP", help="comma-separated exchange:symbol list")
    p_record.add_argument("--data-dir", default="data/cex_arena")
    p_record.add_argument("--timeout", type=float, default=12.0)
    p_record.set_defaults(func=command_record_once)
    p_loop = sub.add_parser("recorder-loop")
    p_loop.add_argument("--markets", default="okx:BTC-USDT-SWAP,okx:ETH-USDT-SWAP")
    p_loop.add_argument("--data-dir", default="data/cex_arena")
    p_loop.add_argument("--timeout", type=float, default=12.0)
    p_loop.add_argument("--interval-sec", type=float, default=30.0)
    p_loop.add_argument("--once", action="store_true")
    p_loop.set_defaults(func=command_recorder_loop)
    p_tick = sub.add_parser("tick")
    p_tick.add_argument("--data-dir", default="data/cex_arena")
    p_tick.add_argument("--strategies", default=",".join(BASELINE_STRATEGIES))
    p_tick.add_argument("--notional", type=float, default=50.0)
    p_tick.add_argument("--max-orders", type=int, default=1)
    p_tick.add_argument("--max-stale-seconds", type=int, default=420)
    p_tick.set_defaults(func=command_tick)
    p_gate = sub.add_parser("runtime-gate")
    p_gate.add_argument("--data-dir", default="data/cex_arena")
    p_gate.add_argument("--max-stale-seconds", type=int, default=180)
    p_gate.set_defaults(func=command_runtime_gate)
    p_init = sub.add_parser("init-account")
    p_init.add_argument("--agent", default="superwing")
    p_init.add_argument("--cash", type=float, default=1500.0)
    p_init.add_argument("--output", default="data/cex_paper/account.json")
    p_init.set_defaults(func=command_init)
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
