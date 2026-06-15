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
import bot_scripts
import generate_dashboard
import strategy_rules


def read_jsonl(path: pathlib.Path, limit: int) -> List[Dict[str, Any]]:
    return generate_dashboard.read_jsonl(path, limit=limit)


def compact_score_row(row: Dict[str, Any]) -> Dict[str, Any]:
    allowed = (
        "agent_id",
        "rank",
        "score",
        "raw_roi_score",
        "roi",
        "drawdown",
        "open_positions",
        "trade_count",
        "order_count",
        "risk_event_count",
    )
    return {key: row.get(key) for key in allowed if key in row}


def compact_tick(tick: Dict[str, Any]) -> Dict[str, Any]:
    agents = {}
    for agent_id, info in (tick.get("agents") or {}).items():
        decision = info.get("decision", {}) if isinstance(info, dict) else {}
        result = info.get("result", {}) if isinstance(info, dict) else {}
        orders = decision.get("orders", []) if isinstance(decision.get("orders", []), list) else []
        fills = result.get("fills", []) if isinstance(result.get("fills", []), list) else []
        rejections = result.get("rejections", []) if isinstance(result.get("rejections", []), list) else []
        agents[agent_id] = {
            "order_count": len(orders),
            "fill_count": len(fills),
            "rejection_count": len(rejections),
        }
    return {
        "tick_id": tick.get("tick_id"),
        "ts": tick.get("ts"),
        "mode": tick.get("mode"),
        "applied": tick.get("applied"),
        "market_count": tick.get("market_count"),
        "scores": [compact_score_row(row) for row in tick.get("scores", []) if isinstance(row, dict)],
        "agents": agents,
    }


def _load_review_state(data_dir: pathlib.Path) -> Dict[str, Any]:
    try:
        return duel.load_state(data_dir)
    except Exception:
        return {"accounts": {}}


def _aggregate_agent_learning_context(
    state: Dict[str, Any],
    prices: Dict[Any, float],
    agent: str,
    replay_feedback: Dict[str, Any],
) -> Dict[str, Any]:
    per_agent_scoreboard = duel.scoreboard_context(state, prices, viewer_agent_id=agent)
    return {
        "agent_id": agent,
        "scope": "aggregate_only_no_raw_ledger",
        "own_scoreboard": per_agent_scoreboard.get("own_scoreboard"),
        "peer_scoreboard": per_agent_scoreboard.get("peer_scoreboard", []),
        "victory": per_agent_scoreboard.get("victory"),
        "replay_feedback": replay_feedback,
    }


def _isolated_agent_learning_context(
    state: Dict[str, Any],
    prices: Dict[Any, float],
    agent: str,
    replay_feedback: Dict[str, Any],
) -> Dict[str, Any]:
    accounts = state.get("accounts", {}) if isinstance(state.get("accounts"), dict) else {}
    raw_account = accounts.get(agent)
    account: Dict[str, Any] = raw_account if isinstance(raw_account, dict) else {}
    context = _aggregate_agent_learning_context(state, prices, agent, replay_feedback)
    context.update(
        {
            "scope": "target_agent_raw_ledger_only",
            "own_account": duel.compact_account(account),
        }
    )
    return context


def _required_output_schema() -> Dict[str, Any]:
    return {
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
        "deepseek_rules_md": "markdown rules for DeepSeek, paper-only/buy/sell/hold-if-no-edge",
        "deepseek_rationale": "why this prompt change helps",
        "bot_scripts": {
            "superwing": "optional mechanical JSON script with buy_when/sell_when/hold_when",
            "deepseek": "optional mechanical JSON script with buy_when/sell_when/hold_when",
        },
        "risk_notes": ["safety note"],
        "public_dashboard_note": "one sentence for human transparency",
    }


def review_context(data_dir: pathlib.Path, limit_ticks: int) -> Dict[str, Any]:
    state = _load_review_state(data_dir)
    ticks = read_jsonl(data_dir / "ticks.jsonl", limit_ticks)
    decisions = read_jsonl(data_dir / "decisions.jsonl", limit_ticks * 2)
    prices = duel.market_price_map(state.get("last_markets", []))
    scoreboard = duel.scoreboard_context(state, prices)
    replay_feedback = generate_dashboard.replay_status(data_dir)
    agent_learning_contexts = {
        agent: _aggregate_agent_learning_context(state, prices, agent, replay_feedback)
        for agent in duel.AGENTS
    }
    return {
        "now": duel.utc_now(),
        "mode": os.environ.get("AURUM_DUEL_MODE", "review_only"),
        "paper_only": True,
        "self_evolution_contract": "Use own ledger/fills/risk_events/replay outcomes to propose better bounded mechanical rules; strict DSL/review gates decide promotion; keep learning state per-agent isolated.",
        "learning_isolation_contract": "Shared review context is aggregate-only. Raw trades/fills/risk_events are supplied only in the isolated target-agent review context for one agent at a time.",
        "competition": scoreboard["competition"],
        "victory": scoreboard.get("victory"),
        "scores": scoreboard["scoreboard"],
        "agent_learning_contexts": agent_learning_contexts,
        "replay_feedback": replay_feedback,
        "current_rules": strategy_rules.summarize_rules(data_dir),
        "current_bot_scripts": {agent: bot_scripts.load_bot_script(data_dir, agent) for agent in duel.AGENTS},
        "recent_ticks": [compact_tick(t) for t in ticks[-limit_ticks:]],
        "recent_decision_count": len(decisions),
        "required_output_schema": _required_output_schema(),
    }


def _target_current_rules(data_dir: pathlib.Path, agent_id: str) -> Dict[str, Any]:
    if agent_id == "superwing":
        return {"superwing": strategy_rules.load_superwing_rules(data_dir)}
    if agent_id == "deepseek":
        text = strategy_rules.load_deepseek_rules(data_dir)
        return {"deepseek_rules_md": text, "deepseek_rules_length": len(text)}
    return {}


def agent_review_context(
    data_dir: pathlib.Path,
    agent_id: str,
    limit_ticks: int,
    *,
    base_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if agent_id not in duel.AGENTS:
        raise duel.DuelError(f"unknown review agent: {agent_id}")
    base = dict(base_context) if base_context is not None else review_context(data_dir, limit_ticks)
    state = _load_review_state(data_dir)
    prices = duel.market_price_map(state.get("last_markets", []))
    replay_feedback_raw = base.get("replay_feedback")
    replay_feedback: Dict[str, Any] = replay_feedback_raw if isinstance(replay_feedback_raw, dict) else generate_dashboard.replay_status(data_dir)
    isolated = _isolated_agent_learning_context(state, prices, agent_id, replay_feedback)
    base["target_agent_id"] = agent_id
    base["agent_learning_context"] = isolated
    base["agent_learning_contexts"] = {agent_id: isolated}
    base["current_rules"] = _target_current_rules(data_dir, agent_id)
    base["current_bot_scripts"] = {agent_id: bot_scripts.load_bot_script(data_dir, agent_id)}
    base["learning_isolation_contract"] = (
        "This model call is isolated for target_agent_id only. "
        "It may use own_account/recent_trades/recent_risk_events only for that target; "
        "peer data is aggregate peer_scoreboard only. Do not infer or copy peer raw ledgers, prompts, or strategy internals."
    )
    return base


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
    target_agent = str(ctx.get("target_agent_id") or "").strip()
    system = (
        "You are the advanced strategy-review model for Aurum's PAPER-ONLY Polymarket duel. "
        "Improve transparency and strategy quality, but never propose real wallets, private keys, USDC deposits, logins, geoblock bypass, live trading, or risk-cap changes. "
        "Return JSON only. The runner will validate and may reject unsafe updates."
    )
    if target_agent:
        system += (
            f" This review call is isolated for target_agent_id={target_agent}. "
            "Use only agent_learning_context.own_account as raw ledger evidence; peer_scoreboard is aggregate only. "
            "Do not infer or copy peer raw trades, prompts, or strategy internals."
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
                out["primary_review_model"] = primary
                out["review_model_fallback_used"] = model != primary
                out["review_model_usage"] = compact_model_usage(data.get("usage"))
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


def _list_field(review: Dict[str, Any], key: str) -> List[Any]:
    value = review.get(key)
    return value if isinstance(value, list) else ([] if value in (None, "") else [value])


USAGE_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens")


def compact_model_usage(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in USAGE_TOKEN_FIELDS:
        if key not in value:
            continue
        try:
            out[key] = int(value.get(key) or 0)
        except Exception:
            continue
    if "total_tokens" not in out:
        total = out.get("prompt_tokens", out.get("input_tokens", 0)) + out.get("completion_tokens", out.get("output_tokens", 0))
        if total:
            out["total_tokens"] = total
    return out


def merge_model_usage(agent_reviews: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    agents: Dict[str, Dict[str, Any]] = {}
    aggregate: Dict[str, int] = {}
    for agent_id in duel.AGENTS:
        usage = compact_model_usage(agent_reviews.get(agent_id, {}).get("review_model_usage"))
        agents[agent_id] = usage
        for key, value in usage.items():
            if isinstance(value, int):
                aggregate[key] = aggregate.get(key, 0) + value
    return {"agents": agents, "aggregate": aggregate}


def agent_review_record_path(data_dir: pathlib.Path, agent_id: str, review_id: str) -> pathlib.Path:
    return data_dir / "strategy_reviews" / "agents" / agent_id / f"{review_id}.json"


def write_agent_review_records(
    data_dir: pathlib.Path,
    review_id: str,
    agent_reviews: Dict[str, Dict[str, Any]],
    model_errors: Dict[str, str],
    review_status: str,
    validation_errors: List[str],
) -> Dict[str, str]:
    paths: Dict[str, str] = {}
    for agent_id in duel.AGENTS:
        review = agent_reviews.get(agent_id, {})
        payload = {
            "ok": agent_id in agent_reviews and agent_id not in model_errors,
            "review_id": review_id,
            "ts": duel.utc_now(),
            "agent_id": agent_id,
            "learning_scope": "target_agent_raw_ledger_only",
            "peer_scope": "aggregate_peer_scoreboard_only",
            "review_status": review_status,
            "model_error": model_errors.get(agent_id, "")[:500],
            "review_model": review.get("review_model", "fallback_no_promote" if agent_id in model_errors else "unknown"),
            "primary_review_model": review.get("primary_review_model"),
            "model_fallback_used": bool(review.get("review_model_fallback_used")) or bool(model_errors),
            "structured_retry_used": bool(review.get("structured_retry_used")) or bool(model_errors),
            "review_model_usage": compact_model_usage(review.get("review_model_usage")),
            "summary": review.get("summary", ""),
            "findings": _list_field(review, "findings"),
            "risk_notes": _list_field(review, "risk_notes"),
            "public_dashboard_note": review.get("public_dashboard_note", ""),
            "validation_errors": validation_errors,
        }
        path = agent_review_record_path(data_dir, agent_id, review_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths[agent_id] = str(path)
    return paths


def merge_isolated_agent_reviews(agent_reviews: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    superwing_review = agent_reviews.get("superwing", {})
    deepseek_review = agent_reviews.get("deepseek", {})
    merged_bot_scripts: Dict[str, Any] = {}
    for agent_id, review in agent_reviews.items():
        raw_scripts = review.get("bot_scripts") if isinstance(review.get("bot_scripts"), dict) else {}
        candidate = raw_scripts.get(agent_id) if isinstance(raw_scripts, dict) else None
        if isinstance(candidate, dict):
            merged_bot_scripts[agent_id] = candidate
    model_parts = []
    for agent_id in duel.AGENTS:
        model = str(agent_reviews.get(agent_id, {}).get("review_model") or "unknown")[:120]
        model_parts.append(f"{agent_id}={model}")
    summaries = [str(agent_reviews.get(agent_id, {}).get("summary") or "").strip() for agent_id in duel.AGENTS]
    notes = [str(agent_reviews.get(agent_id, {}).get("public_dashboard_note") or "").strip() for agent_id in duel.AGENTS]
    return {
        "summary": " | ".join(item for item in summaries if item) or "Per-agent isolated strategy reviews completed.",
        "findings": [item for agent_id in duel.AGENTS for item in _list_field(agent_reviews.get(agent_id, {}), "findings")],
        "superwing_rules": superwing_review.get("superwing_rules"),
        "superwing_rationale": superwing_review.get("superwing_rationale", ""),
        "deepseek_rules_md": deepseek_review.get("deepseek_rules_md"),
        "deepseek_rationale": deepseek_review.get("deepseek_rationale", ""),
        "bot_scripts": merged_bot_scripts,
        "risk_notes": [item for agent_id in duel.AGENTS for item in _list_field(agent_reviews.get(agent_id, {}), "risk_notes")],
        "public_dashboard_note": " | ".join(item for item in notes if item) or "Per-agent isolated strategy reviews completed.",
        "review_model": "per-agent-isolated:" + ",".join(model_parts),
        "review_model_fallback_used": any(bool(agent_reviews.get(agent_id, {}).get("review_model_fallback_used")) for agent_id in duel.AGENTS),
        "structured_retry_used": any(bool(agent_reviews.get(agent_id, {}).get("structured_retry_used")) for agent_id in duel.AGENTS),
    }


def run_review(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = pathlib.Path(args.data_dir)
    env_file = pathlib.Path(args.env_file) if args.env_file else None
    duel.ensure_data_dir(data_dir)
    duel.load_env_file(env_file)
    strategy_rules.ensure_default_rules(data_dir)
    bot_scripts.ensure_default_bot_scripts(data_dir)
    base_ctx = review_context(data_dir, args.limit_ticks)
    agent_reviews: Dict[str, Dict[str, Any]] = {}
    model_errors: Dict[str, str] = {}
    for agent_id in duel.AGENTS:
        isolated_ctx = agent_review_context(data_dir, agent_id, args.limit_ticks, base_context=base_ctx)
        try:
            agent_reviews[agent_id] = call_review_model(isolated_ctx)
        except Exception as exc:
            model_errors[agent_id] = str(exc)
    model_failed = bool(model_errors)
    if model_failed:
        model_error = "; ".join(f"{agent}: {error[:500]}" for agent, error in model_errors.items())
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
    else:
        model_error = ""
        review = merge_isolated_agent_reviews(agent_reviews)
    model_fallback_used = (not model_failed) and bool(review.get("review_model_fallback_used"))
    review_id = duel.utc_now().replace(":", "").replace("+00:00", "Z")
    proposed: Dict[str, Any] = {}
    promoted: Dict[str, Any] = {}

    validation_errors = []
    sw_candidate = review.get("superwing_rules")
    sw_raw: Dict[str, Any] = sw_candidate if isinstance(sw_candidate, dict) else {}
    placeholder_fields = [
        key
        for key in ("name", "selection", "notes", "review_rationale")
        if strategy_rules.contains_placeholder_text(sw_raw.get(key))
    ]
    if strategy_rules.contains_placeholder_text(review.get("superwing_rationale")):
        placeholder_fields.append("superwing_rationale")
    if placeholder_fields:
        validation_errors.append("superwing placeholder fields: " + ", ".join(sorted(set(placeholder_fields))))
    sw_rules = strategy_rules.normalize_superwing_rules(sw_raw)
    sw_proposal = strategy_rules.write_proposal(data_dir, review_id, "superwing", "_rules.json", json.dumps(sw_rules, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    proposed["superwing_rules"] = str(sw_proposal)

    ds_rules_text = review.get("deepseek_rules_md") or strategy_rules.load_deepseek_rules(data_dir)
    try:
        ds_rules_text = strategy_rules.validate_deepseek_rules(str(ds_rules_text))
    except ValueError as exc:
        validation_errors.append("deepseek rules rejected: " + str(exc))
        ds_rules_text = strategy_rules.load_deepseek_rules(data_dir)
    ds_proposal = strategy_rules.write_proposal(data_dir, review_id, "deepseek", "_rules.md", ds_rules_text)
    proposed["deepseek_rules"] = str(ds_proposal)

    bot_script_candidates: Dict[str, Dict[str, Any]] = {}
    raw_bot_scripts = review.get("bot_scripts") if isinstance(review.get("bot_scripts"), dict) else {}
    for agent_id in duel.AGENTS:
        current_script = bot_scripts.load_bot_script(data_dir, agent_id)
        candidate = raw_bot_scripts.get(agent_id) if isinstance(raw_bot_scripts, dict) else None
        if isinstance(candidate, dict):
            candidate = bot_scripts.merge_script_update(current_script, candidate)
        else:
            candidate = current_script
        if agent_id == "superwing":
            candidate = bot_scripts.script_from_superwing_rules(candidate, sw_rules)
        normalized = bot_scripts.normalize_bot_script(candidate, agent_id)
        if normalized.get("status") == "script_invalid":
            validation_errors.append(f"{agent_id} bot script rejected: {normalized.get('risk_reason', 'script_invalid')}")
        bot_script_candidates[agent_id] = normalized
        proposal_path = strategy_rules.write_proposal(
            data_dir,
            review_id,
            agent_id,
            "_bot_script.json",
            json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        proposed[f"{agent_id}_bot_script"] = str(proposal_path)

    promote = (not model_failed) and (not model_fallback_used) and (not validation_errors) and auto_promote_enabled(args)
    if promote:
        promoted["superwing_rules"] = str(strategy_rules.promote_superwing_rules(data_dir, sw_rules, source=f"review:{review_id}", rationale=str(review.get("superwing_rationale", ""))))
        promoted["deepseek_rules"] = str(strategy_rules.promote_deepseek_rules(data_dir, ds_rules_text, source=f"review:{review_id}", rationale=str(review.get("deepseek_rationale", ""))))
        for agent_id, candidate in bot_script_candidates.items():
            promoted[f"{agent_id}_bot_script"] = str(bot_scripts.write_bot_script(data_dir, agent_id, candidate, source=f"review:{review_id}"))

    if model_failed or model_fallback_used:
        review_status = "fallback_no_promote"
    elif validation_errors:
        review_status = "validation_no_promote"
    else:
        review_status = "model_ok"
    usage_summary = merge_model_usage(agent_reviews)
    agent_review_paths = write_agent_review_records(data_dir, review_id, agent_reviews, model_errors, review_status, validation_errors)
    record = {
        "ok": True,
        "review_id": review_id,
        "ts": duel.utc_now(),
        "review_model": review.get("review_model"),
        "review_status": review_status,
        "model_fallback_used": model_fallback_used,
        "review_model_usage": usage_summary,
        "per_agent_review_paths": agent_review_paths,
        "review_model_error": model_error if model_failed else "",
        "validation_errors": validation_errors,
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
    operator_dashboard_dir = args.operator_dashboard_dir or os.environ.get("AURUM_OPERATOR_DASHBOARD_DIR", "")
    try:
        dash_args = argparse.Namespace(
            data_dir=str(data_dir),
            env_file=str(env_file or ""),
            output_dir=dashboard_dir,
            operator_output_dir=operator_dashboard_dir,
        )
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
    p.add_argument("--operator-dashboard-dir", default="")
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
