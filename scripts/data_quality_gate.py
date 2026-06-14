#!/usr/bin/env python3
"""Fail-closed market data quality gate for Aurum paper bots.

This module is deliberately pure/read-only: it inspects recorder artifacts and
returns an explicit gate decision for the hot path. Trading may proceed only on
TRADE_ALLOWED. Missing, stale, unhealthy, or incomplete recorder data stays
HOLD_ONLY; unreadable or structurally invalid core artifacts STOP_SERVICE so
operators see hard breaks instead of silently fetching an unaudited fallback feed.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
from typing import Any, Callable, Dict, List, Optional, Tuple

TRADE_ALLOWED = "TRADE_ALLOWED"
HOLD_ONLY = "HOLD_ONLY"
STOP_SERVICE = "STOP_SERVICE"

REQUIRED_SOURCES = ("gamma_markets", "clob_markets", "data_trades", "clob_book")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def age_seconds(ts_value: Any, now: Callable[[], str]) -> Optional[float]:
    ts = parse_ts(ts_value)
    current = parse_ts(now())
    if not ts or not current:
        return None
    return (current - ts).total_seconds()


def _read_json(path: pathlib.Path) -> Tuple[Optional[Any], Optional[str]]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, "parse:" + type(exc).__name__


def _append_health_reasons(health: Any, reasons: List[str], stop_reasons: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(health, dict):
        stop_reasons.append("invalid_health_report")
        return None
    ok_value = health.get("ok")
    if ok_value is not True:
        if isinstance(ok_value, bool):
            reasons.append("last_capture_not_ok")
        else:
            reasons.append("health_ok_invalid")
    sources = health.get("sources", {}) if isinstance(health.get("sources"), dict) else {}
    for source in REQUIRED_SOURCES:
        detail = sources.get(source)
        if not isinstance(detail, dict):
            reasons.append(f"missing_source:{source}")
            continue
        try:
            ok_frames = _finite_integer(detail.get("ok_frames"))
            if ok_frames is None:
                raise ValueError("invalid frame count")
        except Exception:
            reasons.append(f"source_invalid:{source}")
            continue
        if ok_frames <= 0:
            reasons.append(f"source_not_ok:{source}")
        if source == "clob_book":
            requested = _finite_integer(detail.get("requested_tokens"))
            if requested is None:
                reasons.append("source_invalid:clob_book_requested_tokens")
            elif requested <= 0:
                reasons.append("book_coverage_empty")
            elif ok_frames < requested:
                reasons.append("book_coverage_incomplete")
    book_coverage = health.get("book_coverage") if isinstance(health.get("book_coverage"), dict) else None
    if not isinstance(book_coverage, dict):
        reasons.append("missing_book_coverage")
    else:
        requested = _finite_integer(book_coverage.get("requested_tokens"))
        ok_tokens = _finite_integer(book_coverage.get("ok_tokens"))
        orderable_tokens = _finite_integer(book_coverage.get("orderable_tokens"))
        if requested is None or ok_tokens is None or orderable_tokens is None:
            reasons.append("book_coverage_invalid")
        elif requested <= 0 or ok_tokens < requested or orderable_tokens <= 0:
            reasons.append("book_coverage_incomplete")
    orderable_count = _finite_integer(health.get("orderable_market_count"))
    if orderable_count is None:
        reasons.append("orderable_market_count_invalid")
    elif orderable_count <= 0:
        reasons.append("orderable_market_count_empty")
    manifest = health.get("manifest") if isinstance(health.get("manifest"), dict) else None
    if not isinstance(manifest, dict):
        reasons.append("missing_manifest_verification")
    elif manifest.get("ok") is not True:
        reasons.append("manifest_verification_failed")
    else:
        scope = manifest.get("verification_scope")
        verified_rows = _finite_integer(manifest.get("verified_rows"))
        latest_sequence = _finite_integer(manifest.get("latest_sequence"))
        if scope not in {"full", "tail"}:
            reasons.append("manifest_verification_scope_invalid")
        elif verified_rows is None or verified_rows <= 0:
            reasons.append("manifest_verification_empty")
        elif latest_sequence is None or latest_sequence <= 0:
            reasons.append("manifest_latest_sequence_invalid")
        elif scope == "tail":
            max_rows = _finite_integer(manifest.get("max_rows"))
            if max_rows is None or max_rows <= 0:
                reasons.append("manifest_tail_scope_invalid")
        elif scope == "full" and manifest.get("max_rows") is not None:
            reasons.append("manifest_full_scope_invalid")
    return health


def _finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _finite_integer(value: Any) -> Optional[int]:
    parsed = _finite_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _market_payload_is_complete(market: Dict[str, Any]) -> bool:
    market_id = market.get("market_id")
    if not isinstance(market_id, str) or not market_id.strip():
        return False
    if "volume" not in market:
        return False
    volume = _finite_float(market.get("volume"))
    if volume is None:
        return False
    outcomes = market.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) < 2:
        return False
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            return False
        outcome_name = outcome.get("name")
        if not isinstance(outcome_name, str) or not outcome_name.strip():
            return False
        price = _finite_float(outcome.get("price"))
        if price is None or not (0.0 < price < 1.0):
            return False
    return True


def _append_latest_reasons(latest: Any, reasons: List[str], stop_reasons: List[str]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    if not isinstance(latest, dict):
        stop_reasons.append("invalid_latest_markets")
        return None, []
    markets = latest.get("markets")
    if not isinstance(markets, list) or not markets:
        reasons.append("latest_markets_empty")
        return latest, []
    dict_markets = [market for market in markets if isinstance(market, dict)]
    usable_markets = [market for market in dict_markets if _market_payload_is_complete(market)]
    if len(usable_markets) != len(markets):
        reasons.append("market_payload_incomplete")
    if not usable_markets:
        reasons.append("latest_markets_empty")
    coverage = latest.get("book_coverage") if isinstance(latest.get("book_coverage"), dict) else None
    if not isinstance(coverage, dict):
        reasons.append("latest_missing_book_coverage")
    else:
        requested = _finite_integer(coverage.get("requested_tokens"))
        ok_tokens = _finite_integer(coverage.get("ok_tokens"))
        if requested is None or ok_tokens is None:
            reasons.append("latest_book_coverage_invalid")
        elif requested <= 0 or ok_tokens < requested:
            reasons.append("latest_book_coverage_incomplete")
    orderable_count = _finite_integer(latest.get("orderable_market_count"))
    if orderable_count is None:
        reasons.append("latest_orderable_market_count_invalid")
    elif orderable_count <= 0:
        reasons.append("latest_orderable_market_count_empty")
    return latest, usable_markets


def evaluate_data_quality_gate(
    data_dir: pathlib.Path | str,
    *,
    now: Callable[[], str] = utc_now,
    max_stale_seconds: int = 180,
    allow_unaudited_fallback: bool = False,
) -> Dict[str, Any]:
    """Return a fail-closed gate decision for recorder artifacts.

    Decision semantics:
    - TRADE_ALLOWED: health + latest markets are fresh, complete, and usable.
    - HOLD_ONLY: artifacts exist but are stale, degraded, incomplete, or empty.
    - STOP_SERVICE: required core artifacts are missing/unreadable/structurally invalid.
    """

    root = pathlib.Path(data_dir)
    health_path = root / "reports" / "market_recorder_health.json"
    latest_path = root / "normalized" / "polymarket" / "latest_markets.json"

    reason_codes: List[str] = []
    stop_reasons: List[str] = []

    health_raw, health_error = _read_json(health_path)
    if health_error == "missing":
        reason_codes.append("missing_health_report")
    elif health_error:
        stop_reasons.append("health_report_" + health_error)
    health = _append_health_reasons(health_raw, reason_codes, stop_reasons) if health_error is None else None

    latest_raw, latest_error = _read_json(latest_path)
    if latest_error == "missing":
        reason_codes.append("missing_latest_markets")
    elif latest_error:
        stop_reasons.append("latest_markets_" + latest_error)
    latest, markets = _append_latest_reasons(latest_raw, reason_codes, stop_reasons) if latest_error is None else (None, [])

    health_age = age_seconds(health.get("ts") if health else None, now)
    latest_age = age_seconds(latest.get("ts") if latest else None, now)
    observed_ages = [age for age in (health_age, latest_age) if age is not None]
    recorder_age = max(observed_ages) if observed_ages else None

    if health_age is None and health is not None:
        reason_codes.append("health_ts_invalid")
    if latest_age is None and latest is not None:
        reason_codes.append("latest_markets_ts_invalid")
    if any(age is not None and age < -5 for age in (health_age, latest_age)):
        reason_codes.append("recorder_ts_in_future")
    if any(age is not None and age > max_stale_seconds for age in (health_age, latest_age)):
        reason_codes.append("recorder_stale")
    if isinstance(health, dict) and isinstance(latest, dict):
        if str(health.get("ts") or "") != str(latest.get("ts") or ""):
            reason_codes.append("recorder_artifact_ts_mismatch")
        health_source_value = health.get("source") or health.get("recorder")
        latest_source_value = latest.get("source")
        if str(health_source_value or "") != str(latest_source_value or ""):
            reason_codes.append("recorder_artifact_source_mismatch")
        if health.get("book_coverage") != latest.get("book_coverage"):
            reason_codes.append("recorder_artifact_book_coverage_mismatch")
        if health.get("orderable_market_count") != latest.get("orderable_market_count"):
            reason_codes.append("recorder_artifact_orderable_count_mismatch")

    all_reasons = stop_reasons + reason_codes
    manifest_info: Dict[str, Any] = {}
    book_coverage_info: Dict[str, Any] = {}
    orderable_market_count: Optional[int] = None
    health_source: Optional[str] = None
    universe: Optional[str] = None
    if isinstance(health, dict):
        raw_manifest = health.get("manifest")
        if isinstance(raw_manifest, dict):
            manifest_info = raw_manifest
        raw_coverage = health.get("book_coverage")
        if isinstance(raw_coverage, dict):
            book_coverage_info = raw_coverage
        orderable_market_count = _finite_integer(health.get("orderable_market_count"))
        health_source = health.get("source") or health.get("recorder")
        universe = health.get("universe")
    if stop_reasons:
        decision = STOP_SERVICE
    elif reason_codes:
        decision = HOLD_ONLY
    else:
        decision = TRADE_ALLOWED

    return {
        "ok": decision == TRADE_ALLOWED,
        "decision": decision,
        "trade_allowed": decision == TRADE_ALLOWED,
        "fallback_allowed": bool(allow_unaudited_fallback and decision != STOP_SERVICE),
        "hold_only": decision == HOLD_ONLY,
        "stop_service": decision == STOP_SERVICE,
        "reason_codes": all_reasons,
        "data_dir": str(root),
        "health_path": str(health_path),
        "latest_markets_path": str(latest_path),
        "recorder_age_seconds": round(recorder_age, 1) if recorder_age is not None else None,
        "max_stale_seconds": int(max_stale_seconds),
        "market_source": latest.get("source") if latest else None,
        "market_count": len(markets),
        "health_source": health_source,
        "universe": universe,
        "book_coverage": book_coverage_info,
        "orderable_market_count": orderable_market_count,
        "manifest_verification_scope": manifest_info.get("verification_scope"),
        "manifest_verification_frames": manifest_info.get("frames"),
        "manifest_verification_max_rows": manifest_info.get("max_rows"),
        "manifest_verification_verified_rows": manifest_info.get("verified_rows"),
        "manifest_verification_latest_sequence": manifest_info.get("latest_sequence"),
    }


def hold_only_decision(reason_codes: List[str] | Tuple[str, ...], *, source: str = "data_quality_gate") -> Dict[str, Any]:
    return {
        "ok": False,
        "decision": HOLD_ONLY,
        "trade_allowed": False,
        "hold_only": True,
        "stop_service": False,
        "reason_codes": list(reason_codes),
        "market_source": source,
        "market_count": 0,
    }
