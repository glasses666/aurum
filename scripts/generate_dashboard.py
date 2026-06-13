#!/usr/bin/env python3
"""Generate the public Aurum paper-duel transparency dashboard.

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
from typing import Any, Dict, Iterable, List, Optional

import agent_duel as duel
import strategy_rules

AGENT_LABELS = {
    "superwing": "SuperWing baseline",
    "deepseek": "DeepSeek agent",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_jsonl(path: pathlib.Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    rows = []
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
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def fmt_money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return "0.00"


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except Exception:
        return "+0.00%"


def env_public(env_file: Optional[pathlib.Path]) -> Dict[str, str]:
    allowed = {
        "AURUM_DUEL_MODE",
        "AURUM_DUEL_LIMIT",
        "AURUM_DUEL_MIN_VOLUME",
        "AURUM_DUEL_MAX_ORDERS",
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


def score_chart(ticks: List[Dict[str, Any]]) -> str:
    series: Dict[str, List[float]] = {"superwing": [], "deepseek": []}
    labels: List[str] = []
    for tick in ticks[-48:]:
        labels.append(str(tick.get("ts", tick.get("tick_id", "")))[:16])
        by_agent = {row.get("agent_id"): row for row in tick.get("scores", []) if isinstance(row, dict)}
        for agent in series:
            series[agent].append(float(by_agent.get(agent, {}).get("score", 0.0)))
    if not labels:
        labels = ["start", "now"]
        series = {"superwing": [0.0, 0.0], "deepseek": [0.0, 0.0]}
    values = [v for arr in series.values() for v in arr] or [0.0]
    low = min(values + [-0.5])
    high = max(values + [0.5])
    if high == low:
        high += 1
        low -= 1
    width, height = 820, 260
    pad_l, pad_r, pad_t, pad_b = 52, 24, 24, 42
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def pt(idx: int, value: float, n: int) -> str:
        x = pad_l + (idx / max(1, n - 1)) * plot_w
        y = pad_t + (1 - ((value - low) / (high - low))) * plot_h
        return f"{x:.1f},{y:.1f}"

    lines = []
    colors = {"superwing": "#8b5cf6", "deepseek": "#22d3ee"}
    for agent, arr in series.items():
        points = " ".join(pt(i, v, len(arr)) for i, v in enumerate(arr))
        lines.append(f'<polyline points="{points}" fill="none" stroke="{colors[agent]}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
    zero_y = pad_t + (1 - ((0 - low) / (high - low))) * plot_h
    return f"""
    <svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="ROI score history">
      <rect x="0" y="0" width="{width}" height="{height}" rx="18" fill="rgba(255,255,255,.035)"/>
      <line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width-pad_r}" y2="{zero_y:.1f}" stroke="rgba(255,255,255,.22)" stroke-dasharray="5 7"/>
      <text x="{pad_l}" y="20" fill="rgba(255,255,255,.55)" font-size="12">score / ROI%</text>
      <text x="{pad_l}" y="{height-12}" fill="rgba(255,255,255,.45)" font-size="11">{esc(labels[0])}</text>
      <text x="{width-pad_r-150}" y="{height-12}" fill="rgba(255,255,255,.45)" font-size="11">{esc(labels[-1])}</text>
      {''.join(lines)}
    </svg>
    """


def agent_cards(scores: List[Dict[str, Any]], state: Dict[str, Any], env: Dict[str, str]) -> str:
    by_agent = {row.get("agent_id"): row for row in scores}
    out = []
    for agent in duel.AGENTS:
        row = by_agent.get(agent, {})
        account = state.get("accounts", {}).get(agent, {}) if isinstance(state, dict) else {}
        model = "deterministic baseline"
        if agent == "deepseek":
            model = env.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        out.append(f"""
        <section class="agent-card {agent}">
          <div class="agent-top"><span class="agent-name">{esc(AGENT_LABELS[agent])}</span><span class="pill">{esc(model)}</span></div>
          <div class="metrics">
            <div><span>初始资金</span><b>{fmt_money(account.get('starting_equity', row.get('starting_equity', 1500)))}</b></div>
            <div><span>目前资金</span><b>{fmt_money(row.get('portfolio_value', account.get('cash', 1500)))}</b></div>
            <div><span>现金</span><b>{fmt_money(row.get('cash', account.get('cash', 1500)))}</b></div>
            <div><span>ROI / 分数</span><b>{fmt_pct(row.get('roi', 0))}</b></div>
          </div>
          <p class="muted">持仓 {esc(row.get('open_positions', len(account.get('positions', {}))))} · 已记录交易 {esc(len(account.get('trades', [])))} · 风险事件 {esc(len(account.get('risk_events', [])))}</p>
        </section>
        """)
    return "\n".join(out)


def flow_cards(env: Dict[str, str], rules: Dict[str, Any], latest_tick: Optional[Dict[str, Any]], latest_review: Optional[Dict[str, Any]]) -> str:
    mode = env.get("AURUM_DUEL_MODE", latest_tick.get("mode", "review_only") if latest_tick else "review_only")
    review_model = env.get("AURUM_REVIEW_MODEL", "deepseek-v4-pro")
    auto_promote = env.get("AURUM_RULE_AUTO_PROMOTE", "false")
    ds_apply = env.get("AURUM_DEEPSEEK_ALLOW_PAPER_APPLY", "false")
    return f"""
    <section class="flow">
      <div class="step"><b>1 抓市场</b><span>VPS 每小时取同一份 Polymarket slate</span></div>
      <div class="arrow">→</div>
      <div class="step"><b>2 同场决策</b><span>SuperWing + DeepSeek 共用 snapshot</span></div>
      <div class="arrow">→</div>
      <div class="step"><b>3 本地风控</b><span>buy-only · max_orders={esc(env.get('AURUM_DUEL_MAX_ORDERS', env.get('AURUM_DEEPSEEK_MAX_ORDERS', '2')))} · DS notional≤{esc(env.get('AURUM_DEEPSEEK_MAX_NOTIONAL', '45'))}</span></div>
      <div class="arrow">→</div>
      <div class="step"><b>4 账本模式</b><span class="status">{esc(mode)} · DS paper apply={esc(ds_apply)}</span></div>
      <div class="arrow">→</div>
      <div class="step"><b>5 5h复盘</b><span>{esc(review_model)} · prompt auto-update={esc(auto_promote)}</span></div>
    </section>
    <section class="legend">
      <div><b>当前不是黑箱：</b>网站公开显示资金、tick、决策、拒单、规则、复盘版本。</div>
      <div><b>安全边界：</b>没有真钱钱包、没有私钥、没有真实下单；公开页不输出任何 API key。</div>
      <div><b>最新 tick：</b>{esc((latest_tick or {}).get('tick_id', 'none'))}</div>
      <div><b>最新复盘：</b>{esc((latest_review or {}).get('review_id', 'none'))}</div>
    </section>
    """


def latest_decisions(ticks: List[Dict[str, Any]], decisions: List[Dict[str, Any]], limit: int = 14) -> str:
    rows = []
    # Prefer tick-level records because they show same-snapshot decisions.
    for tick in ticks[-limit:][::-1]:
        agents = tick.get("agents", {})
        for agent in duel.AGENTS:
            info = agents.get(agent, {}) if isinstance(agents, dict) else {}
            decision = info.get("decision", {}) if isinstance(info, dict) else {}
            result = info.get("result", {}) if isinstance(info, dict) else {}
            orders = decision.get("orders", []) or []
            fills = result.get("fills", []) or []
            rejections = result.get("rejections", []) or []
            rows.append(f"""
            <li>
              <span class="time">{esc(tick.get('ts', '')[:19])}</span>
              <b>{esc(agent)}</b>
              <span>orders {len(orders)} · fills {len(fills)} · rejects {len(rejections)}</span>
              <em>{esc(decision.get('notes', ''))}</em>
            </li>
            """)
    if not rows:
        for item in decisions[-limit:][::-1]:
            decision = item.get("decision", {})
            rows.append(f"<li><span class='time'>{esc(item.get('ts','')[:19])}</span><b>{esc(item.get('agent_id'))}</b><span>orders {len(decision.get('orders', []) or [])}</span><em>{esc(decision.get('notes',''))}</em></li>")
    return "<ul class='timeline'>" + "\n".join(rows[:limit]) + "</ul>"


def review_panel(reviews: List[Dict[str, Any]]) -> str:
    if not reviews:
        return "<p class='muted'>还没有 5h 高级复盘记录。</p>"
    latest = reviews[-1]
    findings = latest.get("findings", []) or []
    changes = latest.get("promoted", {}) or latest.get("proposed", {}) or {}
    return f"""
    <div class="review-card">
      <div class="agent-top"><span class="agent-name">最新 5h 高级复盘</span><span class="pill">{esc(latest.get('review_model', ''))}</span></div>
      <p>{esc(latest.get('summary', ''))}</p>
      <p class="muted">review_id={esc(latest.get('review_id', ''))} · auto_promote={esc(latest.get('auto_promote', False))}</p>
      <h3>发现</h3>
      <ul>{''.join('<li>'+esc(x)+'</li>' for x in findings[:6])}</ul>
      <h3>规则更新</h3>
      <pre>{esc(json.dumps(changes, ensure_ascii=False, indent=2)[:2600])}</pre>
    </div>
    """


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


def rules_panel(rules: Dict[str, Any]) -> str:
    sw = rules.get("superwing", {})
    ds = rules.get("deepseek_rules_excerpt", "")
    versions = rules.get("versions", []) or []
    return f"""
    <div class="rules-grid">
      <section>
        <h3>SuperWing 当前规则</h3>
        <pre>{esc(json.dumps(sw, ensure_ascii=False, indent=2))}</pre>
      </section>
      <section>
        <h3>DeepSeek 当前 prompt 规则</h3>
        <pre>{esc(ds)}</pre>
      </section>
    </div>
    <h3>规则版本流水</h3>
    <ul class="compact-list">{''.join('<li>'+esc(v.get('ts',''))+' · '+esc(v.get('agent_id',''))+' · '+esc(v.get('action',''))+'</li>' for v in versions[-8:])}</ul>
    """


def render(args: argparse.Namespace) -> pathlib.Path:
    data_dir = pathlib.Path(args.data_dir)
    out_dir = pathlib.Path(args.output_dir)
    env_file = pathlib.Path(args.env_file) if args.env_file else None
    duel.ensure_data_dir(data_dir)
    strategy_rules.ensure_default_rules(data_dir)
    env = env_public(env_file)
    state = read_json(data_dir / "state.json", {"accounts": {}})
    ticks = read_jsonl(data_dir / "ticks.jsonl", limit=240)
    decisions = read_jsonl(data_dir / "decisions.jsonl", limit=200)
    reviews = read_reviews(data_dir)
    rules = strategy_rules.summarize_rules(data_dir)
    scores = latest_scores(state, ticks)
    latest_tick = ticks[-1] if ticks else None
    latest_review = reviews[-1] if reviews else None
    updated_at = utc_now()

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex,nofollow" />
  <title>Aurum Paper Duel Transparency</title>
  <style>
    :root {{ color-scheme: dark; --bg:#070915; --panel:rgba(255,255,255,.065); --line:rgba(255,255,255,.12); --text:#eff6ff; --muted:rgba(239,246,255,.66); --cyan:#22d3ee; --violet:#8b5cf6; --green:#34d399; --amber:#fbbf24; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",sans-serif; background: radial-gradient(circle at top left, rgba(139,92,246,.28), transparent 36rem), radial-gradient(circle at top right, rgba(34,211,238,.18), transparent 34rem), var(--bg); color:var(--text); }}
    main {{ width:min(1180px, calc(100vw - 32px)); margin:0 auto; padding:34px 0 60px; }}
    header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:24px; }}
    h1 {{ margin:0; font-size:clamp(32px,5vw,58px); letter-spacing:-.05em; }}
    h2 {{ margin:28px 0 14px; font-size:22px; letter-spacing:-.02em; }}
    h3 {{ margin:16px 0 8px; font-size:15px; color:rgba(255,255,255,.78); }}
    .subtitle {{ margin:8px 0 0; color:var(--muted); max-width:760px; line-height:1.55; }}
    .stamp {{ text-align:right; color:var(--muted); font-size:13px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }}
    .agent-card, .panel, .review-card, .legend, .flow, .rules-grid section {{ border:1px solid var(--line); background:linear-gradient(180deg, rgba(255,255,255,.09), rgba(255,255,255,.045)); border-radius:24px; padding:18px; box-shadow:0 24px 70px rgba(0,0,0,.24); }}
    .agent-card.deepseek {{ border-color:rgba(34,211,238,.28); }} .agent-card.superwing {{ border-color:rgba(139,92,246,.32); }}
    .agent-top {{ display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:14px; }}
    .agent-name {{ font-weight:700; font-size:20px; }}
    .pill {{ border:1px solid var(--line); background:rgba(255,255,255,.08); color:rgba(255,255,255,.78); border-radius:999px; padding:6px 10px; font-size:12px; white-space:nowrap; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
    .metrics div {{ border:1px solid rgba(255,255,255,.08); border-radius:16px; padding:12px; background:rgba(0,0,0,.16); }}
    .metrics span {{ display:block; color:var(--muted); font-size:12px; }}
    .metrics b {{ display:block; margin-top:6px; font-size:22px; letter-spacing:-.03em; }}
    .muted {{ color:var(--muted); line-height:1.55; }}
    .flow {{ display:grid; grid-template-columns:1fr auto 1fr auto 1fr auto 1fr auto 1fr; gap:10px; align-items:stretch; }}
    .step {{ border-radius:18px; padding:14px; background:rgba(0,0,0,.2); border:1px solid rgba(255,255,255,.08); min-height:100px; }}
    .step b, .step span {{ display:block; }} .step span {{ margin-top:10px; color:var(--muted); line-height:1.35; font-size:13px; }} .status {{ color:var(--green)!important; }}
    .arrow {{ align-self:center; color:rgba(255,255,255,.36); }}
    .legend {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 20px; margin-top:12px; color:var(--muted); }}
    .chart {{ width:100%; height:auto; display:block; }}
    .timeline, .compact-list {{ list-style:none; padding:0; margin:0; display:grid; gap:8px; }}
    .timeline li, .compact-list li {{ display:grid; grid-template-columns:150px 100px 150px 1fr; gap:10px; align-items:start; border:1px solid rgba(255,255,255,.08); border-radius:14px; padding:10px; background:rgba(0,0,0,.18); }}
    .timeline em {{ color:var(--muted); font-style:normal; }} .time {{ color:rgba(255,255,255,.48); font-variant-numeric:tabular-nums; }}
    .rules-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
    pre {{ overflow:auto; max-height:430px; margin:0; padding:14px; border-radius:16px; background:rgba(0,0,0,.34); border:1px solid rgba(255,255,255,.08); color:rgba(239,246,255,.82); line-height:1.45; font-size:12px; white-space:pre-wrap; }}
    footer {{ margin-top:34px; color:rgba(255,255,255,.42); font-size:12px; }}
    @media (max-width:900px) {{ .grid,.rules-grid,.legend {{ grid-template-columns:1fr; }} .flow {{ grid-template-columns:1fr; }} .arrow {{ display:none; }} .metrics {{ grid-template-columns:repeat(2,1fr); }} header {{ display:block; }} .stamp {{ text-align:left; margin-top:12px; }} .timeline li {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Aurum Paper Duel</h1>
      <p class="subtitle">公开透明面板：LLM 资金、交易动态、规则流程、统计图和 5h 高级复盘。当前仍是 paper-only；页面不含任何 API key、钱包、私钥或真实交易凭证。</p>
    </div>
    <div class="stamp">generated<br>{esc(updated_at)}<br>tick count {len(ticks)}</div>
  </header>

  <div class="grid">{agent_cards(scores, state, env)}</div>

  <h2>一眼看懂：当前交易流程</h2>
  {flow_cards(env, rules, latest_tick, latest_review)}

  <h2>统计图</h2>
  <section class="panel">{score_chart(ticks)}<p class="muted">紫色=SuperWing，青色=DeepSeek。Score = mark-to-market ROI × 100。</p></section>

  <h2>交易动态</h2>
  <section class="panel">{latest_decisions(ticks, decisions)}</section>

  <h2>5h 高级复盘与规则更新</h2>
  <section class="panel">{review_panel(reviews)}</section>

  <h2>当前规则 / Prompt</h2>
  <section class="panel">{rules_panel(rules)}</section>

  <footer>Research-only public transparency page. No live wallet, no private key, no real order execution.</footer>
</main>
</body>
</html>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html_doc, encoding="utf-8")
    manifest = {
        "ok": True,
        "generated_at": updated_at,
        "tick_count": len(ticks),
        "latest_tick": (latest_tick or {}).get("tick_id"),
        "latest_review": (latest_review or {}).get("review_id"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_dir / "index.html"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate Aurum public static dashboard")
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
