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
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&search=bitcoin"
DEFAULT_CLOB_MARKETS_URL = "https://clob.polymarket.com/markets?limit=100"
DEFAULT_DATA_TRADES_URL = "https://data-api.polymarket.com/trades?limit=100"
DEFAULT_CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"
USER_AGENT = "aurum-market-recorder/0.1 (+public-read-only)"
BITCOIN_TERMS = ("bitcoin", "btc", "satoshi")
RAW_SOURCE_STEMS = {
    "gamma_markets": "gamma_markets",
    "clob_markets": "clob_markets",
    "data_trades": "data_trades",
    "clob_book": "clob_books",
}

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


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: pathlib.Path, record: Dict[str, Any]) -> Tuple[pathlib.Path, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(record)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
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


def manifest_sequence(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        stripped = value.strip()
        return int(stripped) if stripped.isdigit() else None
    return None


def strict_positive_int(value: Any) -> Optional[int]:
    parsed = manifest_sequence(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def tail_text_lines(path: pathlib.Path, max_lines: Optional[int] = 200, chunk_size: int = 65536) -> List[str]:
    if max_lines is None:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    if max_lines <= 0:
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        chunks: List[bytes] = []
        newline_count = 0
        while pos > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return data.splitlines()[-max_lines:]


def iter_tail_text_lines(path: pathlib.Path, max_lines: Optional[int] = 200, chunk_size: int = 65536) -> Iterator[str]:
    if max_lines is None:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line.rstrip("\n")
        return
    if max_lines <= 0:
        return
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        pending = b""
        yielded = 0
        while pos > 0 and yielded < max_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + pending
            parts = data.split(b"\n")
            pending = parts[0]
            for raw_line in reversed(parts[1:]):
                if yielded >= max_lines:
                    break
                if not raw_line and pos + read_size == f.tell():
                    continue
                yield raw_line.decode("utf-8", errors="replace")
                yielded += 1
        if pending and yielded < max_lines:
            yield pending.decode("utf-8", errors="replace")


def raw_file_stem(source: str) -> str:
    # Keep the source semantic singular while using a natural plural file for
    # a stream of many book frames.
    return RAW_SOURCE_STEMS.get(source, source)


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


def verify_manifest(
    data_dir: pathlib.Path,
    ts: Optional[str] = None,
    max_rows: Optional[int] = 500,
    frame_tail_rows: Optional[int] = None,
) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    effective_frame_tail_rows = None if max_rows is None else (frame_tail_rows if frame_tail_rows is not None else max(2000, max_rows))
    day = parse_ts(ts).date().isoformat() if ts else dt.datetime.now(dt.timezone.utc).date().isoformat()
    manifest_path = data_dir / "raw" / "polymarket" / day / "manifest.jsonl"
    out: Dict[str, Any] = {
        "ok": True,
        "errors": [],
        "frames": 0,
        "verified_rows": 0,
        "latest_sequence": None,
        "manifest_path": str(manifest_path),
        "last_manifest_sha256": "",
        "verification_scope": "full" if max_rows is None else "tail",
        "max_rows": max_rows,
        "frame_tail_rows": effective_frame_tail_rows,
    }
    if not manifest_path.exists():
        out["ok"] = False
        out["errors"].append("missing_manifest")
        return out
    prev: Optional[str] = None
    expected_sequence: Optional[int] = None
    frame_cache: Dict[pathlib.Path, Optional[Dict[str, str]]] = {}
    needed_hashes_by_path: Dict[pathlib.Path, Set[str]] = {}

    def record_needed_frame_hashes(lines: List[str]) -> None:
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            source = str(row.get("source") or "")
            if source not in RAW_SOURCE_STEMS:
                continue
            rel_raw = str(row.get("path") or "")
            rel_path = pathlib.Path(rel_raw)
            expected_rel = pathlib.Path("raw") / "polymarket" / day / f"{RAW_SOURCE_STEMS[source]}.jsonl"
            if rel_path.is_absolute() or ".." in rel_path.parts or rel_path != expected_rel:
                continue
            line_hash = str(row.get("line_sha256") or "")
            if line_hash:
                needed_hashes_by_path.setdefault(rel_path, set()).add(line_hash)

    def frame_index(rel_path: pathlib.Path) -> Optional[Dict[str, str]]:
        if rel_path in frame_cache:
            return frame_cache[rel_path]
        frame_path = data_dir / rel_path
        if not frame_path.exists():
            frame_cache[rel_path] = None
            return None
        needed_hashes = needed_hashes_by_path.get(rel_path, set())
        index: Dict[str, str] = {}
        if not needed_hashes:
            frame_cache[rel_path] = index
            return index
        for frame_line in iter_tail_text_lines(frame_path, effective_frame_tail_rows):
            if not frame_line.strip():
                continue
            line_hash_value = sha256_text(frame_line)
            if line_hash_value not in needed_hashes:
                continue
            try:
                frame = json.loads(frame_line)
                index[line_hash_value] = str(frame.get("payload_sha256") or "")
            except Exception:
                index[line_hash_value] = "__FRAME_JSON_ERROR__"
            if needed_hashes.issubset(index):
                break
        frame_cache[rel_path] = index
        return index

    manifest_lines = tail_text_lines(manifest_path, None if max_rows is None else max_rows + 1)
    boundary_prev_hash: Optional[str] = None
    boundary_prev_sequence: Optional[int] = None
    if max_rows is not None and len(manifest_lines) > max_rows:
        boundary_line = manifest_lines[0]
        manifest_lines = manifest_lines[-max_rows:]
        try:
            boundary_row = json.loads(boundary_line)
            boundary_hash = str(boundary_row.get("manifest_sha256") or "")
            boundary_unsigned = {k: v for k, v in boundary_row.items() if k != "manifest_sha256"}
            if boundary_hash != sha256_text(canonical_json(boundary_unsigned)):
                out["errors"].append("manifest_boundary_hash_error")
            else:
                boundary_prev_hash = boundary_hash
            boundary_prev_sequence = manifest_sequence(boundary_row.get("sequence"))
            if boundary_prev_sequence is None:
                out["errors"].append("manifest_boundary_sequence_error")
        except Exception:
            out["errors"].append("manifest_boundary_json_error")
    record_needed_frame_hashes(manifest_lines)

    for line_no, line in enumerate(manifest_lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            out["errors"].append(f"manifest_json_error:{line_no}")
            continue
        sequence_int = manifest_sequence(row.get("sequence"))
        if expected_sequence is None:
            if boundary_prev_sequence is not None:
                expected_sequence = boundary_prev_sequence + 1
                prev = boundary_prev_hash
            else:
                expected_sequence = 1
                prev = ""
        if sequence_int != expected_sequence:
            out["errors"].append(f"manifest_sequence_error:{line_no}")
        if prev is not None and str(row.get("prev_manifest_sha256") or "") != prev:
            out["errors"].append(f"manifest_prev_hash_error:{line_no}")
        manifest_hash = str(row.get("manifest_sha256") or "")
        unsigned = {k: v for k, v in row.items() if k != "manifest_sha256"}
        if manifest_hash != sha256_text(canonical_json(unsigned)):
            out["errors"].append(f"manifest_hash_error:{line_no}")
        source = str(row.get("source") or "")
        if source not in RAW_SOURCE_STEMS:
            out["errors"].append(f"manifest_source_unexpected:{line_no}")
            prev = manifest_hash
            expected_sequence = (sequence_int + 1) if sequence_int is not None else ((expected_sequence or 0) + 1)
            out["verified_rows"] += 1
            out["frames"] = out["verified_rows"]
            out["latest_sequence"] = sequence_int if sequence_int is not None else out["latest_sequence"]
            out["last_manifest_sha256"] = manifest_hash
            continue
        rel_raw = str(row.get("path") or "")
        rel_path = pathlib.Path(rel_raw)
        expected_rel = pathlib.Path("raw") / "polymarket" / day / f"{RAW_SOURCE_STEMS[source]}.jsonl"
        if rel_path.is_absolute() or ".." in rel_path.parts:
            out["errors"].append(f"manifest_path_unsafe:{line_no}")
            prev = manifest_hash
            expected_sequence = (sequence_int + 1) if sequence_int is not None else ((expected_sequence or 0) + 1)
            out["verified_rows"] += 1
            out["frames"] = out["verified_rows"]
            out["latest_sequence"] = sequence_int if sequence_int is not None else out["latest_sequence"]
            out["last_manifest_sha256"] = manifest_hash
            continue
        if rel_path != expected_rel:
            out["errors"].append(f"manifest_path_unexpected:{line_no}")
            prev = manifest_hash
            expected_sequence = (sequence_int + 1) if sequence_int is not None else ((expected_sequence or 0) + 1)
            out["verified_rows"] += 1
            out["frames"] = out["verified_rows"]
            out["latest_sequence"] = sequence_int if sequence_int is not None else out["latest_sequence"]
            out["last_manifest_sha256"] = manifest_hash
            continue
        line_hash = str(row.get("line_sha256") or "")
        payload_hash = str(row.get("payload_sha256") or "")
        index = frame_index(rel_path)
        if index is None or not line_hash:
            out["errors"].append(f"manifest_frame_missing:{line_no}")
        else:
            found_payload_hash = index.get(line_hash)
            if found_payload_hash is None:
                out["errors"].append(f"manifest_frame_line_missing:{line_no}")
            elif found_payload_hash == "__FRAME_JSON_ERROR__":
                out["errors"].append(f"manifest_frame_json_error:{line_no}")
            elif found_payload_hash != payload_hash:
                out["errors"].append(f"manifest_payload_hash_mismatch:{line_no}")
        prev = manifest_hash
        expected_sequence = (sequence_int + 1) if sequence_int is not None else ((expected_sequence or 0) + 1)
        out["verified_rows"] += 1
        out["frames"] = out["verified_rows"]
        out["latest_sequence"] = sequence_int if sequence_int is not None else out["latest_sequence"]
        out["last_manifest_sha256"] = manifest_hash
    out["ok"] = not out["errors"] and out["verified_rows"] > 0
    return out


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


def _book_levels(book: Any, side: str) -> List[Tuple[float, float]]:
    payload = jsonish(book)
    if isinstance(payload, dict) and "book" in payload:
        payload = jsonish(payload.get("book"))
    if not isinstance(payload, dict):
        return []
    raw_levels = payload.get(side) or []
    levels: List[Tuple[float, float]] = []
    if not isinstance(raw_levels, list):
        return levels
    for level in raw_levels:
        price = size = None
        if isinstance(level, dict):
            price = level.get("price") or level.get("px")
            size = level.get("size") or level.get("qty") or level.get("quantity")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = level[0], level[1]
        try:
            if price is None or size is None:
                continue
            p = float(price)
            s = float(size)
        except Exception:
            continue
        if 0.0 < p < 1.0 and s > 0:
            levels.append((p, s))
    return levels


def best_bid_ask(book: Any) -> Dict[str, Any]:
    bids = _book_levels(book, "bids")
    asks = _book_levels(book, "asks")
    best_bid = max((p for p, _s in bids), default=None)
    best_ask = min((p for p, _s in asks), default=None)
    spread = round(best_ask - best_bid, 6) if best_bid is not None and best_ask is not None else None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_depth": round(sum(s for _p, s in bids), 6),
        "ask_depth": round(sum(s for _p, s in asks), 6),
        "orderable": best_bid is not None and best_ask is not None and best_bid < best_ask,
    }


def build_orderable_feed(
    markets: List[Dict[str, Any]],
    books_by_token: Dict[str, Any],
    ts: str,
    requested_token_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    requested_tokens = [str(token) for token in (requested_token_ids or []) if str(token)]
    requested_set = set(requested_tokens)
    rows: List[Dict[str, Any]] = []
    market_summaries: List[Dict[str, Any]] = []
    matched_requested: set[str] = set()
    for market in markets:
        market_requested = 0
        ok_tokens = 0
        orderable_tokens = 0
        for outcome in market.get("outcomes", []) or []:
            token_id = str((outcome or {}).get("token_id") or "")
            if not token_id:
                continue
            if requested_set and token_id not in requested_set:
                continue
            matched_requested.add(token_id)
            market_requested += 1
            book_ok = token_id in books_by_token
            quote = best_bid_ask(books_by_token.get(token_id)) if book_ok else {
                "best_bid": None,
                "best_ask": None,
                "spread": None,
                "bid_depth": 0.0,
                "ask_depth": 0.0,
                "orderable": False,
            }
            if book_ok:
                ok_tokens += 1
            if quote.get("orderable"):
                orderable_tokens += 1
            row = {
                "market_id": market.get("market_id"),
                "question": market.get("question"),
                "outcome": (outcome or {}).get("name"),
                "token_id": token_id,
                "price": (outcome or {}).get("price"),
                "book_ok": bool(book_ok),
                **quote,
            }
            rows.append(row)
        market_summaries.append(
            {
                "market_id": market.get("market_id"),
                "requested_tokens": market_requested,
                "ok_tokens": ok_tokens,
                "orderable_tokens": orderable_tokens,
                "orderable": market_requested > 0 and ok_tokens == market_requested and orderable_tokens == market_requested,
            }
        )
    requested = len(requested_set) if requested_set else sum(row["requested_tokens"] for row in market_summaries)
    ok_tokens = len([token for token in requested_set if token in books_by_token]) if requested_set else sum(row["ok_tokens"] for row in market_summaries)
    orderable_tokens = sum(row["orderable_tokens"] for row in market_summaries)
    unmatched_requested = sorted(requested_set - matched_requested)
    return {
        "ts": ts,
        "source": "polymarket_market_recorder_v0",
        "requested_tokens": requested,
        "ok_tokens": ok_tokens,
        "orderable_tokens": orderable_tokens,
        "coverage_ratio": round(ok_tokens / requested, 6) if requested else 0.0,
        "orderable_ratio": round(orderable_tokens / requested, 6) if requested else 0.0,
        "unmatched_requested_tokens": unmatched_requested,
        "markets": market_summaries,
        "rows": rows,
    }


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
    books_by_token: Dict[str, Any] = {}
    for token_id in token_ids:
        url = clob_book_url.format(token_id=urllib.parse.quote(str(token_id), safe=""))
        ok, payload, status, elapsed_ms = safe_fetch(fetcher, url, timeout)
        append_frame(data_dir, ts, "clob_book", url, {"token_id": token_id, "book": payload}, ok=ok, status=status, elapsed_ms=elapsed_ms)
        if ok:
            books_by_token[str(token_id)] = payload
            book_ok += 1
        else:
            book_errors += 1
    sources["clob_book"] = {"ok_frames": book_ok, "errors": book_errors, "requested_tokens": len(token_ids)}

    orderable_feed = build_orderable_feed(markets, books_by_token, ts, requested_token_ids=token_ids)
    atomic_write_json(data_dir / "features" / "polymarket_orderable_feed.json", orderable_feed)
    manifest_status = verify_manifest(data_dir, ts=ts)
    trades = trades_payload if isinstance(trades_payload, list) else market_items(trades_payload)
    book_coverage_ok = bool(orderable_feed["requested_tokens"] > 0 and orderable_feed["ok_tokens"] == orderable_feed["requested_tokens"])
    orderable_market_count = sum(1 for row in orderable_feed["markets"] if row.get("orderable"))
    summary_ok = bool(gamma_ok and clob_ok and trades_ok and markets and book_ok > 0 and book_coverage_ok and orderable_market_count > 0 and manifest_status.get("ok"))
    summary = {
        "ok": summary_ok,
        "ts": ts,
        "source": "polymarket_market_recorder_v0",
        "recorder": "polymarket_market_recorder_v0",
        "universe": universe,
        "market_count": len(markets),
        "token_count": len(token_ids),
        "trade_count": len(trades) if isinstance(trades, list) else 0,
        "sources": sources,
        "book_coverage": {
            "requested_tokens": orderable_feed["requested_tokens"],
            "ok_tokens": orderable_feed["ok_tokens"],
            "orderable_tokens": orderable_feed["orderable_tokens"],
            "coverage_ratio": orderable_feed["coverage_ratio"],
            "orderable_ratio": orderable_feed["orderable_ratio"],
        },
        "orderable_market_count": orderable_market_count,
        "manifest": manifest_status,
        "raw_day_dir": str(raw_day_dir(data_dir, ts)),
    }
    atomic_write_json(data_dir / "normalized" / "polymarket" / "latest_summary.json", summary)
    latest_markets = {
        "ts": ts,
        "source": "polymarket_market_recorder_v0",
        "markets": markets,
        "book_coverage": summary["book_coverage"],
        "orderable_market_count": summary["orderable_market_count"],
    }
    atomic_write_json(data_dir / "normalized" / "polymarket" / "latest_markets.json", latest_markets)
    atomic_write_json(data_dir / "reports" / "market_recorder_health.json", summary)
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
                continue
            try:
                ok_frames = int(detail.get("ok_frames") or 0)
            except Exception:
                out["errors"].append(f"source_invalid:{source}")
                continue
            if ok_frames <= 0:
                out["errors"].append(f"source_not_ok:{source}")
        coverage = health.get("book_coverage") if isinstance(health.get("book_coverage"), dict) else {}
        if coverage:
            requested_tokens = strict_positive_int(coverage.get("requested_tokens"))
            ok_tokens = manifest_sequence(coverage.get("ok_tokens"))
            if requested_tokens is None or ok_tokens is None or ok_tokens < requested_tokens:
                out["errors"].append("book_coverage_incomplete")
        else:
            out["errors"].append("missing_book_coverage")
        orderable_count = strict_positive_int(health.get("orderable_market_count"))
        if orderable_count is None:
            out["errors"].append("orderable_market_count_invalid")
        manifest = health.get("manifest") if isinstance(health.get("manifest"), dict) else {}
        if manifest.get("ok") is not True:
            out["errors"].append("manifest_verification_failed")
        else:
            scope = manifest.get("verification_scope")
            verified_rows = strict_positive_int(manifest.get("verified_rows"))
            latest_sequence = strict_positive_int(manifest.get("latest_sequence"))
            if scope not in {"tail", "full"}:
                out["errors"].append("manifest_scope_invalid")
            elif verified_rows is None:
                out["errors"].append("manifest_verified_rows_invalid")
            elif latest_sequence is None:
                out["errors"].append("manifest_latest_sequence_invalid")
            elif scope == "tail":
                max_rows = strict_positive_int(manifest.get("max_rows"))
                if max_rows is None:
                    out["errors"].append("manifest_tail_scope_invalid")
            elif manifest.get("max_rows") is not None:
                out["errors"].append("manifest_full_scope_invalid")
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
