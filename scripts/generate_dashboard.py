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
from typing import Any, Dict, Iterable, List, Optional, Tuple

import agent_duel as duel
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


def env_public(env_file: Optional[pathlib.Path]) -> Dict[str, str]:
    allowed = {
        "AURUM_DUEL_MODE",
        "AURUM_DUEL_LIMIT",
        "AURUM_DUEL_MIN_VOLUME",
        "AURUM_DUEL_MAX_ORDERS",
        "AURUM_DUEL_UNIVERSE",
        "AURUM_DUEL_SEARCH_QUERY",
        "AURUM_BOT_MIN_INTERVAL_SEC",
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


def agent_status_panel(scores: List[Dict[str, Any]], state: Dict[str, Any], env: Dict[str, str]) -> str:
    score_by_agent = {row.get("agent_id"): row for row in scores if isinstance(row, dict)}
    ranked = sorted(scores, key=lambda r: float(r.get("score", 0.0) or 0.0), reverse=True)
    rank_by_agent = {row.get("agent_id"): idx + 1 for idx, row in enumerate(ranked)}
    parts: List[str] = []
    for agent in duel.AGENTS:
        row = score_by_agent.get(agent, {})
        account = state.get("accounts", {}).get(agent, {}) if isinstance(state, dict) else {}
        model = "fixed baseline"
        if agent == "deepseek":
            model = env.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        color = AGENT_COLORS[agent]
        parts.append(f"""
        <div class="agent-row">
          <div class="agent-head">
            <span class="agent-name" style="color:{color}"><span class="dot"></span>{esc(AGENT_LABELS[agent])}</span>
            <span class="pill">rank #{esc(rank_by_agent.get(agent, '—'))}</span>
          </div>
          <div class="agent-sub">{esc(model)} · {esc(env.get('AURUM_DUEL_MODE', account.get('mode', 'paper')))}</div>
          <div class="agent-stats">
            <span>equity<b>{fmt_money(row.get('portfolio_value', account.get('cash', 1500)))}</b></span>
            <span>ROI<b>{fmt_pct(row.get('roi', 0))}</b></span>
            <span>cash<b>{fmt_money(row.get('cash', account.get('cash', 1500)))}</b></span>
            <span>trades<b>{esc(len(account.get('trades', [])))}</b></span>
          </div>
        </div>
        """)
    return "\n".join(parts)


def positions_panel(scores: List[Dict[str, Any]]) -> str:
    rows: List[str] = []
    for score in scores:
        for detail in score.get("details", []) or []:
            rows.append(f"""
            <div class="position-row">
              <div class="rank-head"><span>{esc(score.get('agent_id'))}</span><span>{fmt_money(detail.get('value'))}</span></div>
              <div class="tiny">{esc(detail.get('key'))} · shares {esc(round(float(detail.get('shares', 0.0)), 4))} · mark {fmt_price(detail.get('mark'))}</div>
            </div>
            """)
    if not rows:
        return "<div class='caption'>暂无持仓。review_only 阶段会先记录建议/拒单；开 paper_apply 后这里会变成真实纸盘仓位。</div>"
    return "\n".join(rows[:8])


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
        return "<div class='empty'>暂无交易事件。Bitcoin-only bot demo 会先显示 ORDER / FILL / REJECT / NOTE。</div>"
    rows = []
    for item in events[:80]:
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
    root = recorder_data_root(data_dir)
    health = market_recorder.recorder_health(root, max_stale_seconds=int(os.environ.get("AURUM_RECORDER_MAX_STALE_SECONDS", "180")))
    last = health.get("last_capture", {}) if isinstance(health.get("last_capture"), dict) else {}
    sources = last.get("sources", {}) if isinstance(last.get("sources"), dict) else {}
    status = "live" if health.get("ok") else "check"
    pill = "green" if health.get("ok") else "amber"
    age = health.get("age_seconds", "n/a")
    return f"""
      <div class=\"rule-line\"><span>Status</span><b><span class=\"pill {pill}\">{esc(status)}</span></b></div>
      <div class=\"rule-line\"><span>Age</span><b>{esc(age)}s</b></div>
      <div class=\"rule-line\"><span>Markets</span><b>{esc(last.get('market_count', 0))}</b></div>
      <div class=\"rule-line\"><span>Books</span><b>{esc((sources.get('clob_book') or {}).get('ok_frames', 0))}</b></div>
      <div class=\"caption\">raw/normalized recorder root: {esc(str(root))}</div>
      <div class=\"caption\">{esc(', '.join(health.get('errors', [])) or 'Gamma/CLOB/Data API frames captured independently from paper fills.')}</div>
    """


def render(args: argparse.Namespace) -> pathlib.Path:
    data_dir = pathlib.Path(args.data_dir)
    out_dir = pathlib.Path(args.output_dir)
    env_file = pathlib.Path(args.env_file) if args.env_file else None
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
    roi = roi_series(ticks, state, scores)
    events = extract_trade_events(ticks, decisions)
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
        <div class="rail-title"><span>Agents</span><span>{esc(mode)}</span></div>
        {agent_status_panel(scores, state, env)}
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
      <div class="chart-wrap">
        <div class="chart-title">
          <div>
            <h2>Bitcoin price × agent ROI × trade points</h2>
            <p>{esc(market_question)} · latest BTC market price {fmt_price(latest_btc_price)} · source {esc(btc_source)}</p>
          </div>
          <div class="legend-line">
            <span style="color:var(--btc)">● BTC market line</span>
            <span style="color:var(--superwing)">● SuperWing ROI / trades</span>
            <span style="color:var(--deepseek)">● DeepSeek ROI / trades</span>
          </div>
        </div>
        {trade_chart(btc, roi, events)}
      </div>
      <div class="stage-footer">
        <div class="footer-cell"><span>Universe</span><b>{esc(universe)}</b></div>
        <div class="footer-cell"><span>Fastest bot interval</span><b>{esc(min_interval)}s hard floor</b></div>
        <div class="footer-cell"><span>Latest tick</span><b>{esc(latest_tick.get('tick_id', 'none'))}</b></div>
        <div class="footer-cell"><span>Latest 5h review</span><b>{esc(trunc(latest_review_summary(reviews), 72))}</b></div>
      </div>
    </section>

    <aside class="right-log">
      <div class="log-head"><h2>Event Log</h2><span class="pill">orders / fills / rejects / notes</span></div>
      <div class="log-list">{event_log(events)}</div>
    </aside>
  </main>
</body>
</html>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html_doc, encoding="utf-8")
    manifest = {
        "ok": True,
        "view": "trade_terminal_v2",
        "generated_at": updated_at,
        "tick_count": len(ticks),
        "btc_frame_count": len(btc),
        "latest_tick": latest_tick.get("tick_id"),
        "latest_review": (reviews[-1] if reviews else {}).get("review_id"),
        "universe": universe,
        "market_question": market_question,
        "paper_execution_rules": duel.paper_execution_rules(),
        "market_recorder": market_recorder.recorder_health(recorder_data_root(data_dir), max_stale_seconds=int(os.environ.get("AURUM_RECORDER_MAX_STALE_SECONDS", "180"))),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_dir / "index.html"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate Aurum public static trading terminal dashboard")
    p.add_argument("--data-dir", default="data/paper_duel")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--output-dir", default="public/dashboard")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    path = render(args)
    print(json.dumps({"ok": True, "path": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
