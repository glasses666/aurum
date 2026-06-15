#!/usr/bin/env python3
"""Emit a redacted Aurum runtime status report.

The report is intentionally artifact-based and read-only. It distinguishes code
that is locally complete from runtime proof that is actually healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
from typing import Any, Dict, List, Optional

import agent_duel as duel
import data_quality_gate
import generate_dashboard


def latest_tick(data_dir: pathlib.Path) -> Dict[str, Any]:
    ticks = generate_dashboard.read_jsonl(data_dir / "ticks.jsonl", limit=1)
    return ticks[-1] if ticks else {}


def gate_status(data_dir: pathlib.Path, recorder_dir: pathlib.Path, max_stale_seconds: int) -> Dict[str, Any]:
    return generate_dashboard.redact_value(
        data_quality_gate.evaluate_data_quality_gate(
            recorder_dir,
            max_stale_seconds=max_stale_seconds,
            allow_unaudited_fallback=False,
        )
    )


def public_scoreboard_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
    public_rows: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            public_rows.append({key: row.get(key) for key in allowed if key in row})
    return public_rows


def build_report(data_dir: pathlib.Path, recorder_dir: pathlib.Path, max_stale_seconds: int) -> Dict[str, Any]:
    data_dir = pathlib.Path(data_dir)
    recorder_dir = pathlib.Path(recorder_dir)
    tick = latest_tick(data_dir)
    gate = gate_status(data_dir, recorder_dir, max_stale_seconds)
    recorder = generate_dashboard.public_recorder_summary(data_dir, recorder_dir=recorder_dir, max_stale_seconds=max_stale_seconds)
    registry = generate_dashboard.public_bot_registry_summary(data_dir)
    backup = generate_dashboard.backup_status(data_dir, recorder_dir=recorder_dir)
    replay = generate_dashboard.replay_status(data_dir, recorder_dir=recorder_dir)
    ledger = generate_dashboard.risk_ledger_status(data_dir)
    try:
        state = duel.load_state(data_dir)
        prices = duel.market_price_map(state.get("last_markets", []))
        scoreboard = duel.scoreboard_context(state, prices)
    except Exception:
        scoreboard = {"competition": duel.competition_context(), "scoreboard": [], "victory": duel.victory_status([])}
    runtime_complete = generate_dashboard.runtime_is_complete(gate, recorder, registry, backup, replay, ledger)
    report = {
        "ok": True,
        "report": "aurum_status_report_v1",
        "completion_state": "runtime-complete" if runtime_complete else "code-complete-only",
        "latest_tick_id": tick.get("tick_id"),
        "data_quality_gate": gate,
        "recorder": recorder,
        "manifest": recorder.get("manifest", {}),
        "book_coverage": recorder.get("book_coverage", {}),
        "orderable_market_count": recorder.get("orderable_market_count"),
        "bot_registry": registry,
        "competition": scoreboard.get("competition", {}),
        "scoreboard": public_scoreboard_rows(scoreboard.get("scoreboard", [])),
        "victory": scoreboard.get("victory", duel.victory_status([])),
        "backup": {k: v for k, v in backup.items() if k != "redacted"},
        "replay": replay,
        "risk_ledger": ledger,
    }
    return generate_dashboard.redact_value(report)


def write_report(path: pathlib.Path, report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit a redacted Aurum runtime status report")
    parser.add_argument("--data-dir", default="data/paper_duel")
    parser.add_argument("--recorder-data-dir", default="")
    parser.add_argument("--max-stale-seconds", type=int, default=int(os.environ.get("AURUM_RECORDER_MAX_STALE_SECONDS", "180")))
    parser.add_argument("--output", default="")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = pathlib.Path(args.data_dir)
    recorder_dir = pathlib.Path(args.recorder_data_dir) if args.recorder_data_dir else generate_dashboard.recorder_data_root(data_dir)
    report = build_report(data_dir, recorder_dir, args.max_stale_seconds)
    if args.output:
        write_report(pathlib.Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
