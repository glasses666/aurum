#!/usr/bin/env python3
"""Generate the public Aurum paper-duel trading terminal dashboard.

Static output only. It reads paper-ledger/tick/review files and writes an HTML
snapshot that can be served by nginx. No API keys or env secrets are emitted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import pathlib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import agent_duel as duel
import bot_scripts
import market_recorder
import strategy_rules

AGENT_LABELS = {
    "superwing": "SuperWing",
    "deepseek": "DeepSeek",
}

AGENT_COLORS = {
    "superwing": "#a78bfa",
    "deepseek": "#22d3ee",
}

BITCOIN_TERMS = ("bitcoin", "btc", "satoshi")
REDACTED = "[redacted]"
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "credential",
    "bearer",
    "authorization",
    "private",
    "ssh",
    "host",
    "hostname",
    "ip",
    "server",
    "connection",
    "env",
    "data_dir",
    "root",
    "path",
    "file",
    "remote_error",
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"(?:ssh|postgres|mysql|mongodb|redis)://[^\s\"']+", re.I),
    re.compile(r"\b[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"),
    re.compile(r"(?:^|[\s\"'])/(?:Users|home|root|opt|etc|var)/[^\s\"']+"),
    re.compile(r"(?:^|[\s\"'])~/.ssh/[^\s\"']+"),
)

CSS = """
:root {
  color-scheme: dark;
  --bg: #050505;
  --panel: #0a0a0a;
  --line: #222;
  --line-soft: #151515;
  --text: #f5f5f5;
  --muted: #8a8a8a;
  --muted-2: #5e5e5e;
  --btc: #f7931a;
  --superwing: #a78bfa;
  --deepseek: #22d3ee;
  --green: #2fd37f;
  --red: #ff5c5c;
  --amber: #f3c969;
}
* { box-sizing: border-box; }
html, body { min-height: 100%; overflow-x: hidden; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "Segoe UI", sans-serif;
  font-variant-numeric: tabular-nums;
}
a { color: inherit; }
.terminal {
  min-height: 100vh;
  width: 100%;
  max-width: 100vw;
  overflow-x: hidden;
  display: grid;
  grid-template-columns: clamp(230px, 20vw, 300px) minmax(0, 1fr) clamp(280px, 24vw, 360px);
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
.left-rail, .right-log {
  min-width: 0;
  min-height: 100vh;
  background: #070707;
}
.left-rail { border-right: 1px solid var(--line); }
.right-log { border-left: 1px solid var(--line); }
.center-stage { min-width: 0; overflow: hidden; display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr) auto; }
.topbar {
  height: 66px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 0 22px;
  border-bottom: 1px solid var(--line);
  background: #060606;
}
.brand { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
.brand h1 { margin: 0; font-size: 21px; letter-spacing: -.04em; font-weight: 650; }
.brand span, .meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
.meta { display: flex; gap: 16px; align-items: center; }
.rail-section { padding: 16px 16px 18px; border-bottom: 1px solid var(--line); }
.rail-title { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .12em; margin-bottom: 12px; }
.big-number { font-size: 34px; line-height: 1; letter-spacing: -.06em; font-weight: 680; }
.caption { margin-top: 6px; color: var(--muted); font-size: 12px; line-height: 1.45; }
.quality-banner { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; padding: 12px 22px; border-bottom: 1px solid var(--line); background: #070707; }
.quality-banner.warning { border-bottom-color: rgba(243,201,105,.45); }
.quality-item { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.quality-item b { display: block; margin-top: 4px; color: var(--text); font-size: 13px; text-transform: none; letter-spacing: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-row, .rank-row, .log-row, .position-row {
  display: grid;
  gap: 6px;
  padding: 10px 0;
  border-top: 1px solid var(--line-soft);
}
.agent-row:first-of-type, .rank-row:first-of-type, .log-row:first-of-type, .position-row:first-of-type { border-top: 0; }
.agent-head, .rank-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.agent-name { font-weight: 650; letter-spacing: -.02em; }
.agent-sub, .tiny { color: var(--muted); font-size: 12px; line-height: 1.45; }
.agent-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; color: var(--muted); font-size: 12px; }
.agent-stats b { display: block; color: var(--text); font-size: 15px; margin-top: 2px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; vertical-align: middle; margin-right: 8px; background: currentColor; }
.pill { border: 1px solid var(--line); color: var(--muted); padding: 3px 7px; border-radius: 999px; font-size: 11px; white-space: nowrap; }
.pill.green { color: var(--green); border-color: rgba(47,211,127,.35); }
.pill.amber { color: var(--amber); border-color: rgba(243,201,105,.35); }
.chart-wrap { position: relative; min-width: 0; padding: 18px 22px 10px; min-height: 0; overflow: hidden; }
.chart-title { display: grid; grid-template-columns: minmax(0, 1fr) max-content; gap: 18px; align-items: end; margin-bottom: 12px; min-width: 0; }
.chart-title > div:first-child { min-width: 0; }
.chart-title h2 { margin: 0; font-size: clamp(28px, 3.2vw, 46px); letter-spacing: -.06em; font-weight: 650; line-height: .98; text-wrap: balance; }
.chart-title p { margin: 7px 0 0; color: var(--muted); max-width: 780px; line-height: 1.45; }
.legend-line { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 10px 14px; color: var(--muted); font-size: 12px; max-width: min(280px, 100%); }
.legend-line span { white-space: nowrap; }
.chart-shell {
  width: 100%;
  min-width: 0;
  height: clamp(300px, calc(100vh - 304px), 560px);
  min-height: 300px;
  overflow: hidden;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  background: #050505;
}
.chart { width: 100%; height: 100%; display: block; background: #050505; }
.mid-grid { stroke: #171717; stroke-width: 1; }
.axis { stroke: #333; stroke-width: 1; }
.axis-label { fill: #7f7f7f; font-size: 11px; }
.trade-pip { stroke: #050505; stroke-width: 2; }
.stage-footer {
  border-top: 1px solid var(--line);
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  min-height: 104px;
}
.footer-cell { padding: 14px 16px; border-right: 1px solid var(--line); }
.footer-cell:last-child { border-right: 0; }
.footer-cell span { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .11em; }
.footer-cell b { display: block; margin-top: 8px; font-size: 18px; letter-spacing: -.03em; }
.log-head { height: 66px; padding: 14px 16px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; }
.log-head h2 { margin: 0; font-size: 18px; letter-spacing: -.03em; }
.log-list { max-height: calc(100vh - 66px); overflow: auto; }
.log-row { padding: 12px 16px; grid-template-columns: 62px 1fr; }
.log-time { color: var(--muted-2); font-size: 11px; }
.log-main { min-width: 0; }
.log-top { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 4px; }
.log-action { font-weight: 650; }
.log-market { color: var(--muted); font-size: 12px; line-height: 1.35; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.log-detail { color: var(--text); font-size: 12px; line-height: 1.45; }
.log-note { color: var(--muted); font-size: 11px; line-height: 1.35; margin-top: 4px; }
pre.rules {
  margin: 10px 0 0;
  padding: 10px 0 0;
  border: 0;
  border-top: 1px solid var(--line-soft);
  max-height: 190px;
  overflow: auto;
  color: var(--muted);
  font: 11px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap;
  background: transparent;
}
.rule-list { display: grid; gap: 8px; margin-top: 8px; }
.rule-line { display: flex; justify-content: space-between; gap: 10px; padding-top: 8px; border-top: 1px solid var(--line-soft); color: var(--muted); font-size: 12px; line-height: 1.35; }
.rule-line:first-child { border-top: 0; padding-top: 0; }
.rule-line b { color: var(--text); font-weight: 600; text-align: right; }
.empty { color: var(--muted); padding: 18px 16px; line-height: 1.45; }
@media (max-width: 1280px) {
  .chart-title { grid-template-columns: minmax(0, 1fr); }
  .legend-line { justify-content: flex-start; max-width: 100%; }
  .chart-title h2 { font-size: clamp(30px, 4vw, 44px); }
  .chart-shell { height: clamp(280px, calc(100vh - 330px), 520px); min-height: 280px; }
}
@media (max-width: 1120px) {
  .terminal { grid-template-columns: clamp(230px, 26vw, 270px) minmax(0, 1fr); }
  .right-log { grid-column: 1 / -1; min-height: auto; border-left: 0; border-top: 1px solid var(--line); }
  .log-list { max-height: 420px; }
}
@media (max-width: 860px) {
  .terminal { display: block; }
  .left-rail, .right-log { min-height: auto; border: 0; border-bottom: 1px solid var(--line); }
  .topbar, .meta { display: block; height: auto; }
  .topbar { padding: 16px; }
  .chart-wrap { padding: 16px; min-height: auto; }
  .chart-title { display: block; }
  .legend-line { margin-top: 12px; }
  .chart-shell { height: clamp(260px, 62vw, 420px); min-height: 260px; }
  .stage-footer { grid-template-columns: 1fr 1fr; }
}
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def short_time(value: Any) -> str:
    parsed = parse_ts(value)
    if parsed:
        return parsed.strftime("%H:%M:%S")
    text = str(value or "")
    return text[11:19] if len(text) >= 19 else text[:8]


def read_jsonl(path: pathlib.Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    rows: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except Exception:
            continue
    return rows


def read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def trunc(value: Any, limit: int = 86) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return "0.00"


def fmt_pct(value: Any, already_pct: bool = False) -> str:
    try:
        v = float(value)
        if not already_pct:
            v *= 100
        return f"{v:+.2f}%"
    except Exception:
        return "+0.00%"


def fmt_price(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except Exception:
        return "—"


def sensitive_key(key: Any) -> bool:
    lower = str(key or "").lower()
    return any(part in lower for part in SENSITIVE_KEY_PARTS)


def redact_string(value: str) -> str:
    redacted = value
    for pattern in SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact_value(value: Any, key: Any = "") -> Any:
    if sensitive_key(key) and isinstance(value, (str, int, float, bool)):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item, key) for item in value]
    if isinstance(value, str):
        return redact_string(value)
    return value


def safe_errors(errors: Any, limit: int = 8) -> List[str]:
    if not isinstance(errors, list):
        return []
    return [str(redact_string(str(item)))[:160] for item in errors[:limit]]


def env_public(env_file: Optional[pathlib.Path]) -> Dict[str, str]:
    allowed = {
        "AURUM_DUEL_MODE",
        "AURUM_DUEL_LIMIT",
        "AURUM_DUEL_MIN_VOLUME",
        "AURUM_DUEL_MAX_ORDERS",
        "AURUM_DUEL_UNIVERSE",
        "AURUM_DUEL_SEARCH_QUERY",
        "AURUM_BOT_MIN_INTERVAL_SEC",
        "AURUM_BOT_DEFAULT_INTERVAL_SEC",
        "AURUM_FIRST_CONTEST_DAYS",
        "AURUM_DEEPSEEK_ALLOW_PAPER_APPLY",
        "AURUM_DEEPSEEK_MAX_ORDERS",
        "AURUM_DEEPSEEK_MAX_NOTIONAL",
        "AURUM_DEEPSEEK_THINKING",
        "AURUM_DEEPSEEK_REASONING_EFFORT",
        "DEEPSEEK_MODEL",
        "AURUM_REVIEW_MODEL",
        "AURUM_RULE_AUTO_PROMOTE",
        "AURUM_REVIEW_INTERVAL_HOURS",
        "AURUM_PUBLIC_DASHBOARD_DIR",
        "AURUM_PAPER_TRADE_ROLE",
        "AURUM_POLYMARKET_FEE_MODE",
        "AURUM_POLY_MIN_ORDER_USDC",
    }
    out: Dict[str, str] = {}
    if env_file and env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in allowed:
                out[k] = v.strip().strip('"').strip("'")
    for k in allowed:
        if k in os.environ:
            out[k] = os.environ[k]
    return out


def latest_scores(state: Dict[str, Any], ticks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if ticks and isinstance(ticks[-1].get("scores"), list):
        return ticks[-1]["scores"]
    markets = state.get("last_markets", []) if isinstance(state, dict) else []
    prices = duel.market_price_map(markets)
    rows = []
    for agent in duel.AGENTS:
        account = state.get("accounts", {}).get(agent) if isinstance(state, dict) else None
        if account:
            rows.append(duel.portfolio_value(account, prices))
    return rows


def score_band(row: Dict[str, Any]) -> str:
    try:
        roi = float(row.get("roi", 0.0) or 0.0)
    except Exception:
        roi = 0.0
    if roi >= 0.05:
        return "strong up"
    if roi >= 0.005:
        return "up"
    if roi <= -0.05:
        return "drawdown"
    if roi <= -0.005:
        return "down"
    return "flat"


def coarse_scores(scores: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked = sorted(scores, key=lambda r: float(r.get("score", 0.0) or 0.0), reverse=True)
    rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(ranked, 1):
        agent = str(row.get("agent_id") or "unknown")
        details = row.get("details") if isinstance(row.get("details"), list) else []
        rows.append(
            {
                "agent_id": agent,
                "label": AGENT_LABELS.get(agent, agent),
                "rank": idx,
                "score_band": score_band(row),
                "position_bucket": bucket_count(len(details)),
            }
        )
    return rows


def bucket_count(count: int) -> str:
    if count <= 0:
        return "none"
    if count <= 3:
        return "1-3"
    if count <= 10:
        return "4-10"
    return "10+"


def market_is_bitcoin(market: Dict[str, Any]) -> bool:
    text = " ".join(
        str(market.get(k, "")) for k in ("question", "slug", "title", "category", "description")
    ).lower()
    return any(term in text for term in BITCOIN_TERMS)


def choose_bitcoin_market(markets: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [m for m in markets if isinstance(m, dict) and market_is_bitcoin(m)]
    if not candidates:
        return None
    candidates.sort(key=lambda m: (float(m.get("volume", 0.0) or 0.0), float(m.get("liquidity", 0.0) or 0.0)), reverse=True)
    return candidates[0]


def yes_price(market: Dict[str, Any]) -> Optional[float]:
    outcomes = market.get("outcomes") or []
    if not isinstance(outcomes, list):
        return None
    chosen = None
    for item in outcomes:
        if str(item.get("name", "")).lower() in {"yes", "up", "higher", "above"}:
            chosen = item
            break
    if chosen is None and outcomes:
        chosen = outcomes[0]
    try:
        return float(chosen.get("price")) if chosen else None
    except Exception:
        return None


def snapshot_records(data_dir: pathlib.Path, ticks: List[Dict[str, Any]], limit: int = 160) -> List[Dict[str, Any]]:
    seen: set[pathlib.Path] = set()
    paths: List[pathlib.Path] = []
    root = data_dir / "snapshots"
    if root.exists():
        paths.extend(sorted(root.glob("*.json"))[-limit:])
    for tick in ticks[-limit:]:
        raw = tick.get("snapshot_file")
        if raw:
            p = pathlib.Path(str(raw))
            if p.exists():
                paths.append(p)
    rows = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        row = read_json(path, None)
        if isinstance(row, dict):
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("ts") or r.get("snapshot_id") or ""))
    return rows[-limit:]


def bitcoin_series(data_dir: pathlib.Path, ticks: List[Dict[str, Any]], state: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    points: List[Dict[str, Any]] = []
    latest_market: Optional[Dict[str, Any]] = None
    for snap in snapshot_records(data_dir, ticks):
        market = choose_bitcoin_market(snap.get("markets", []) or [])
        if not market:
            continue
        price = yes_price(market)
        if price is None:
            continue
        latest_market = market
        points.append({"ts": snap.get("ts") or snap.get("snapshot_id"), "value": price, "market": market})
    if not latest_market:
        latest_market = choose_bitcoin_market(state.get("last_markets", []) if isinstance(state, dict) else [])
        if latest_market:
            price = yes_price(latest_market)
            if price is not None:
                points.append({"ts": state.get("updated_at") or utc_now(), "value": price, "market": latest_market})
    return points[-120:], latest_market, "btc_market_yes_price"


def roi_series(ticks: List[Dict[str, Any]], state: Dict[str, Any], scores: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    series: Dict[str, List[Dict[str, Any]]] = {"superwing": [], "deepseek": []}
    for tick in ticks[-120:]:
        by_agent = {row.get("agent_id"): row for row in tick.get("scores", []) if isinstance(row, dict)}
        for agent in series:
            row = by_agent.get(agent)
            if row:
                series[agent].append({"ts": tick.get("ts"), "value": float(row.get("roi", 0.0) or 0.0)})
    if not any(series.values()):
        now = state.get("updated_at") if isinstance(state, dict) else utc_now()
        by_agent = {row.get("agent_id"): row for row in scores if isinstance(row, dict)}
        for agent in series:
            row = by_agent.get(agent, {})
            series[agent].append({"ts": now, "value": float(row.get("roi", 0.0) or 0.0)})
    return series


def seconds_since(value: Any, *, now_value: Optional[str] = None) -> Optional[float]:
    ts = parse_ts(value)
    now_dt = parse_ts(now_value or utc_now())
    if not ts or not now_dt:
        return None
    return max(0.0, (now_dt - ts).total_seconds())


def fallback_ticks_last_hour(ticks: List[Dict[str, Any]], latest_tick: Dict[str, Any]) -> int:
    now_value = latest_tick.get("ts") or utc_now()
    count = 0
    for tick in ticks:
        source = ((tick.get("market_source") or {}) if isinstance(tick.get("market_source"), dict) else {}).get("source", "")
        if "fallback" not in str(source):
            continue
        age = seconds_since(tick.get("ts"), now_value=now_value)
        if age is None or age <= 3600:
            count += 1
    return count


def data_quality_banner(latest_tick: Dict[str, Any], ticks: List[Dict[str, Any]], env: Dict[str, str]) -> str:
    market_source = latest_tick.get("market_source") if isinstance(latest_tick.get("market_source"), dict) else {}
    gate = latest_tick.get("data_quality_gate") if isinstance(latest_tick.get("data_quality_gate"), dict) else None
    gate_missing = gate is None
    if gate is None:
        gate = market_source.get("data_quality_gate") if isinstance(market_source.get("data_quality_gate"), dict) else None
        gate_missing = gate is None
    if not isinstance(gate, dict):
        gate = {}
    source = str(market_source.get("source") or "none")
    decision = str(gate.get("decision") or "UNKNOWN")
    reasons = gate.get("reason_codes", []) if isinstance(gate.get("reason_codes"), list) else []
    reason_text = ",".join(str(reason) for reason in reasons) or "none"
    recorder_age = gate.get("recorder_age_seconds") or market_source.get("recorder_age_seconds")
    coverage = gate.get("book_coverage") if isinstance(gate.get("book_coverage"), dict) else {}
    requested_books = coverage.get("requested_tokens")
    ok_books = coverage.get("ok_tokens")
    coverage_text = "unknown" if requested_books is None or ok_books is None else f"{ok_books}/{requested_books}"
    manifest_scope = gate.get("manifest_verification_scope") or "unknown"
    manifest_max_rows = gate.get("manifest_verification_max_rows")
    manifest_verified_rows = gate.get("manifest_verification_verified_rows")
    manifest_text = f"{manifest_scope} max {manifest_max_rows if manifest_max_rows is not None else 'full'} verified {manifest_verified_rows if manifest_verified_rows is not None else 'unknown'}"
    orderable_count = gate.get("orderable_market_count")
    universe = str(gate.get("universe") or env.get("AURUM_DUEL_UNIVERSE") or "unknown")
    normalized_universe = universe.strip().lower()
    btc_only = "yes" if normalized_universe in {"bitcoin", "btc"} else "unknown" if universe == "unknown" else "no"
    latest_age = seconds_since(latest_tick.get("ts")) if latest_tick else None
    actual_interval = latest_tick.get("loop_interval_sec")
    configured_interval = env.get("AURUM_BOT_DEFAULT_INTERVAL_SEC") or env.get("AURUM_BOT_MIN_INTERVAL_SEC") or "unknown"
    mismatch = False
    try:
        mismatch = actual_interval is not None and int(float(actual_interval)) != int(float(configured_interval))
    except Exception:
        mismatch = False
    warnings = []
    if gate_missing:
        warnings.append("no gate")
    if "fallback" in source:
        warnings.append("unaudited fallback")
    if decision not in {"TRADE_ALLOWED", "UNKNOWN"}:
        warnings.append(decision.lower())
    if mismatch:
        warnings.append("interval mismatch")
    warning_text = "; ".join(warnings) or "clear"
    css = "quality-banner warning" if warnings else "quality-banner"
    return f"""
      <div class=\"{css}\" aria-label=\"Data quality\">
        <div class=\"quality-item\">Data quality<b>{esc(decision)} · {esc(warning_text)}</b></div>
        <div class=\"quality-item\">Source<b>{esc(source)} · fallback ticks last 1h {fallback_ticks_last_hour(ticks, latest_tick)}</b></div>
        <div class=\"quality-item\">Recorder age<b>{esc('n/a' if recorder_age is None else str(recorder_age) + 's')} · latest tick age {esc('n/a' if latest_age is None else str(round(latest_age, 1)) + 's')}</b></div>
        <div class=\"quality-item\">Manifest<b>{esc(manifest_text)}</b></div>
        <div class=\"quality-item\">Book coverage<b>{esc(coverage_text)} · orderable markets {esc(orderable_count if orderable_count is not None else 'unknown')}</b></div>
        <div class=\"quality-item\">BTC-only<b>{esc(btc_only)} · universe {esc(universe)}</b></div>
        <div class=\"quality-item\">Interval<b>actual {esc(actual_interval if actual_interval is not None else 'unknown')}s · configured {esc(configured_interval)}s</b></div>
        <div class=\"quality-item\">Reasons<b>{esc(reason_text)}</b></div>
        <div class=\"quality-item\">Tick<b>{esc(latest_tick.get('tick_id', 'none') if latest_tick else 'none')}</b></div>
      </div>
    """


def latest_json_candidate(paths: Iterable[pathlib.Path]) -> Optional[Dict[str, Any]]:
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            candidates = sorted(path.glob("*.json"))
            if not candidates:
                continue
            path = candidates[-1]
        value = read_json(path, None)
        if isinstance(value, dict):
            return value
    return None


def public_recorder_summary(data_dir: pathlib.Path) -> Dict[str, Any]:
    health = market_recorder.recorder_health(
        recorder_data_root(data_dir),
        max_stale_seconds=int(os.environ.get("AURUM_RECORDER_MAX_STALE_SECONDS", "180")),
    )
    last = health.get("last_capture", {}) if isinstance(health.get("last_capture"), dict) else {}
    manifest = last.get("manifest") if isinstance(last.get("manifest"), dict) else {}
    coverage = last.get("book_coverage") if isinstance(last.get("book_coverage"), dict) else {}
    sources = last.get("sources", {}) if isinstance(last.get("sources"), dict) else {}
    return {
        "ok": bool(health.get("ok")),
        "age_seconds": health.get("age_seconds"),
        "errors": safe_errors(health.get("errors", [])),
        "market_count": last.get("market_count"),
        "sources": {
            source: {"ok_frames": (detail or {}).get("ok_frames")}
            for source, detail in sources.items()
            if isinstance(detail, dict)
        },
        "manifest": {
            "ok": manifest.get("ok"),
            "verification_scope": manifest.get("verification_scope"),
            "max_rows": manifest.get("max_rows"),
            "verified_rows": manifest.get("verified_rows"),
            "latest_sequence": manifest.get("latest_sequence"),
        },
        "book_coverage": coverage,
        "orderable_market_count": last.get("orderable_market_count"),
    }


def public_gate_summary(latest_tick: Dict[str, Any]) -> Dict[str, Any]:
    gate = latest_tick.get("data_quality_gate") if isinstance(latest_tick.get("data_quality_gate"), dict) else {}
    if not gate:
        market_source = latest_tick.get("market_source") if isinstance(latest_tick.get("market_source"), dict) else {}
        gate = market_source.get("data_quality_gate") if isinstance(market_source.get("data_quality_gate"), dict) else {}
    return {
        "decision": gate.get("decision", "UNKNOWN"),
        "trade_allowed": bool(gate.get("trade_allowed")),
        "hold_only": bool(gate.get("hold_only")),
        "reason_codes": safe_errors(gate.get("reason_codes", [])),
        "recorder_age_seconds": gate.get("recorder_age_seconds"),
        "book_coverage": gate.get("book_coverage") if isinstance(gate.get("book_coverage"), dict) else {},
        "orderable_market_count": gate.get("orderable_market_count"),
        "manifest_verification_scope": gate.get("manifest_verification_scope"),
        "manifest_verification_max_rows": gate.get("manifest_verification_max_rows"),
        "manifest_verification_verified_rows": gate.get("manifest_verification_verified_rows"),
    }


def public_bot_registry_summary(data_dir: pathlib.Path) -> Dict[str, Any]:
    verified = bot_scripts.verify_bot_registry_manifest(data_dir)
    current = verified.get("current") if isinstance(verified.get("current"), dict) else {}
    recorded = verified.get("recorded") if isinstance(verified.get("recorded"), dict) else {}
    agents = current.get("agents") if isinstance(current.get("agents"), dict) else {}
    return {
        "ok": bool(verified.get("ok")),
        "schema_version": current.get("schema_version") or recorded.get("schema_version"),
        "errors": safe_errors(verified.get("errors", [])),
        "agents": {
            agent: {
                "review_status": (entry or {}).get("review_status"),
                "tradable": bool((entry or {}).get("tradable")),
                "hold_only": bool((entry or {}).get("hold_only")),
            }
            for agent, entry in agents.items()
            if isinstance(entry, dict)
        },
    }


def backup_status(data_dir: pathlib.Path) -> Dict[str, Any]:
    root = recorder_data_root(data_dir)
    candidates = (
        data_dir / "reports" / "aurum_stability_backup_status.json",
        data_dir / "reports" / "stability_backup_status.json",
        root / "reports" / "aurum_stability_backup_status.json",
        root / "reports" / "stability_backup_status.json",
        root / "reports" / "backup_status.json",
    )
    payload = latest_json_candidate(candidates)
    if not payload:
        return {"ok": None, "status": "missing"}
    safe = redact_value(payload)
    return {
        "ok": bool(payload.get("ok")),
        "status": "ok" if payload.get("ok") else "check",
        "artifact_count": payload.get("artifact_count") or payload.get("file_count") or payload.get("snapshot_count"),
        "contains_recorder_raw": bool(payload.get("contains_recorder_raw") or payload.get("recorder_raw")),
        "contains_manifest": bool(payload.get("contains_manifest") or payload.get("manifest")),
        "contains_features": bool(payload.get("contains_features") or payload.get("features")),
        "contains_reports": bool(payload.get("contains_reports") or payload.get("reports")),
        "redacted": safe,
    }


def replay_status(data_dir: pathlib.Path) -> Dict[str, Any]:
    root = recorder_data_root(data_dir)
    payload = latest_json_candidate(
        (
            data_dir / "replay_summary.json",
            data_dir / "reports" / "replay_summary.json",
            data_dir / "replays",
            root / "reports" / "replay_summary.json",
            root / "replays",
        )
    )
    if not payload:
        return {"ok": None, "status": "missing"}
    return {
        "ok": bool(payload.get("ok")),
        "status": "ok" if payload.get("ok") else "check",
        "mode": payload.get("mode"),
        "market_count": payload.get("market_count"),
        "bot_registry_ok": payload.get("bot_registry_ok"),
        "risk_ledger_rows": payload.get("risk_ledger_rows"),
    }


def risk_ledger_status(data_dir: pathlib.Path) -> Dict[str, Any]:
    path = data_dir / "risk_ledger.jsonl"
    if not path.exists():
        return {"ok": None, "status": "missing", "rows": 0}
    rows = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows += 1
    return {"ok": True, "status": "present", "rows": rows}


def public_runtime_status(data_dir: pathlib.Path, latest_tick: Dict[str, Any]) -> Dict[str, Any]:
    recorder = public_recorder_summary(data_dir)
    registry = public_bot_registry_summary(data_dir)
    backup = backup_status(data_dir)
    replay = replay_status(data_dir)
    ledger = risk_ledger_status(data_dir)
    gate = public_gate_summary(latest_tick)
    runtime_complete = bool(
        gate.get("decision") == "TRADE_ALLOWED"
        and recorder.get("ok") is True
        and registry.get("ok") is True
        and backup.get("ok") is True
    )
    return {
        "completion_state": "runtime-complete" if runtime_complete else "code-complete-only",
        "data_quality_gate": gate,
        "recorder": recorder,
        "bot_registry": registry,
        "backup": {k: v for k, v in backup.items() if k != "redacted"},
        "replay": replay,
        "risk_ledger": ledger,
    }


def read_reviews(data_dir: pathlib.Path) -> List[Dict[str, Any]]:
    root = data_dir / "strategy_reviews"
    if not root.exists():
        return []
    rows = []
    for path in sorted(root.glob("*.json"))[-12:]:
        row = read_json(path, None)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def extract_trade_events(ticks: List[Dict[str, Any]], decisions: List[Dict[str, Any]], limit: int = 80) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for tick in ticks[-80:]:
        tick_ts = tick.get("ts") or tick.get("tick_id")
        agents = tick.get("agents", {}) if isinstance(tick.get("agents"), dict) else {}
        for agent in duel.AGENTS:
            info = agents.get(agent, {}) if isinstance(agents, dict) else {}
            decision = info.get("decision", {}) if isinstance(info, dict) else {}
            result = info.get("result", {}) if isinstance(info, dict) else {}
            for order in decision.get("orders", []) or []:
                if isinstance(order, dict):
                    rows.append({
                        "ts": tick_ts,
                        "agent_id": agent,
                        "kind": "ORDER",
                        "side": str(order.get("side", "buy")).upper(),
                        "notional": order.get("notional"),
                        "price": order.get("limit_price"),
                        "market_id": order.get("market_id"),
                        "question": order.get("question", "") or order.get("market", ""),
                        "outcome": order.get("outcome"),
                        "note": order.get("rationale", ""),
                    })
            for fill in result.get("fills", []) or []:
                if isinstance(fill, dict):
                    rows.append({
                        "ts": fill.get("ts") or tick_ts,
                        "agent_id": agent,
                        "kind": "FILL",
                        "side": str(fill.get("side", "buy")).upper(),
                        "notional": fill.get("notional"),
                        "price": fill.get("fill_price"),
                        "fee": fill.get("fee"),
                        "fee_rate": fill.get("fee_rate"),
                        "fee_category": fill.get("fee_category"),
                        "gross_cost": fill.get("gross_cost"),
                        "market_id": fill.get("market_id"),
                        "question": fill.get("question", ""),
                        "outcome": fill.get("outcome"),
                        "note": fill.get("rationale", ""),
                    })
            for reject in result.get("rejections", []) or []:
                if isinstance(reject, dict):
                    order = reject.get("order", {}) if isinstance(reject.get("order"), dict) else {}
                    rows.append({
                        "ts": reject.get("ts") or tick_ts,
                        "agent_id": agent,
                        "kind": "REJECT",
                        "side": str(order.get("side", "buy")).upper(),
                        "notional": order.get("notional"),
                        "price": order.get("limit_price"),
                        "market_id": order.get("market_id"),
                        "question": order.get("question", ""),
                        "outcome": order.get("outcome"),
                        "note": reject.get("reason", ""),
                    })
            # Hold/no-trade notes are useful for audit, but they drown the trading log.
            # Only emit them as a fallback when there are no order/fill/reject rows at all.
    if not rows:
        for item in decisions[-limit:]:
            decision = item.get("decision", {}) if isinstance(item.get("decision"), dict) else {}
            for order in decision.get("orders", []) or []:
                if isinstance(order, dict):
                    rows.append({
                        "ts": item.get("ts"),
                        "agent_id": item.get("agent_id"),
                        "kind": "ORDER",
                        "side": str(order.get("side", "buy")).upper(),
                        "notional": order.get("notional"),
                        "price": order.get("limit_price"),
                        "market_id": order.get("market_id"),
                        "question": order.get("question", ""),
                        "outcome": order.get("outcome"),
                        "note": order.get("rationale", ""),
                    })
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return rows[:limit]


def public_tick_events(ticks: List[Dict[str, Any]], limit: int = 80) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for tick in ticks[-limit:]:
        agents = tick.get("agents", {}) if isinstance(tick.get("agents"), dict) else {}
        order_count = 0
        fill_count = 0
        reject_count = 0
        for info in agents.values():
            if not isinstance(info, dict):
                continue
            decision = info.get("decision", {}) if isinstance(info.get("decision"), dict) else {}
            result = info.get("result", {}) if isinstance(info.get("result"), dict) else {}
            order_count += len(decision.get("orders", []) or [])
            fill_count += len(result.get("fills", []) or [])
            reject_count += len(result.get("rejections", []) or [])
        gate = tick.get("data_quality_gate") if isinstance(tick.get("data_quality_gate"), dict) else {}
        rows.append(
            {
                "ts": tick.get("ts"),
                "tick_id": tick.get("tick_id"),
                "kind": "TICK",
                "mode": tick.get("effective_mode") or tick.get("mode"),
                "applied": bool(tick.get("applied")),
                "market_count": tick.get("market_count"),
                "decision": gate.get("decision", "UNKNOWN"),
                "orders": order_count,
                "fills": fill_count,
                "rejects": reject_count,
            }
        )
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return rows


def agent_status_panel(scores: List[Dict[str, Any]], state: Dict[str, Any], env: Dict[str, str], data_dir: Optional[pathlib.Path] = None) -> str:
    score_by_agent = {row.get("agent_id"): row for row in coarse_scores(scores) if isinstance(row, dict)}
    scripts: Dict[str, Dict[str, Any]] = {}
    if data_dir is not None:
        for agent in duel.AGENTS:
            try:
                scripts[agent] = bot_scripts.load_bot_script(data_dir, agent)
            except Exception:
                scripts[agent] = {}
    parts: List[str] = []
    for agent in duel.AGENTS:
        row = score_by_agent.get(agent, {})
        script = scripts.get(agent, {})
        status = script.get("status") or ("hold_only" if script.get("hold_only") else "active")
        review = bot_scripts.review_status(script) if script else "unknown"
        tradable = "tradable" if bot_scripts.script_tradable(script) else "hold-only"
        color = AGENT_COLORS[agent]
        parts.append(f"""
        <div class="agent-row">
          <div class="agent-head">
            <span class="agent-name" style="color:{color}"><span class="dot"></span>{esc(AGENT_LABELS[agent])}</span>
            <span class="pill">rank #{esc(row.get('rank', '—'))}</span>
          </div>
          <div class="agent-sub">{esc(env.get('AURUM_DUEL_MODE', 'review_only'))} · {esc(status)} · review {esc(review)}</div>
          <div class="agent-stats">
            <span>score band<b>{esc(row.get('score_band', 'unknown'))}</b></span>
            <span>position bucket<b>{esc(row.get('position_bucket', 'unknown'))}</b></span>
            <span>script gate<b>{esc(tradable)}</b></span>
            <span>paper mode<b>aggregate only</b></span>
          </div>
        </div>
        """)
    return "\n".join(parts)


def positions_panel(scores: List[Dict[str, Any]]) -> str:
    total_positions = 0
    active_agents = 0
    for score in scores:
        details = score.get("details") if isinstance(score.get("details"), list) else []
        total_positions += len(details)
        if details:
            active_agents += 1
    return f"""
      <div class="position-row">
        <div class="rank-head"><span>Open exposure</span><span>{esc(bucket_count(total_positions))}</span></div>
        <div class="tiny">aggregate paper-position bucket only; per-agent positions stay in operator output</div>
      </div>
      <div class="position-row">
        <div class="rank-head"><span>Agents with exposure</span><span>{esc(bucket_count(active_agents))}</span></div>
        <div class="tiny">no raw position keys, share counts, or account-like balances are public</div>
      </div>
    """


def rule_excerpt(rules: Dict[str, Any]) -> str:
    sw = rules.get("superwing", {})
    ds = rules.get("deepseek_rules_excerpt", "")
    payload = {
        "superwing": {k: sw.get(k) for k in ("name", "version", "price_min", "price_max", "max_notional", "min_volume")},
        "deepseek_excerpt": str(ds)[:650],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def polymarket_rules_panel() -> str:
    rules = duel.paper_execution_rules()
    crypto_rate = float(rules["taker_fee_rates"].get("crypto", 0.0))
    other_rate = float(rules["taker_fee_rates"].get("other", 0.0))
    return f"""
      <div class="rule-list">
        <div class="rule-line"><span>Fill role</span><b>{esc(rules['paper_fill_role']).upper()}</b></div>
        <div class="rule-line"><span>Fee formula</span><b>shares × rate × p × (1-p)</b></div>
        <div class="rule-line"><span>BTC/Crypto taker rate</span><b>{crypto_rate:.2%}</b></div>
        <div class="rule-line"><span>Other/general rate</span><b>{other_rate:.2%}</b></div>
        <div class="rule-line"><span>Maker fee/rebate</span><b>0 fee · rebate not credited</b></div>
        <div class="rule-line"><span>Fee precision</span><b>{rules['fee_precision_places']} decimals</b></div>
        <div class="rule-line"><span>Min paper order</span><b>${fmt_money(rules['min_order_usdc'])}</b></div>
      </div>
      <div class="caption">按 Polymarket fee docs: taker fee 在 match time 计算；当前 paper fill 都按 taker，maker queue/rebate 等 recorder v2 再模拟。</div>
    """


def x_for_index(idx: int, total: int, left: float, width: float) -> float:
    if total <= 1:
        return left + width * 0.5
    return left + (idx / (total - 1)) * width


def line_path(values: List[float], low: float, high: float, left: float, top: float, width: float, height: float) -> str:
    if not values:
        return ""
    span = high - low if high != low else 1.0
    pts = []
    for idx, value in enumerate(values):
        x = x_for_index(idx, len(values), left, width)
        y = top + (1 - ((value - low) / span)) * height
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def trade_chart(
    btc: List[Dict[str, Any]],
    roi: Dict[str, List[Dict[str, Any]]],
    events: List[Dict[str, Any]],
) -> str:
    width, height = 1060, 560
    left, right, top, bottom = 62, 62, 36, 56
    plot_w = width - left - right
    plot_h = height - top - bottom
    btc_values = [float(p.get("value", 0.0)) for p in btc]
    roi_values = [float(p.get("value", 0.0)) for arr in roi.values() for p in arr]
    btc_low = max(0.0, min(btc_values or [0.45]) - 0.03)
    btc_high = min(1.0, max(btc_values or [0.55]) + 0.03)
    if btc_high - btc_low < 0.08:
        mid = (btc_high + btc_low) / 2
        btc_low, btc_high = max(0.0, mid - 0.04), min(1.0, mid + 0.04)
    roi_low = min(roi_values + [-0.004])
    roi_high = max(roi_values + [0.004])
    if roi_high == roi_low:
        roi_high += 0.01
        roi_low -= 0.01
    btc_points = line_path(btc_values, btc_low, btc_high, left, top, plot_w, plot_h)
    roi_lines = []
    for agent, arr in roi.items():
        vals = [float(p.get("value", 0.0)) for p in arr]
        points = line_path(vals, roi_low, roi_high, left, top, plot_w, plot_h)
        if points:
            roi_lines.append(f'<polyline points="{points}" fill="none" stroke="{AGENT_COLORS[agent]}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" opacity=".95"/>')
    grid = []
    for i in range(6):
        y = top + i * plot_h / 5
        grid.append(f'<line class="mid-grid" x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}"/>')
    pips = []
    event_points = [e for e in events if e.get("kind") in {"ORDER", "FILL"} and e.get("price") is not None]
    for idx, event in enumerate(event_points[:30]):
        raw_price = event.get("price")
        if raw_price is None:
            continue
        try:
            price = float(raw_price)
        except Exception:
            continue
        x = x_for_index(max(0, len(btc_values) - 1 - (idx % max(1, len(btc_values)))), max(2, len(btc_values)), left, plot_w)
        span = btc_high - btc_low if btc_high != btc_low else 1.0
        y = top + (1 - ((price - btc_low) / span)) * plot_h
        agent = str(event.get("agent_id") or "")
        color = AGENT_COLORS.get(agent, "#fff")
        shape = "r='5'" if event.get("kind") == "FILL" else "r='3.5'"
        pips.append(f'<circle class="trade-pip" cx="{x:.1f}" cy="{y:.1f}" {shape} fill="{color}" opacity=".95"><title>{esc(agent)} {esc(event.get("kind"))} {fmt_price(price)}</title></circle>')
    if btc_points:
        btc_line = f'<polyline points="{btc_points}" fill="none" stroke="var(--btc)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
    else:
        btc_line = f'<text x="{left + 20}" y="{top + plot_h/2:.1f}" fill="#777" font-size="16">waiting for Bitcoin recorder frames</text>'
    return f"""
    <div class="chart-shell">
      <svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="Bitcoin market line, agent ROI lines, and trade points">
        {''.join(grid)}
        <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/>
        <line class="axis" x1="{width-right}" y1="{top}" x2="{width-right}" y2="{height-bottom}"/>
        <line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>
        <text class="axis-label" x="{left}" y="22">BTC market price / probability</text>
        <text class="axis-label" x="{width-right-88}" y="22">agent ROI</text>
        <text class="axis-label" x="{left}" y="{height-20}">{esc(short_time(btc[0].get('ts') if btc else 'start'))}</text>
        <text class="axis-label" x="{width-right-70}" y="{height-20}">{esc(short_time(btc[-1].get('ts') if btc else 'now'))}</text>
        <text class="axis-label" x="14" y="{top+8}">{btc_high:.3f}</text>
        <text class="axis-label" x="14" y="{height-bottom}">{btc_low:.3f}</text>
        <text class="axis-label" x="{width-right+10}" y="{top+8}">{fmt_pct(roi_high)}</text>
        <text class="axis-label" x="{width-right+10}" y="{height-bottom}">{fmt_pct(roi_low)}</text>
        {btc_line}
        {''.join(roi_lines)}
        {''.join(pips)}
      </svg>
    </div>
    """


def event_log(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "<div class='empty'>暂无 tick 事件。公共视图只显示粗粒度 tick 汇总。</div>"
    rows = []
    for item in events[:80]:
        if item.get("kind") == "TICK":
            rows.append(f"""
            <div class="log-row">
              <div class="log-time">{esc(short_time(item.get('ts')))}</div>
              <div class="log-main">
                <div class="log-top"><span class="log-action">TICK · {esc(item.get('mode'))}</span><span class="pill">{esc(item.get('decision'))}</span></div>
                <div class="log-detail">markets {esc(item.get('market_count'))} · orders {esc(item.get('orders'))} · fills {esc(item.get('fills'))} · rejects {esc(item.get('rejects'))}</div>
                <div class="log-market">tick {esc(item.get('tick_id') or 'none')} · applied {esc(str(item.get('applied')).lower())}</div>
                <div class="log-note">coarse public activity; detailed order and ledger diagnostics are operator-only</div>
              </div>
            </div>
            """)
            continue
        agent = str(item.get("agent_id") or "")
        color = AGENT_COLORS.get(agent, "#ddd")
        notional = item.get("notional")
        price = item.get("price")
        fee = item.get("fee")
        fee_text = f" · fee ${fmt_money(fee)}" if fee is not None else ""
        fee_category = item.get("fee_category")
        fee_category_text = f" · {fee_category}" if fee_category else ""
        rows.append(f"""
        <div class="log-row">
          <div class="log-time">{esc(short_time(item.get('ts')))}</div>
          <div class="log-main">
            <div class="log-top"><span class="log-action" style="color:{color}">{esc(item.get('kind'))} · {esc(AGENT_LABELS.get(agent, agent))}</span><span class="pill">{esc(item.get('side'))}</span></div>
            <div class="log-detail">{esc(item.get('outcome') or '')} · ${fmt_money(notional)} @ {fmt_price(price)}{esc(fee_text)}{esc(fee_category_text)}</div>
            <div class="log-market">{esc(trunc(item.get('question') or item.get('market_id') or 'market', 96))}</div>
            <div class="log-note">{esc(trunc(item.get('note'), 150))}</div>
          </div>
        </div>
        """)
    return "\n".join(rows)


def latest_review_summary(reviews: List[Dict[str, Any]]) -> str:
    if not reviews:
        return "No 5h pro review yet."
    latest = reviews[-1]
    summary = latest.get("summary") or latest.get("public_dashboard_note") or "review recorded"
    return f"{latest.get('review_id', '')} · {summary}"


def recorder_data_root(data_dir: pathlib.Path) -> pathlib.Path:
    configured = os.environ.get("AURUM_RECORDER_DATA_DIR", "").strip()
    if configured:
        return pathlib.Path(configured)
    if data_dir.name == "paper_duel":
        return data_dir.parent
    return data_dir


def recorder_panel(data_dir: pathlib.Path) -> str:
    summary = public_recorder_summary(data_dir)
    sources = summary.get("sources", {}) if isinstance(summary.get("sources"), dict) else {}
    clob_book = sources.get("clob_book") if isinstance(sources.get("clob_book"), dict) else {}
    status = "live" if summary.get("ok") else "check"
    pill = "green" if summary.get("ok") else "amber"
    age = summary.get("age_seconds", "n/a")
    coverage = summary.get("book_coverage") if isinstance(summary.get("book_coverage"), dict) else {}
    requested = coverage.get("requested_tokens", "unknown")
    ok_tokens = coverage.get("ok_tokens", "unknown")
    return f"""
      <div class=\"rule-line\"><span>Status</span><b><span class=\"pill {pill}\">{esc(status)}</span></b></div>
      <div class=\"rule-line\"><span>Age</span><b>{esc(age)}s</b></div>
      <div class=\"rule-line\"><span>Markets</span><b>{esc(summary.get('market_count', 0))}</b></div>
      <div class=\"rule-line\"><span>Books</span><b>{esc(clob_book.get('ok_frames', 0))} · coverage {esc(ok_tokens)}/{esc(requested)}</b></div>
      <div class=\"caption\">{esc(', '.join(summary.get('errors', [])) or 'Gamma/CLOB/Data API frames captured independently from paper fills.')}</div>
    """


def runtime_panel(runtime: Dict[str, Any]) -> str:
    registry = runtime.get("bot_registry") if isinstance(runtime.get("bot_registry"), dict) else {}
    backup = runtime.get("backup") if isinstance(runtime.get("backup"), dict) else {}
    replay = runtime.get("replay") if isinstance(runtime.get("replay"), dict) else {}
    ledger = runtime.get("risk_ledger") if isinstance(runtime.get("risk_ledger"), dict) else {}
    return f"""
      <div class="rule-line"><span>Completion</span><b>{esc(runtime.get('completion_state', 'code-complete-only'))}</b></div>
      <div class="rule-line"><span>Bot registry</span><b>{esc('ok' if registry.get('ok') else 'check')}</b></div>
      <div class="rule-line"><span>Backup</span><b>{esc(backup.get('status', 'missing'))}</b></div>
      <div class="rule-line"><span>Replay</span><b>{esc(replay.get('status', 'missing'))}</b></div>
      <div class="rule-line"><span>Risk ledger</span><b>{esc(ledger.get('status', 'missing'))} · rows {esc(ledger.get('rows', 0))}</b></div>
    """


def write_operator_output(
    operator_dir: pathlib.Path,
    *,
    data_dir: pathlib.Path,
    state: Dict[str, Any],
    ticks: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
    reviews: List[Dict[str, Any]],
    scores: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    runtime: Dict[str, Any],
) -> pathlib.Path:
    operator_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = redact_value(
        {
            "ok": True,
            "view": "operator_diagnostics_v1",
            "generated_at": utc_now(),
            "runtime": runtime,
            "scores": scores,
            "latest_tick": ticks[-1] if ticks else {},
            "recent_ticks": ticks[-20:],
            "recent_decisions": decisions[-40:],
            "recent_events": events[:80],
            "state": state,
            "reviews": reviews[-5:],
            "bot_registry": bot_scripts.verify_bot_registry_manifest(data_dir),
            "market_recorder": market_recorder.recorder_health(
                recorder_data_root(data_dir),
                max_stale_seconds=int(os.environ.get("AURUM_RECORDER_MAX_STALE_SECONDS", "180")),
            ),
            "backup": backup_status(data_dir),
            "replay": replay_status(data_dir),
            "risk_ledger": risk_ledger_status(data_dir),
        }
    )
    (operator_dir / "operator.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="robots" content="noindex,nofollow" />
  <title>Aurum Operator Diagnostics</title>
  <style>body{{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#050505;color:#f5f5f5;margin:0;padding:24px}}pre{{white-space:pre-wrap}}</style>
</head>
<body>
<h1>Aurum Operator Diagnostics</h1>
<p>Redacted operator-only diagnostics. Do not publish this directory.</p>
<pre>{esc(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True))}</pre>
</body>
</html>
"""
    (operator_dir / "index.html").write_text(html_doc, encoding="utf-8")
    return operator_dir / "index.html"


def render(args: argparse.Namespace) -> pathlib.Path:
    data_dir = pathlib.Path(args.data_dir)
    out_dir = pathlib.Path(args.output_dir)
    env_file = pathlib.Path(args.env_file) if args.env_file else None
    operator_output = str(getattr(args, "operator_output_dir", "") or os.environ.get("AURUM_OPERATOR_DASHBOARD_DIR", "")).strip()
    duel.ensure_data_dir(data_dir)
    strategy_rules.ensure_default_rules(data_dir)
    env = env_public(env_file)
    state = read_json(data_dir / "state.json", {"accounts": {}})
    ticks = read_jsonl(data_dir / "ticks.jsonl", limit=320)
    decisions = read_jsonl(data_dir / "decisions.jsonl", limit=240)
    reviews = read_reviews(data_dir)
    rules = strategy_rules.summarize_rules(data_dir)
    scores = latest_scores(state, ticks)
    scores.sort(key=lambda r: float(r.get("score", 0.0) or 0.0), reverse=True)
    latest_tick = ticks[-1] if ticks else {}
    btc, latest_btc_market, btc_source = bitcoin_series(data_dir, ticks, state)
    roi: Dict[str, List[Dict[str, Any]]] = {}
    public_events = public_tick_events(ticks)
    operator_events = extract_trade_events(ticks, decisions)
    runtime = public_runtime_status(data_dir, latest_tick)
    updated_at = utc_now()
    universe = env.get("AURUM_DUEL_UNIVERSE", "bitcoin").lower() or "bitcoin"
    contest_days = env.get("AURUM_FIRST_CONTEST_DAYS", "7")
    min_interval = env.get("AURUM_BOT_MIN_INTERVAL_SEC", "5")
    mode = env.get("AURUM_DUEL_MODE", latest_tick.get("mode", "review_only"))
    market_question = latest_btc_market.get("question") if latest_btc_market else "Waiting for first Bitcoin snapshot"
    latest_btc_price = btc[-1]["value"] if btc else None

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex,nofollow" />
  <title>Aurum BTC Paper Duel Terminal</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="terminal">
    <aside class="left-rail">
      <section class="rail-section">
        <div class="rail-title"><span>Contest</span><span class="pill amber">first {esc(contest_days)} days</span></div>
        <div class="big-number">BTC only</div>
        <div class="caption">第一版只交易 Bitcoin 相关 Polymarket 市场。paper engine 消费独立 market_recorder 的同源数据；agent 只比策略，不比谁抓到不同盘口。</div>
      </section>
      <section class="rail-section">
        <div class="rail-title"><span>Recorder</span><span>market data</span></div>
        {recorder_panel(data_dir)}
      </section>
      <section class="rail-section">
        <div class="rail-title"><span>Runtime</span><span>proof status</span></div>
        {runtime_panel(runtime)}
      </section>
      <section class="rail-section">
        <div class="rail-title"><span>Agents</span><span>{esc(mode)}</span></div>
        {agent_status_panel(scores, state, env, data_dir)}
      </section>
      <section class="rail-section">
        <div class="rail-title"><span>Positions</span><span>paper</span></div>
        {positions_panel(scores)}
      </section>
      <section class="rail-section">
        <div class="rail-title"><span>Polymarket Rules</span><span>fees</span></div>
        {polymarket_rules_panel()}
      </section>
      <section class="rail-section">
        <div class="rail-title"><span>Rules</span><span>visible</span></div>
        <div class="caption">bot v2 目标：agent 写受限策略规格；固定执行引擎做 5s+ paper 自动交易。</div>
        <pre class="rules">{esc(rule_excerpt(rules))}</pre>
      </section>
    </aside>

    <section class="center-stage">
      <div class="topbar">
        <div class="brand"><h1>Aurum Trading Terminal</h1><span>paper-only · no wallet · no live order</span></div>
        <div class="meta"><span>generated {esc(updated_at)}</span><span>ticks {len(ticks)}</span><span>btc frames {len(btc)}</span></div>
      </div>
      {data_quality_banner(latest_tick, ticks, env)}
      <div class="chart-wrap">
        <div class="chart-title">
          <div>
            <h2>Bitcoin recorder line × coarse score bands</h2>
            <p>{esc(market_question)} · latest BTC market price {fmt_price(latest_btc_price)} · source {esc(btc_source)}</p>
          </div>
          <div class="legend-line">
            <span style="color:var(--btc)">● BTC market line</span>
            <span style="color:var(--superwing)">● SuperWing score band</span>
            <span style="color:var(--deepseek)">● DeepSeek score band</span>
          </div>
        </div>
        {trade_chart(btc, roi, [])}
      </div>
      <div class="stage-footer">
        <div class="footer-cell"><span>Universe</span><b>{esc(universe)}</b></div>
        <div class="footer-cell"><span>Fastest bot interval</span><b>{esc(min_interval)}s hard floor</b></div>
        <div class="footer-cell"><span>Runtime state</span><b>{esc(runtime.get('completion_state'))}</b></div>
        <div class="footer-cell"><span>Latest 5h review</span><b>{esc(trunc(latest_review_summary(reviews), 72))}</b></div>
      </div>
    </section>

    <aside class="right-log">
      <div class="log-head"><h2>Activity Log</h2><span class="pill">coarse tick summary</span></div>
      <div class="log-list">{event_log(public_events)}</div>
    </aside>
  </main>
</body>
</html>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html_doc, encoding="utf-8")
    manifest = {
        "ok": True,
        "view": "public_trade_terminal_v3",
        "generated_at": updated_at,
        "tick_count": len(ticks),
        "btc_frame_count": len(btc),
        "latest_tick": latest_tick.get("tick_id"),
        "latest_review": (reviews[-1] if reviews else {}).get("review_id"),
        "universe": universe,
        "market_question": market_question,
        "scores": coarse_scores(scores),
        "runtime": runtime,
        "operator_output_enabled": bool(operator_output),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if operator_output:
        write_operator_output(
            pathlib.Path(operator_output),
            data_dir=data_dir,
            state=state,
            ticks=ticks,
            decisions=decisions,
            reviews=reviews,
            scores=scores,
            events=operator_events,
            runtime=runtime,
        )
    return out_dir / "index.html"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate Aurum public static trading terminal dashboard")
    p.add_argument("--data-dir", default="data/paper_duel")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--output-dir", default="public/dashboard")
    p.add_argument("--operator-output-dir", default="")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    path = render(args)
    print(json.dumps({"ok": True, "path": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
