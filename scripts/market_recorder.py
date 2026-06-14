#!/usr/bin/env python3
"""Polymarket public market-data recorder for Aurum.

Read-only recorder: captures public Gamma/CLOB/Data API frames, selected CLOB
order books, append-only raw JSONL, and a hash-chained manifest. It does not
authenticate, place orders, read wallets, or require private keys.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&search=bitcoin"
DEFAULT_CLOB_MARKETS_URL = "https://clob.polymarket.com/markets?limit=100"
DEFAULT_DATA_TRADES_URL = "https://data-api.polymarket.com/trades?limit=100"
DEFAULT_CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"
USER_AGENT = "aurum-market-recorder/0.1 (+public-read-only)"
BITCOIN_TERMS = ("bitcoin", "btc", "satoshi")

_STOP = False

JsonFetcher = Callable[..., Any]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def fetch_json_url(url: str, timeout: float = 12.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
    )
    # Default to the process network environment so local Hermes can use its
    # configured proxy, while VPS direct mode still works when no proxy exists.
    if os.environ.get("AURUM_RECORDER_NO_PROXY", "").lower() in {"1", "true", "yes"}:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def fetch_json_curl(url: str, timeout: float = 12.0) -> Any:
    proc = subprocess.run(
        ["curl", "-fsS", "--max-time", str(int(max(1, timeout))), "-A", USER_AGENT, url],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return json.loads(proc.stdout)


def default_fetch_json(url: str, timeout: float = 12.0) -> Any:
    try:
        return fetch_json_url(url, timeout=timeout)
    except Exception:
        if os.environ.get("AURUM_RECORDER_DISABLE_CURL_FALLBACK", "").lower() in {"1", "true", "yes"}:
            raise
        return fetch_json_curl(url, timeout=timeout)


def raw_day_dir(data_dir: pathlib.Path, ts: str) -> pathlib.Path:
    day = parse_ts(ts).date().isoformat()
    return data_dir / "raw" / "polymarket" / day


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> Tuple[pathlib.Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(record)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path, sha256_text(line)


def last_manifest_hash(manifest_path: pathlib.Path) -> str:
    if not manifest_path.exists():
        return ""
    last = ""
    with manifest_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                last = line
    if not last:
        return ""
    try:
        return str(json.loads(last).get("manifest_sha256") or "")
    except Exception:
        return ""


def manifest_count(manifest_path: pathlib.Path) -> int:
    if not manifest_path.exists():
        return 0
    with manifest_path.open("r", encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f if line.strip())


def raw_file_stem(source: str) -> str:
    # Keep the source semantic singular while using a natural plural file for
    # a stream of many book frames.
    return "clob_books" if source == "clob_book" else source


def append_frame(
    data_dir: pathlib.Path,
    ts: str,
    source: str,
    url: str,
    payload: Any,
    ok: bool = True,
    status: str = "ok",
    elapsed_ms: int = 0,
) -> Dict[str, Any]:
    day_dir = raw_day_dir(data_dir, ts)
    payload_sha = sha256_text(canonical_json(payload))
    frame = {
        "ts": ts,
        "source": source,
        "url": url,
        "ok": bool(ok),
        "status": status,
        "elapsed_ms": int(elapsed_ms),
        "payload_sha256": payload_sha,
        "payload": payload,
    }
    frame_path, line_sha = append_jsonl(day_dir / f"{raw_file_stem(source)}.jsonl", frame)
    manifest_path = day_dir / "manifest.jsonl"
    prev = last_manifest_hash(manifest_path)
    manifest = {
        "sequence": manifest_count(manifest_path) + 1,
        "ts": ts,
        "source": source,
        "path": str(frame_path.relative_to(data_dir)),
        "line_sha256": line_sha,
        "payload_sha256": payload_sha,
        "prev_manifest_sha256": prev,
    }
    manifest["manifest_sha256"] = sha256_text(canonical_json(manifest))
    append_jsonl(manifest_path, manifest)
    return manifest


def safe_fetch(fetcher: JsonFetcher, url: str, timeout: float) -> Tuple[bool, Any, str, int]:
    started = time.perf_counter()
    try:
        payload = fetcher(url, timeout=timeout)
        elapsed = int((time.perf_counter() - started) * 1000)
        return True, payload, "ok", elapsed
    except TypeError:
        # Test fetchers may not accept timeout.
        try:
            payload = fetcher(url)
            elapsed = int((time.perf_counter() - started) * 1000)
            return True, payload, "ok", elapsed
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return False, {"error": type(exc).__name__, "message": str(exc)[:500]}, type(exc).__name__, elapsed
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return False, {"error": type(exc).__name__, "message": str(exc)[:500]}, type(exc).__name__, elapsed


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def market_items(payload: Any) -> List[Dict[str, Any]]:
    payload = jsonish(payload)
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    out: List[Dict[str, Any]] = []
    for key in ("markets", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            out.extend(x for x in value if isinstance(x, dict))
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets", []) or []:
                if isinstance(market, dict):
                    out.append(market)
    return out


def normalize_market(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    outcomes = jsonish(raw.get("outcomes")) or []
    prices = jsonish(raw.get("outcomePrices")) or jsonish(raw.get("outcome_prices")) or []
    token_ids = jsonish(raw.get("clobTokenIds")) or jsonish(raw.get("clob_token_ids")) or []
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
                "token_id": str(token_ids[idx]) if isinstance(token_ids, list) and idx < len(token_ids) else "",
            }
        )
    market_id = str(raw.get("id") or raw.get("conditionId") or raw.get("condition_id") or raw.get("slug") or "")
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
        "volume": to_float(raw.get("volume") or raw.get("volumeNum") or raw.get("volume24hr") or raw.get("volume24hrClob"), 0.0),
        "liquidity": to_float(raw.get("liquidity") or raw.get("liquidityNum"), 0.0),
        "end_date": raw.get("endDate") or raw.get("end_date") or raw.get("endDateIso") or "",
        "outcomes": norm_outcomes,
    }


def market_matches_universe(market: Dict[str, Any], universe: str = "bitcoin") -> bool:
    universe = (universe or "").strip().lower()
    if not universe or universe in {"all", "any", "general"}:
        return True
    text = " ".join(str(market.get(k, "")) for k in ("question", "slug", "category", "fee_category", "market_id", "condition_id")).lower()
    if universe in {"bitcoin", "btc"}:
        return any(term in text for term in BITCOIN_TERMS)
    return universe in text


def normalize_markets(payload: Any, universe: str = "bitcoin") -> List[Dict[str, Any]]:
    markets = []
    for item in market_items(payload):
        market = normalize_market(item)
        if market and market_matches_universe(market, universe):
            markets.append(market)
    markets.sort(key=lambda m: (m.get("volume", 0.0), m.get("liquidity", 0.0)), reverse=True)
    return markets


def extract_token_ids_from_market(market: Dict[str, Any]) -> List[str]:
    token_ids: List[str] = []
    for key in ("clobTokenIds", "clob_token_ids", "tokenIds", "token_ids"):
        value = jsonish(market.get(key))
        if isinstance(value, list):
            token_ids.extend(str(x) for x in value if str(x))
    tokens = jsonish(market.get("tokens"))
    if isinstance(tokens, list):
        for token in tokens:
            if isinstance(token, dict):
                tid = token.get("token_id") or token.get("tokenId") or token.get("id")
                if tid:
                    token_ids.append(str(tid))
    outcomes = jsonish(market.get("outcomes"))
    if isinstance(outcomes, list):
        for outcome in outcomes:
            if isinstance(outcome, dict):
                tid = outcome.get("token_id") or outcome.get("tokenId")
                if tid:
                    token_ids.append(str(tid))
    return token_ids


def extract_token_ids(*payloads: Any, limit: int) -> List[str]:
    seen = set()
    out = []
    for payload in payloads:
        for market in market_items(payload):
            for token_id in extract_token_ids_from_market(market):
                if token_id and token_id not in seen:
                    seen.add(token_id)
                    out.append(token_id)
                    if len(out) >= limit:
                        return out
    return out


def capture_source(
    data_dir: pathlib.Path,
    ts: str,
    source: str,
    url: str,
    fetcher: JsonFetcher,
    timeout: float,
) -> Tuple[Dict[str, Any], Any, bool]:
    ok, payload, status, elapsed_ms = safe_fetch(fetcher, url, timeout)
    manifest = append_frame(data_dir, ts, source, url, payload, ok=ok, status=status, elapsed_ms=elapsed_ms)
    return manifest, payload, ok


def capture_once(
    data_dir: pathlib.Path,
    fetcher: JsonFetcher = default_fetch_json,
    now: Callable[[], str] = utc_now,
    max_books: int = 10,
    timeout: float = 12.0,
    gamma_url: str = DEFAULT_GAMMA_URL,
    clob_markets_url: str = DEFAULT_CLOB_MARKETS_URL,
    data_trades_url: str = DEFAULT_DATA_TRADES_URL,
    clob_book_url: str = DEFAULT_CLOB_BOOK_URL,
    universe: str = "bitcoin",
) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    ts = now()
    (data_dir / "raw" / "polymarket").mkdir(parents=True, exist_ok=True)
    (data_dir / "normalized" / "polymarket").mkdir(parents=True, exist_ok=True)
    (data_dir / "features").mkdir(parents=True, exist_ok=True)
    (data_dir / "reports").mkdir(parents=True, exist_ok=True)

    sources: Dict[str, Dict[str, Any]] = {}
    manifest, gamma_payload, gamma_ok = capture_source(data_dir, ts, "gamma_markets", gamma_url, fetcher, timeout)
    sources["gamma_markets"] = {"ok_frames": 1 if gamma_ok else 0, "errors": 0 if gamma_ok else 1, "last_manifest_sha256": manifest["manifest_sha256"]}
    manifest, clob_payload, clob_ok = capture_source(data_dir, ts, "clob_markets", clob_markets_url, fetcher, timeout)
    sources["clob_markets"] = {"ok_frames": 1 if clob_ok else 0, "errors": 0 if clob_ok else 1, "last_manifest_sha256": manifest["manifest_sha256"]}
    manifest, trades_payload, trades_ok = capture_source(data_dir, ts, "data_trades", data_trades_url, fetcher, timeout)
    sources["data_trades"] = {"ok_frames": 1 if trades_ok else 0, "errors": 0 if trades_ok else 1, "last_manifest_sha256": manifest["manifest_sha256"]}

    markets = normalize_markets(gamma_payload, universe=universe)
    token_ids = []
    seen_tokens = set()
    for market in markets:
        for outcome in market.get("outcomes", []) or []:
            token_id = str((outcome or {}).get("token_id") or "")
            if token_id and token_id not in seen_tokens:
                seen_tokens.add(token_id)
                token_ids.append(token_id)
            if len(token_ids) >= max(0, int(max_books)):
                break
        if len(token_ids) >= max(0, int(max_books)):
            break
    if not token_ids:
        token_ids = extract_token_ids(gamma_payload, clob_payload, limit=max(0, int(max_books)))
    book_ok = 0
    book_errors = 0
    for token_id in token_ids:
        url = clob_book_url.format(token_id=urllib.parse.quote(str(token_id), safe=""))
        ok, payload, status, elapsed_ms = safe_fetch(fetcher, url, timeout)
        append_frame(data_dir, ts, "clob_book", url, {"token_id": token_id, "book": payload}, ok=ok, status=status, elapsed_ms=elapsed_ms)
        if ok:
            book_ok += 1
        else:
            book_errors += 1
    sources["clob_book"] = {"ok_frames": book_ok, "errors": book_errors, "requested_tokens": len(token_ids)}

    trades = trades_payload if isinstance(trades_payload, list) else market_items(trades_payload)
    summary_ok = bool(gamma_ok and clob_ok and trades_ok and markets and book_ok > 0)
    summary = {
        "ok": summary_ok,
        "ts": ts,
        "recorder": "polymarket_market_recorder_v0",
        "universe": universe,
        "market_count": len(markets),
        "token_count": len(token_ids),
        "trade_count": len(trades) if isinstance(trades, list) else 0,
        "sources": sources,
        "raw_day_dir": str(raw_day_dir(data_dir, ts)),
    }
    (data_dir / "normalized" / "polymarket" / "latest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    latest_markets = {"ts": ts, "source": "polymarket_market_recorder_v0", "markets": markets}
    (data_dir / "normalized" / "polymarket" / "latest_markets.json").write_text(
        json.dumps(latest_markets, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (data_dir / "reports" / "market_recorder_health.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def load_latest_markets(
    data_dir: pathlib.Path,
    now: Callable[[], str] = utc_now,
    max_stale_seconds: int = 180,
) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    path = data_dir / "normalized" / "polymarket" / "latest_markets.json"
    if not path.exists():
        raise FileNotFoundError(str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    ts = parse_ts(str(payload.get("ts") or ""))
    age = (parse_ts(now()) - ts).total_seconds()
    if age > max_stale_seconds:
        raise RuntimeError(f"recorder markets stale: age_seconds={age:.1f}")
    markets = payload.get("markets")
    if not isinstance(markets, list) or not markets:
        raise RuntimeError("recorder latest_markets is empty")
    return payload


def recorder_health(
    data_dir: pathlib.Path,
    now: Callable[[], str] = utc_now,
    max_stale_seconds: int = 180,
) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    path = data_dir / "reports" / "market_recorder_health.json"
    out: Dict[str, Any] = {"ok": True, "errors": []}
    if not path.exists():
        return {"ok": False, "errors": ["missing_health_report"]}
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
        ts = parse_ts(str(health.get("ts") or ""))
        current = parse_ts(now())
        age = (current - ts).total_seconds()
        out["last_capture"] = health
        out["age_seconds"] = round(age, 1)
        if not health.get("ok"):
            out["errors"].append("last_capture_not_ok")
        if age > max_stale_seconds:
            out["errors"].append("recorder_stale")
        sources = health.get("sources", {}) if isinstance(health.get("sources"), dict) else {}
        for source in ("gamma_markets", "clob_markets", "data_trades", "clob_book"):
            detail = sources.get(source)
            if not isinstance(detail, dict):
                out["errors"].append(f"missing_source:{source}")
            elif int(detail.get("ok_frames") or 0) <= 0:
                out["errors"].append(f"source_not_ok:{source}")
        out["ok"] = not out["errors"]
        return out
    except Exception as exc:
        return {"ok": False, "errors": ["health_parse:" + type(exc).__name__]}


def env_url(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aurum Polymarket public market-data recorder")
    p.add_argument("--data-dir", default=os.environ.get("AURUM_DATA_DIR", "/opt/aurum/data"))
    p.add_argument("--interval-sec", type=int, default=int(os.environ.get("AURUM_RECORDER_INTERVAL_SEC", "30")))
    p.add_argument("--once", action="store_true")
    p.add_argument("--health", action="store_true")
    p.add_argument("--max-books", type=int, default=int(os.environ.get("AURUM_RECORDER_MAX_BOOKS", "10")))
    p.add_argument("--max-stale-seconds", type=int, default=int(os.environ.get("AURUM_RECORDER_MAX_STALE_SECONDS", "180")))
    p.add_argument("--timeout", type=float, default=float(os.environ.get("AURUM_RECORDER_TIMEOUT", "12")))
    p.add_argument("--universe", default=os.environ.get("AURUM_DUEL_UNIVERSE", "bitcoin"))
    p.add_argument("--gamma-url", default=env_url("AURUM_RECORDER_GAMMA_URL", DEFAULT_GAMMA_URL))
    p.add_argument("--clob-markets-url", default=env_url("AURUM_RECORDER_CLOB_MARKETS_URL", DEFAULT_CLOB_MARKETS_URL))
    p.add_argument("--data-trades-url", default=env_url("AURUM_RECORDER_DATA_TRADES_URL", DEFAULT_DATA_TRADES_URL))
    p.add_argument("--clob-book-url", default=env_url("AURUM_RECORDER_CLOB_BOOK_URL", DEFAULT_CLOB_BOOK_URL))
    return p


def _stop(_signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    data_dir = pathlib.Path(args.data_dir)
    if args.health:
        print(json.dumps(recorder_health(data_dir, max_stale_seconds=args.max_stale_seconds), ensure_ascii=False, sort_keys=True))
        return 0

    def run_one() -> Dict[str, Any]:
        return capture_once(
            data_dir,
            max_books=args.max_books,
            timeout=args.timeout,
            gamma_url=args.gamma_url,
            clob_markets_url=args.clob_markets_url,
            data_trades_url=args.data_trades_url,
            clob_book_url=args.clob_book_url,
            universe=args.universe,
        )

    if args.once:
        print(json.dumps(run_one(), ensure_ascii=False, sort_keys=True))
        return 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = max(10, int(args.interval_sec))
    while not _STOP:
        try:
            result = run_one()
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        except Exception as exc:
            error = {"ok": False, "ts": utc_now(), "recorder": "polymarket_market_recorder_v0", "error": type(exc).__name__, "message": str(exc)[:500]}
            append_frame(data_dir, error["ts"], "recorder_error", "internal", error, ok=False, status=type(exc).__name__)
            print(json.dumps(error, ensure_ascii=False, sort_keys=True), flush=True)
        for _ in range(interval):
            if _STOP:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
