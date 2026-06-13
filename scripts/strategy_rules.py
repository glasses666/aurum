#!/usr/bin/env python3
"""Versioned paper-strategy rules for the Aurum duel.

These files are intentionally local data, not secrets. They let the dashboard show
what each lane is currently allowed to do, and let a review job propose/update
paper-only strategy prompts without touching wallet keys or live orders.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import shutil
from typing import Any, Dict, Optional

DEFAULT_SUPERWING_RULES: Dict[str, Any] = {
    "version": 1,
    "agent_id": "superwing",
    "name": "Conservative underdog probe",
    "selection": "Among the shared market slate, look for liquid cheaper outcomes in a bounded probability band. Hold if no clean setup exists.",
    "price_min": 0.25,
    "price_max": 0.48,
    "max_notional": 30.0,
    "limit_buffer": 0.015,
    "confidence": 0.51,
    "min_volume": 0.0,
    "notes": "Deterministic baseline; paper-only; buy-only; small capped risk.",
    "updated_at": "",
    "updated_by": "default",
}

DEFAULT_DEEPSEEK_RULES = """# DeepSeek paper strategy rules

- Scope: paper-only Polymarket research; never ask for wallets, private keys, USDC deposits, logins, geoblock bypass, or live orders.
- Side: buy-only paper orders. If the edge is unclear, hold.
- Prefer high-liquidity markets with a clear thesis, near-term resolution, and prices that leave room for mispricing.
- Avoid forced trades. A no-order decision is valid when the slate is noisy or the available market prices already look efficient.
- Keep each rationale one concise sentence: what edge you think exists and why the quoted limit is acceptable.
- Respect runner-enforced caps: max order count, max notional per order, reserve cash, total risk, and per-market exposure.
"""

FORBIDDEN_RULE_TERMS = re.compile(
    r"(?i)(private\s*key|wallet\s*seed|seed\s*phrase|real\s*order|live\s*trading|"
    r"deposit\s*usdc|send\s*usdc|geoblock\s*bypass|bypass\s*geo|clob\s*secret|"
    r"api\s*secret|signature\s*key)"
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def rules_root(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "strategy_rules"


def current_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return rules_root(data_dir) / "current"


def proposal_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return rules_root(data_dir) / "proposals"


def history_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return rules_root(data_dir) / "history"


def superwing_rules_path(data_dir: pathlib.Path) -> pathlib.Path:
    return current_dir(data_dir) / "superwing_rules.json"


def deepseek_rules_path(data_dir: pathlib.Path) -> pathlib.Path:
    return current_dir(data_dir) / "deepseek_rules.md"


def version_log_path(data_dir: pathlib.Path) -> pathlib.Path:
    return rules_root(data_dir) / "versions.jsonl"


def append_version(data_dir: pathlib.Path, record: Dict[str, Any]) -> None:
    path = version_log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": utc_now(), **record}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def ensure_default_rules(data_dir: pathlib.Path) -> None:
    current_dir(data_dir).mkdir(parents=True, exist_ok=True)
    proposal_dir(data_dir).mkdir(parents=True, exist_ok=True)
    history_dir(data_dir).mkdir(parents=True, exist_ok=True)
    sw_path = superwing_rules_path(data_dir)
    ds_path = deepseek_rules_path(data_dir)
    if not sw_path.exists():
        rules = dict(DEFAULT_SUPERWING_RULES)
        rules["updated_at"] = utc_now()
        sw_path.write_text(json.dumps(rules, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        append_version(data_dir, {"agent_id": "superwing", "action": "init", "path": str(sw_path)})
    if not ds_path.exists():
        ds_path.write_text(DEFAULT_DEEPSEEK_RULES, encoding="utf-8")
        append_version(data_dir, {"agent_id": "deepseek", "action": "init", "path": str(ds_path)})


def load_superwing_rules(data_dir: pathlib.Path) -> Dict[str, Any]:
    ensure_default_rules(data_dir)
    try:
        raw = json.loads(superwing_rules_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return normalize_superwing_rules(raw)
    except Exception:
        pass
    return dict(DEFAULT_SUPERWING_RULES)


def load_deepseek_rules(data_dir: pathlib.Path) -> str:
    ensure_default_rules(data_dir)
    return deepseek_rules_path(data_dir).read_text(encoding="utf-8")


def normalize_superwing_rules(raw: Dict[str, Any]) -> Dict[str, Any]:
    rules = dict(DEFAULT_SUPERWING_RULES)
    rules.update({k: v for k, v in raw.items() if k in rules or k in {"review_rationale"}})

    def f(name: str, default: float, low: float, high: float) -> float:
        try:
            value = float(rules.get(name, default))
        except Exception:
            value = default
        return round(max(low, min(high, value)), 4)

    rules["price_min"] = f("price_min", DEFAULT_SUPERWING_RULES["price_min"], 0.05, 0.85)
    rules["price_max"] = f("price_max", DEFAULT_SUPERWING_RULES["price_max"], rules["price_min"] + 0.01, 0.95)
    rules["max_notional"] = f("max_notional", DEFAULT_SUPERWING_RULES["max_notional"], 1.0, 45.0)
    rules["limit_buffer"] = f("limit_buffer", DEFAULT_SUPERWING_RULES["limit_buffer"], 0.001, 0.035)
    rules["confidence"] = f("confidence", DEFAULT_SUPERWING_RULES["confidence"], 0.45, 0.8)
    rules["min_volume"] = f("min_volume", DEFAULT_SUPERWING_RULES["min_volume"], 0.0, 2_000_000.0)
    rules["agent_id"] = "superwing"
    rules["updated_at"] = str(rules.get("updated_at") or utc_now())[:64]
    return rules


def validate_deepseek_rules(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("deepseek rules are empty")
    if len(text) > 5000:
        raise ValueError("deepseek rules exceed 5000 characters")
    if FORBIDDEN_RULE_TERMS.search(text):
        raise ValueError("deepseek rules contain live-trading/secret-like forbidden terms")
    lower = text.lower()
    required = ["paper", "buy", "hold"]
    missing = [word for word in required if word not in lower]
    if missing:
        text += "\n\nSafety footer: This is paper-only, buy-only, and holding is required when no clear edge exists.\n"
    return text + ("\n" if not text.endswith("\n") else "")


def write_proposal(data_dir: pathlib.Path, tick_id: str, agent_id: str, suffix: str, content: str) -> pathlib.Path:
    proposal_dir(data_dir).mkdir(parents=True, exist_ok=True)
    safe_tick = re.sub(r"[^A-Za-z0-9_.-]", "_", tick_id)
    path = proposal_dir(data_dir) / f"{safe_tick}_{agent_id}{suffix}"
    path.write_text(content, encoding="utf-8")
    return path


def promote_superwing_rules(data_dir: pathlib.Path, proposed: Dict[str, Any], source: str, rationale: str = "") -> pathlib.Path:
    ensure_default_rules(data_dir)
    proposed = normalize_superwing_rules({**proposed, "updated_at": utc_now(), "updated_by": source, "review_rationale": rationale[:1000]})
    dest = superwing_rules_path(data_dir)
    hist = history_dir(data_dir) / f"{utc_now().replace(':', '').replace('+00:00', 'Z')}_superwing_rules.json"
    hist.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, hist)
    dest.write_text(json.dumps(proposed, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_version(data_dir, {"agent_id": "superwing", "action": "promote", "source": source, "path": str(dest), "backup": str(hist)})
    return dest


def promote_deepseek_rules(data_dir: pathlib.Path, proposed_text: str, source: str, rationale: str = "") -> pathlib.Path:
    ensure_default_rules(data_dir)
    proposed_text = validate_deepseek_rules(proposed_text)
    header = f"<!-- updated_at={utc_now()} updated_by={source} rationale={rationale[:300].replace('--', '—')} -->\n"
    dest = deepseek_rules_path(data_dir)
    hist = history_dir(data_dir) / f"{utc_now().replace(':', '').replace('+00:00', 'Z')}_deepseek_rules.md"
    hist.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, hist)
    dest.write_text(header + proposed_text, encoding="utf-8")
    append_version(data_dir, {"agent_id": "deepseek", "action": "promote", "source": source, "path": str(dest), "backup": str(hist)})
    return dest


def summarize_rules(data_dir: pathlib.Path) -> Dict[str, Any]:
    ensure_default_rules(data_dir)
    sw = load_superwing_rules(data_dir)
    ds_text = load_deepseek_rules(data_dir)
    versions = []
    vpath = version_log_path(data_dir)
    if vpath.exists():
        try:
            lines = vpath.read_text(encoding="utf-8").splitlines()[-12:]
            versions = [json.loads(line) for line in lines if line.strip()]
        except Exception:
            versions = []
    return {
        "superwing": sw,
        "deepseek_rules_excerpt": ds_text[:1800],
        "deepseek_rules_length": len(ds_text),
        "versions": versions,
    }
