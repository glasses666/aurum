#!/usr/bin/env python3
"""Five-hour advanced review for the Aurum paper duel.

The review model summarizes recent paper decisions, proposes strategy-rule updates,
validates them against local safety constraints, and optionally promotes them to
the current paper rules. It never touches live wallets or real orders.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import agent_duel as duel
import generate_dashboard
import strategy_rules


def read_jsonl(path: pathlib.Path, limit: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    return rows


def compact_tick(tick: Dict[str, Any]) -> Dict[str, Any]:
    agents = {}
    for agent_id, info in (tick.get("agents") or {}).items():
        decision = info.get("decision", {}) if isinstance(info, dict) else {}
        result = info.get("result", {}) if isinstance(info, dict) else {}
        agents[agent_id] = {
            "orders": decision.get("orders", [])[:3] if isinstance(decision.get("orders", []), list) else [],
            "notes": decision.get("notes", ""),
            "fills": result.get("fills", [])[:3] if isinstance(result.get("fills", []), list) else [],
            "rejections": result.get("rejections", [])[:3] if isinstance(result.get("rejections", []), list) else [],
        }
    return {
        "tick_id": tick.get("tick_id"),
        "ts": tick.get("ts"),
        "mode": tick.get("mode"),
        "applied": tick.get("applied"),
        "market_count": tick.get("market_count"),
        "scores": tick.get("scores", []),
        "agents": agents,
    }


def review_context(data_dir: pathlib.Path, limit_ticks: int) -> Dict[str, Any]:
    state = {}
    try:
        state = duel.load_state(data_dir)
    except Exception:
        state = {"accounts": {}}
    ticks = read_jsonl(data_dir / "ticks.jsonl", limit_ticks)
    decisions = read_jsonl(data_dir / "decisions.jsonl", limit_ticks * 2)
    prices = duel.market_price_map(state.get("last_markets", []))
    scores = []
    for agent in duel.AGENTS:
        account = state.get("accounts", {}).get(agent)
        if account:
            scores.append(duel.portfolio_value(account, prices))
    return {
        "now": duel.utc_now(),
        "mode": os.environ.get("AURUM_DUEL_MODE", "review_only"),
        "paper_only": True,
        "scores": scores,
        "current_rules": strategy_rules.summarize_rules(data_dir),
        "recent_ticks": [compact_tick(t) for t in ticks[-limit_ticks:]],
        "recent_decision_count": len(decisions),
        "required_output_schema": {
            "summary": "one short paragraph",
            "findings": ["short observation"],
            "superwing_rules": {
                "name": "string",
                "selection": "string",
                "price_min": "0.05-0.85",
                "price_max": "price_min+0.01 to 0.95",
                "max_notional": "1-45",
                "limit_buffer": "0.001-0.035",
                "confidence": "0.45-0.8",
                "min_volume": "0-2000000",
                "notes": "string",
            },
            "superwing_rationale": "why this rule change helps",
            "deepseek_rules_md": "markdown rules for DeepSeek, paper-only/buy-only/hold-if-no-edge",
            "deepseek_rationale": "why this prompt change helps",
            "risk_notes": ["safety note"],
            "public_dashboard_note": "one sentence for human transparency",
        },
    }


def call_review_model(ctx: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise duel.DuelError("DEEPSEEK_API_KEY is not set; cannot run strategy review")
    endpoint = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions").strip()
    if endpoint.rstrip("/").endswith("/v1"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"
    primary = os.environ.get("AURUM_REVIEW_MODEL", "deepseek-v4-pro").strip() or "deepseek-v4-pro"
    fallback = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    models = [primary] + ([fallback] if fallback != primary else [])
    system = (
        "You are the advanced strategy-review model for Aurum's PAPER-ONLY Polymarket duel. "
        "Improve transparency and strategy quality, but never propose real wallets, private keys, USDC deposits, logins, geoblock bypass, live trading, or risk-cap changes. "
        "Return JSON only. The runner will validate and may reject unsafe updates."
    )
    user = json.dumps(ctx, ensure_ascii=False)
    last_error = ""
    for model in models:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": 2400,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        if os.environ.get("AURUM_REVIEW_THINKING", "enabled").strip().lower() == "enabled":
            payload["thinking"] = {"type": "enabled"}
            effort = os.environ.get("AURUM_REVIEW_REASONING_EFFORT", "high").strip().lower()
            if effort in {"high", "max"}:
                payload["reasoning_effort"] = effort
            payload.pop("temperature", None)
        payloads = [(payload, False)]
        if payload.get("thinking", {}).get("type") == "enabled" and os.environ.get("AURUM_REVIEW_STRUCTURED_RETRY", "true").strip().lower() in {"1", "true", "yes", "on"}:
            retry_payload = dict(payload)
            retry_payload["thinking"] = {"type": "disabled"}
            retry_payload["temperature"] = 0.1
            retry_payload.pop("reasoning_effort", None)
            retry_payload["messages"] = [
                {"role": "system", "content": system + " Return the final answer in the content field as exactly one valid JSON object."},
                {"role": "user", "content": user + "\n\nThe prior response was not parseable JSON. Retry with one strict JSON object only."},
            ]
            payloads.append((retry_payload, True))
        model_error = ""
        for attempt_payload, retry_used in payloads:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(attempt_payload).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "aurum-strategy-review/0.1 (+paper-only)",
                },
                method="POST",
            )
            try:
                opener = duel.no_proxy_opener(False)
                with opener.open(req, timeout=90) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                out = duel.extract_deepseek_decision_from_response(data)
                out["review_model"] = model
                out["structured_retry_used"] = retry_used
                return out
            except urllib.error.HTTPError as exc:
                detail = exc.read(800).decode("utf-8", "replace") if exc.fp else ""
                model_error = f"{model} HTTP {exc.code}: {detail[:300]}"
                break
            except Exception as exc:
                model_error = f"{model}: {exc}"
                continue
        last_error = model_error
        continue
    raise duel.DuelError("strategy review model call failed: " + last_error)


def auto_promote_enabled(args: argparse.Namespace) -> bool:
    if args.no_promote:
        return False
    if args.auto_promote:
        return True
    allow = os.environ.get("AURUM_RULE_AUTO_PROMOTE", "false").strip().lower() in {"1", "true", "yes", "on"}
    confirm = os.environ.get("AURUM_RULE_OPERATOR_CONFIRM", "").strip() == "ALLOW_RULE_AUTO_PROMOTE"
    return allow and confirm


def run_review(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = pathlib.Path(args.data_dir)
    env_file = pathlib.Path(args.env_file) if args.env_file else None
    duel.ensure_data_dir(data_dir)
    duel.load_env_file(env_file)
    strategy_rules.ensure_default_rules(data_dir)
    ctx = review_context(data_dir, args.limit_ticks)
    try:
        review = call_review_model(ctx)
        model_failed = False
        model_error = ""
    except Exception as exc:
        model_failed = True
        model_error = str(exc)
        review = {
            "summary": "Strategy review model output was not parseable; no rule changes promoted in this maintenance cycle.",
            "findings": [f"review_model_error: {model_error[:500]}"],
            "superwing_rules": strategy_rules.load_superwing_rules(data_dir),
            "superwing_rationale": "Fallback only; keep current SuperWing rules.",
            "deepseek_rules_md": strategy_rules.load_deepseek_rules(data_dir),
            "deepseek_rationale": "Fallback only; keep current DeepSeek rules.",
            "risk_notes": ["No rule promotion was performed because the review model output failed validation."],
            "public_dashboard_note": "Strategy review fallback: malformed model output; current rules kept unchanged.",
            "review_model": "fallback_no_promote",
            "structured_retry_used": True,
        }
    review_id = duel.utc_now().replace(":", "").replace("+00:00", "Z")
    proposed: Dict[str, Any] = {}
    promoted: Dict[str, Any] = {}

    sw_candidate = review.get("superwing_rules")
    sw_raw: Dict[str, Any] = sw_candidate if isinstance(sw_candidate, dict) else {}
    sw_rules = strategy_rules.normalize_superwing_rules(sw_raw)
    sw_proposal = strategy_rules.write_proposal(data_dir, review_id, "superwing", "_rules.json", json.dumps(sw_rules, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    proposed["superwing_rules"] = str(sw_proposal)

    ds_rules_text = review.get("deepseek_rules_md") or strategy_rules.load_deepseek_rules(data_dir)
    ds_rules_text = strategy_rules.validate_deepseek_rules(str(ds_rules_text))
    ds_proposal = strategy_rules.write_proposal(data_dir, review_id, "deepseek", "_rules.md", ds_rules_text)
    proposed["deepseek_rules"] = str(ds_proposal)

    promote = (not model_failed) and auto_promote_enabled(args)
    if promote:
        promoted["superwing_rules"] = str(strategy_rules.promote_superwing_rules(data_dir, sw_rules, source=f"review:{review_id}", rationale=str(review.get("superwing_rationale", ""))))
        promoted["deepseek_rules"] = str(strategy_rules.promote_deepseek_rules(data_dir, ds_rules_text, source=f"review:{review_id}", rationale=str(review.get("deepseek_rationale", ""))))

    record = {
        "ok": True,
        "review_id": review_id,
        "ts": duel.utc_now(),
        "review_model": review.get("review_model"),
        "review_status": "fallback_no_promote" if model_failed else "model_ok",
        "review_model_error": model_error if model_failed else "",
        "structured_retry_used": bool(review.get("structured_retry_used")),
        "auto_promote": promote,
        "summary": review.get("summary", ""),
        "findings": review.get("findings", []),
        "risk_notes": review.get("risk_notes", []),
        "public_dashboard_note": review.get("public_dashboard_note", ""),
        "proposed": proposed,
        "promoted": promoted,
        "superwing_rationale": review.get("superwing_rationale", ""),
        "deepseek_rationale": review.get("deepseek_rationale", ""),
    }
    out_dir = data_dir / "strategy_reviews"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{review_id}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    dashboard_dir = args.dashboard_dir or os.environ.get("AURUM_PUBLIC_DASHBOARD_DIR", "/opt/aurum/public/dashboard")
    try:
        dash_args = argparse.Namespace(data_dir=str(data_dir), env_file=str(env_file or ""), output_dir=dashboard_dir)
        dashboard_path = generate_dashboard.render(dash_args)
        record["dashboard_path"] = str(dashboard_path)
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:
        record["dashboard_error"] = str(exc)
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run Aurum 5h strategy review and optional rule-prompt promotion")
    p.add_argument("--data-dir", default="data/paper_duel")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--dashboard-dir", default="")
    p.add_argument("--limit-ticks", type=int, default=24)
    p.add_argument("--auto-promote", action="store_true", help="promote validated rule updates even without env gate")
    p.add_argument("--no-promote", action="store_true", help="write proposals only")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        record = run_review(args)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
