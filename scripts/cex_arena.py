#!/usr/bin/env python3
"""CEX public-data adapter and exchange-like paper arena for Aurum.

This module is deliberately public-data/paper-only.  It normalizes exchange
REST snapshots into a small frame contract and applies local paper fills against
that frame.  It never signs, submits, cancels, or authenticates exchange orders.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import sys
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional

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

    data_dir = pathlib.Path(data_dir)
    ts = str(frame.get("ts") or utc_now())
    day = _day_from_ts(ts)
    raw_dir = data_dir / "raw" / "cex" / day
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "snapshots.jsonl"
    raw_record = {"ts": ts, "source": frame.get("source") or "cex_public_api_v0", "ok": True, "payload": dict(frame)}
    with raw_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(raw_record, ensure_ascii=False, sort_keys=True) + "\n")
    markets_payload = {
        "ts": ts,
        "source": "cex_public_api_v0",
        "market_source": "cex_public_api_v0",
        "exchange": frame.get("exchange"),
        "symbols": [frame.get("symbol")],
        "market_count": 1,
        "markets": [dict(frame)],
    }
    health = {
        "ts": ts,
        "ok": True,
        "source": "cex_public_api_v0",
        "latest_markets_path": "normalized/cex/latest_markets.json",
        "raw_path": str(raw_path.relative_to(data_dir)),
        "market_count": 1,
    }
    atomic_write_json(data_dir / "normalized" / "cex" / "latest_markets.json", markets_payload)
    atomic_write_json(data_dir / "reports" / "cex_recorder_health.json", health)
    return health


def command_snapshot(args: argparse.Namespace) -> None:
    frame = fetch_exchange_snapshot(args.exchange, args.symbol)
    if args.data_dir:
        record_snapshot(pathlib.Path(args.data_dir), frame)
    if args.output:
        atomic_write_json(pathlib.Path(args.output), frame)
    print(json.dumps(frame, ensure_ascii=False, indent=2, sort_keys=True))


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
