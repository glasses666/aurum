#!/usr/bin/env python3
"""Replay and recorded-book execution helpers for Aurum paper trading.

The helpers in this module are read-only against recorder data. They build a
small execution context that the paper engine can use to prove which recorder
capture, manifest row, orderable feed, and CLOB book depth informed a tick.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
from typing import Any, Dict, Iterable, List, Optional, Tuple

import agent_duel as duel
import bot_scripts
import market_recorder


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def hash_json(value: Any) -> str:
    return market_recorder.sha256_text(market_recorder.canonical_json(value))


def hash_file(path: pathlib.Path) -> str:
    return market_recorder.sha256_text(path.read_text(encoding="utf-8", errors="replace"))


def manifest_path_for_ts(data_dir: pathlib.Path, ts: str) -> pathlib.Path:
    return market_recorder.raw_day_dir(data_dir, ts) / "manifest.jsonl"


def manifest_rows_for_ts(data_dir: pathlib.Path, ts: str, *, max_lines: Optional[int] = None) -> List[Dict[str, Any]]:
    manifest_path = manifest_path_for_ts(data_dir, ts)
    rows: List[Dict[str, Any]] = []
    if not manifest_path.exists():
        return rows
    iter_tail_text_lines = getattr(market_recorder, "iter_tail_text_lines")
    manifest_sequence = getattr(market_recorder, "manifest_sequence")
    for line in iter_tail_text_lines(manifest_path, max_lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("ts") or "") == str(ts):
            rows.append(row)
    rows.sort(key=lambda row: manifest_sequence(row.get("sequence")) or 0)
    return rows


def manifest_row_error(row: Dict[str, Any], day: str) -> Optional[str]:
    manifest_hash = str(row.get("manifest_sha256") or "")
    unsigned = {k: v for k, v in row.items() if k != "manifest_sha256"}
    if manifest_hash != market_recorder.sha256_text(market_recorder.canonical_json(unsigned)):
        return "manifest_hash_mismatch"
    source = str(row.get("source") or "")
    if source not in market_recorder.RAW_SOURCE_STEMS:
        return "manifest_source_unexpected"
    rel = pathlib.PurePosixPath(str(row.get("path") or ""))
    expected = pathlib.PurePosixPath("raw") / "polymarket" / day / f"{market_recorder.RAW_SOURCE_STEMS[source]}.jsonl"
    if rel.is_absolute() or ".." in rel.parts:
        return "manifest_path_unsafe"
    if rel != expected:
        return "manifest_path_unexpected"
    return None


def frame_for_manifest_row(
    data_dir: pathlib.Path,
    row: Dict[str, Any],
    *,
    max_frame_lines: Optional[int] = None,
    parse_frame: bool = True,
) -> Optional[Dict[str, Any]]:
    rel = pathlib.PurePosixPath(str(row.get("path") or ""))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    path = data_dir / pathlib.Path(*rel.parts)
    if not path.exists():
        return None
    expected_line_sha = str(row.get("line_sha256") or "")
    iter_tail_text_lines = getattr(market_recorder, "iter_tail_text_lines")
    for line_number, line in enumerate(iter_tail_text_lines(path, max_frame_lines), 1):
        if not line.strip():
            continue
        if market_recorder.sha256_text(line) != expected_line_sha:
            continue
        frame: Optional[Dict[str, Any]] = None
        if parse_frame:
            try:
                parsed = json.loads(line)
            except Exception:
                return None
            if not isinstance(parsed, dict):
                return None
            if str(parsed.get("payload_sha256") or "") != str(row.get("payload_sha256") or ""):
                return None
            frame = parsed
        return {
            "line_number": line_number,
            "frame": frame,
            "frame_path": str(path.relative_to(data_dir)),
            "line_sha256": expected_line_sha,
            "payload_sha256": row.get("payload_sha256"),
            "manifest_sha256": row.get("manifest_sha256"),
            "sequence": row.get("sequence"),
            "source": row.get("source"),
        }
    return None


def latest_tail_manifest_proof(root: pathlib.Path, capture_ts: str) -> Dict[str, Any]:
    health = read_json(root / "reports" / "market_recorder_health.json")
    if str(health.get("ts") or "") != str(capture_ts):
        raise RuntimeError("recorder health does not match requested capture")
    manifest = health.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("ok") is not True:
        raise RuntimeError("recorder health manifest is not ok")
    if manifest.get("verification_scope") != "tail":
        raise RuntimeError("recorder health manifest is not tail verified")
    if not market_recorder.strict_positive_int(manifest.get("verified_rows")):
        raise RuntimeError("recorder health manifest has no verified rows")
    return manifest


def verify_manifest_tail_chain(
    root: pathlib.Path,
    capture_ts: str,
    proof: Dict[str, Any],
    *,
    max_rows: Optional[int],
) -> Dict[str, Any]:
    if max_rows is None:
        raise ValueError("tail chain verification requires max_rows")
    day = market_recorder.parse_ts(capture_ts).date().isoformat()
    manifest_path = manifest_path_for_ts(root, capture_ts)
    manifest_lines = market_recorder.tail_text_lines(manifest_path, max_rows + 1)
    if not manifest_lines:
        raise RuntimeError("missing recorder manifest tail")
    prev_hash = ""
    expected_sequence = 1
    if len(manifest_lines) > max_rows:
        boundary_line = manifest_lines[0]
        manifest_lines = manifest_lines[-max_rows:]
        try:
            boundary = json.loads(boundary_line)
        except Exception as exc:
            raise RuntimeError("manifest boundary json error") from exc
        boundary_error = manifest_row_error(boundary, day)
        if boundary_error:
            raise RuntimeError("manifest boundary " + boundary_error)
        boundary_sequence = market_recorder.manifest_sequence(boundary.get("sequence"))
        if boundary_sequence is None:
            raise RuntimeError("manifest boundary sequence invalid")
        prev_hash = str(boundary.get("manifest_sha256") or "")
        expected_sequence = boundary_sequence + 1

    verified_rows = 0
    latest_sequence: Optional[int] = None
    last_hash = ""
    for line in manifest_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception as exc:
            raise RuntimeError("manifest tail json error") from exc
        row_error = manifest_row_error(row, day)
        if row_error:
            raise RuntimeError(row_error)
        sequence = market_recorder.manifest_sequence(row.get("sequence"))
        if sequence != expected_sequence:
            raise RuntimeError("manifest_sequence_error")
        if str(row.get("prev_manifest_sha256") or "") != prev_hash:
            raise RuntimeError("manifest_prev_hash_error")
        last_hash = str(row.get("manifest_sha256") or "")
        prev_hash = last_hash
        latest_sequence = sequence
        expected_sequence += 1
        verified_rows += 1

    proof_sequence = market_recorder.strict_positive_int(proof.get("latest_sequence"))
    if proof_sequence is not None and latest_sequence != proof_sequence:
        raise RuntimeError("manifest_latest_sequence_mismatch")
    proof_hash = str(proof.get("last_manifest_sha256") or "")
    if proof_hash and last_hash != proof_hash:
        raise RuntimeError("manifest_latest_hash_mismatch")
    return {
        "ok": True,
        "verification_scope": "tail",
        "max_rows": max_rows,
        "verified_rows": verified_rows,
        "latest_sequence": latest_sequence,
        "last_manifest_sha256": last_hash,
    }


def _public_frame_ref(frame_ref: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": frame_ref.get("source"),
        "sequence": frame_ref.get("sequence"),
        "line_sha256": frame_ref.get("line_sha256"),
        "payload_sha256": frame_ref.get("payload_sha256"),
        "manifest_sha256": frame_ref.get("manifest_sha256"),
    }


def build_recorder_context(
    data_dir: pathlib.Path | str,
    *,
    ts: Optional[str] = None,
    verify_scope: str = "tail",
    max_rows: Optional[int] = 500,
) -> Dict[str, Any]:
    """Return recorder execution context for the latest capture or a capture ts."""

    root = pathlib.Path(data_dir)
    latest = read_json(root / "normalized" / "polymarket" / "latest_markets.json")
    latest_ts = str(latest.get("ts") or "")
    capture_ts = str(ts or latest_ts)
    if verify_scope not in {"tail", "full"}:
        raise ValueError("verify_scope must be tail or full")
    use_latest_tail_proof = verify_scope == "tail" and capture_ts == latest_ts
    if use_latest_tail_proof:
        verified = latest_tail_manifest_proof(root, capture_ts)
        tail_chain = verify_manifest_tail_chain(root, capture_ts, verified, max_rows=max_rows)
        verified = {**verified, **tail_chain}
    else:
        verified = market_recorder.verify_manifest(root, ts=capture_ts, max_rows=None if verify_scope == "full" else max_rows)
    if not verified.get("ok"):
        raise RuntimeError("recorder manifest verification failed: " + ",".join(str(e) for e in verified.get("errors", [])))

    orderable_path = root / "features" / "polymarket_orderable_feed.json"
    orderable_feed = read_json(orderable_path)
    orderable_feed_sha256 = hash_file(orderable_path)
    use_bounded_manifest_lookup = verify_scope == "tail" and capture_ts == latest_ts
    max_manifest_lines = max(50, int(max_rows or 500)) if use_bounded_manifest_lookup else None
    manifest_rows = manifest_rows_for_ts(root, capture_ts, max_lines=max_manifest_lines)
    if not manifest_rows and use_bounded_manifest_lookup:
        manifest_rows = manifest_rows_for_ts(root, capture_ts, max_lines=None)
    if not manifest_rows:
        raise RuntimeError("no manifest rows for recorder capture")

    source_refs: Dict[str, List[Dict[str, Any]]] = {}
    books_by_token: Dict[str, Any] = {}
    book_refs_by_token: Dict[str, Dict[str, Any]] = {}
    max_frame_lines = 2000 if verify_scope == "tail" else None
    capture_day = market_recorder.parse_ts(capture_ts).date().isoformat()
    for row in manifest_rows:
        source = str(row.get("source") or "")
        row_error = manifest_row_error(row, capture_day)
        if row_error:
            raise RuntimeError(row_error)
        frame_ref = frame_for_manifest_row(root, row, max_frame_lines=max_frame_lines, parse_frame=True)
        if not frame_ref:
            raise RuntimeError("missing recorder raw frame for manifest row")
        source_refs.setdefault(source, []).append(_public_frame_ref(frame_ref))
        if source == "clob_book":
            frame = frame_ref.get("frame") or {}
            payload = frame.get("payload") if isinstance(frame, dict) else {}
            if isinstance(payload, dict):
                token_id = str(payload.get("token_id") or "")
                if token_id:
                    books_by_token[token_id] = payload.get("book")
                    book_refs_by_token[token_id] = _public_frame_ref(frame_ref)

    latest_manifest_hash = str((manifest_rows[-1] or {}).get("manifest_sha256") or verified.get("last_manifest_sha256") or "")
    capture_id = hash_json({"ts": capture_ts, "latest_manifest_sha256": latest_manifest_hash, "orderable_feed_sha256": orderable_feed_sha256})
    return {
        "source": "polymarket_market_recorder_v0",
        "capture_id": capture_id,
        "ts": capture_ts,
        "manifest": {
            "ok": verified.get("ok"),
            "verification_scope": verified.get("verification_scope"),
            "max_rows": verified.get("max_rows"),
            "verified_rows": verified.get("verified_rows"),
            "latest_sequence": verified.get("latest_sequence"),
            "last_manifest_sha256": verified.get("last_manifest_sha256"),
        },
        "orderable_feed_sha256": orderable_feed_sha256,
        "orderable_market_count": latest.get("orderable_market_count"),
        "book_coverage": latest.get("book_coverage"),
        "source_refs": source_refs,
        "books_by_token": books_by_token,
        "book_refs_by_token": book_refs_by_token,
        "orderable_feed": orderable_feed,
    }


def public_context(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return {
        "source": context.get("source"),
        "capture_id": context.get("capture_id"),
        "ts": context.get("ts"),
        "manifest": context.get("manifest"),
        "orderable_feed_sha256": context.get("orderable_feed_sha256"),
        "book_coverage": context.get("book_coverage"),
        "orderable_market_count": context.get("orderable_market_count"),
        "source_refs": context.get("source_refs"),
    }


def _levels(book: Any, side: str) -> List[Tuple[float, float]]:
    return market_recorder._book_levels(book, side)  # recorder already normalizes common CLOB shapes.


def outcome_token_id(market: Dict[str, Any], outcome_name: str) -> str:
    for outcome in market.get("outcomes", []) or []:
        if str(outcome.get("name") or "").lower() == str(outcome_name or "").lower():
            return str(outcome.get("token_id") or "")
    return ""


def book_walk_fill(
    *,
    market: Dict[str, Any],
    outcome_name: str,
    side: str,
    limit_price: float,
    notional: float = 0.0,
    shares: float = 0.0,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    token_id = outcome_token_id(market, outcome_name)
    if not token_id:
        return {"ok": False, "reason": "missing_outcome_token_id"}
    book = (context.get("books_by_token") or {}).get(token_id)
    book_ref = (context.get("book_refs_by_token") or {}).get(token_id) or {}
    if not book:
        return {"ok": False, "reason": "missing_recorded_orderbook", "token_id": token_id}

    if side == "buy":
        remaining_notional = float(notional)
        if remaining_notional <= 0:
            return {"ok": False, "reason": "buy_notional_required", "token_id": token_id}
        levels = sorted(_levels(book, "asks"), key=lambda row: row[0])
        used: List[Dict[str, float]] = []
        filled_shares = 0.0
        spent = 0.0
        for price, size in levels:
            if price > limit_price:
                break
            level_notional = price * size
            take_notional = min(remaining_notional, level_notional)
            if take_notional <= 0:
                continue
            take_shares = take_notional / price
            used.append({"price": round(price, 6), "shares": round(take_shares, 6)})
            filled_shares += take_shares
            spent += take_notional
            remaining_notional -= take_notional
            if remaining_notional <= 1e-9:
                break
        if remaining_notional > 1e-6 or filled_shares <= 0:
            return {"ok": False, "reason": "insufficient_recorded_ask_depth", "token_id": token_id, "limit_price": limit_price}
        return {
            "ok": True,
            "source": "recorded_orderbook_depth",
            "token_id": token_id,
            "side": "buy",
            "fill_price": spent / filled_shares,
            "shares": filled_shares,
            "notional": spent,
            "levels": used,
            "book_ref": book_ref,
        }

    if side == "sell":
        requested_shares = float(shares)
        if requested_shares <= 0:
            return {"ok": False, "reason": "sell_shares_required", "token_id": token_id}
        levels = sorted(_levels(book, "bids"), key=lambda row: row[0], reverse=True)
        used = []
        filled_shares = 0.0
        proceeds = 0.0
        remaining_shares = requested_shares
        for price, size in levels:
            if price < limit_price:
                break
            take_shares = min(remaining_shares, size)
            if take_shares <= 0:
                continue
            used.append({"price": round(price, 6), "shares": round(take_shares, 6)})
            filled_shares += take_shares
            proceeds += take_shares * price
            remaining_shares -= take_shares
            if remaining_shares <= 1e-9:
                break
        if remaining_shares > 1e-6 or filled_shares <= 0:
            return {"ok": False, "reason": "insufficient_recorded_bid_depth", "token_id": token_id, "limit_price": limit_price}
        return {
            "ok": True,
            "source": "recorded_orderbook_depth",
            "token_id": token_id,
            "side": "sell",
            "fill_price": proceeds / filled_shares,
            "shares": filled_shares,
            "notional": proceeds,
            "levels": used,
            "book_ref": book_ref,
        }

    return {"ok": False, "reason": "unsupported_side", "token_id": token_id}


def risk_ledger_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "risk_ledger.jsonl"


def append_risk_ledger(data_dir: pathlib.Path, record: Dict[str, Any]) -> None:
    path = risk_ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _copy_bot_scripts(src_paper_dir: pathlib.Path, dst_paper_dir: pathlib.Path) -> None:
    src = bot_scripts.scripts_root(src_paper_dir)
    dst = bot_scripts.scripts_root(dst_paper_dir)
    if src.exists():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        bot_scripts.ensure_default_bot_scripts(dst_paper_dir)


def replay_session(
    *,
    recorder_data_dir: pathlib.Path,
    paper_data_dir: pathlib.Path,
    output_dir: pathlib.Path,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    max_frames: int = 1,
    verify_scope: str = "full",
    max_rows: Optional[int] = None,
    max_orders: int = 2,
    limit: int = 12,
    min_volume: float = 0.0,
) -> Dict[str, Any]:
    if max_frames != 1:
        raise RuntimeError("current replay checkpoint supports exactly one recorder capture frame")
    context = build_recorder_context(
        recorder_data_dir,
        ts=start_ts,
        verify_scope=verify_scope,
        max_rows=max_rows,
    )
    if end_ts and str(context["ts"]) > str(end_ts):
        raise RuntimeError("selected recorder capture is after replay end-ts")
    latest = market_recorder.load_latest_markets(recorder_data_dir, max_stale_seconds=999999999)
    markets = [m for m in latest.get("markets", []) if float(m.get("volume") or 0.0) >= float(min_volume)]
    if limit > 0:
        markets = markets[:limit]
    if not markets:
        raise RuntimeError("no replay markets after filters")

    replay_paper = output_dir / "paper_duel"
    if replay_paper.exists():
        shutil.rmtree(replay_paper)
    _copy_bot_scripts(paper_data_dir, replay_paper)
    registry = bot_scripts.verify_bot_registry_manifest(replay_paper)
    if not registry.get("ok"):
        raise RuntimeError("bot registry verification failed: " + ",".join(str(e) for e in registry.get("errors", [])))

    fixed_now = lambda: str(context["ts"])
    state = duel.init_state(replay_paper, reset=True, now_fn=fixed_now)
    agents: Dict[str, Any] = {}
    for agent_id in duel.AGENTS:
        state = duel.load_state(replay_paper)
        script = bot_scripts.load_bot_script(replay_paper, agent_id, write_manifest=False)
        decision = bot_scripts.mechanical_decision_for_agent(state["accounts"][agent_id], markets, script)
        result = duel.validate_and_apply(
            replay_paper,
            state,
            agent_id,
            decision,
            markets,
            apply=True,
            max_orders=int(script.get("max_orders_per_tick", max_orders)),
            max_notional_per_order=duel.STARTING_EQUITY * duel.MAX_TRADE_FRACTION,
            execution_context={
                "recorder": context,
                "recorder_public": public_context(context),
                "bot_script_hash": bot_scripts.sha256_json(script),
                "bot_script_status": script.get("status"),
                "source": "recorder_replay",
            },
            now_fn=fixed_now,
        )
        agents[agent_id] = {"script_hash": bot_scripts.sha256_json(script), "decision": decision, "result": result}

    final_state = duel.load_state(replay_paper)
    prices = duel.market_price_map(markets)
    scores = [duel.portfolio_value(final_state["accounts"][agent], prices) for agent in duel.AGENTS]
    scores.sort(key=lambda row: row["score"], reverse=True)
    ledger_rows = []
    ledger_path = risk_ledger_path(replay_paper)
    if ledger_path.exists():
        ledger_rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = {
        "ok": True,
        "mode": "recorder_replay",
        "recorder_context": public_context(context),
        "bot_registry_ok": registry.get("ok"),
        "market_count": len(markets),
        "agents": agents,
        "scores": scores,
        "state_path": str(replay_paper / "state.json"),
        "risk_ledger_path": str(ledger_path),
        "risk_ledger_rows": len(ledger_rows),
        "account_state_hash": hash_json(final_state.get("accounts", {})),
        "risk_ledger_hash": hash_json(ledger_rows),
    }
    write_json(output_dir / "replay_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Replay Aurum paper session from verified recorder data")
    p.add_argument("--recorder-data-dir", required=True)
    p.add_argument("--paper-data-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--start-ts", default="")
    p.add_argument("--end-ts", default="")
    p.add_argument("--max-frames", type=int, default=1)
    p.add_argument("--verify-scope", choices=("full", "tail"), default="full")
    p.add_argument("--max-rows", type=int, default=0)
    p.add_argument("--max-orders", type=int, default=2)
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--min-volume", type=float, default=0.0)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    summary = replay_session(
        recorder_data_dir=pathlib.Path(args.recorder_data_dir),
        paper_data_dir=pathlib.Path(args.paper_data_dir),
        output_dir=pathlib.Path(args.output_dir),
        start_ts=args.start_ts or None,
        end_ts=args.end_ts or None,
        max_frames=args.max_frames,
        verify_scope=args.verify_scope,
        max_rows=args.max_rows or None,
        max_orders=args.max_orders,
        limit=args.limit,
        min_volume=args.min_volume,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
