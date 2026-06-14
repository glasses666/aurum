import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_bot_loop
import agent_duel
import bot_scripts
import market_recorder
import recorder_replay


class ReplayFetcher:
    def __init__(self, *, ask_size: float = 100.0):
        self.ask_size = ask_size

    def __call__(self, url, timeout=12.0):
        if "gamma-api.polymarket.com" in url:
            return [
                {
                    "id": "btc",
                    "question": "Will Bitcoin close above 100k?",
                    "slug": "btc-100k",
                    "category": "Crypto",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.42", "0.58"]',
                    "clobTokenIds": '["tok_yes", "tok_no"]',
                    "volume": "5000",
                    "liquidity": "1000",
                }
            ]
        if "clob.polymarket.com/markets" in url:
            return {"markets": []}
        if "data-api.polymarket.com/trades" in url:
            return []
        if "clob.polymarket.com/book" in url:
            return {"bids": [["0.41", "100"]], "asks": [["0.43", str(self.ask_size)]]}
        raise AssertionError(url)


class RecorderReplayTests(unittest.TestCase):
    def capture(self, root: pathlib.Path, *, ask_size: float = 100.0, ts: str = "2026-06-14T03:30:00+00:00") -> None:
        market_recorder.capture_once(
            root,
            fetcher=ReplayFetcher(ask_size=ask_size),
            now=lambda: ts,
            max_books=2,
        )

    def test_book_walk_buy_uses_recorded_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root)
            context = recorder_replay.build_recorder_context(root, verify_scope="full")
            market = market_recorder.load_latest_markets(root, max_stale_seconds=999999999)["markets"][0]

            fill = recorder_replay.book_walk_fill(
                market=market,
                outcome_name="Yes",
                side="buy",
                limit_price=0.44,
                notional=10.0,
                context=context,
            )

        self.assertTrue(fill["ok"])
        self.assertEqual(fill["source"], "recorded_orderbook_depth")
        self.assertAlmostEqual(fill["fill_price"], 0.43)
        self.assertEqual(fill["book_ref"]["source"], "clob_book")

    def test_book_walk_rejects_insufficient_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root, ask_size=1.0)
            context = recorder_replay.build_recorder_context(root, verify_scope="full")
            market = market_recorder.load_latest_markets(root, max_stale_seconds=999999999)["markets"][0]
            data_dir = root / "paper_duel"
            state = agent_duel.init_state(data_dir, reset=True)
            decision = {
                "agent_id": "superwing",
                "orders": [{"market_id": "btc", "outcome": "Yes", "side": "buy", "notional": 10.0, "limit_price": 0.44}],
            }

            result = agent_duel.validate_and_apply(
                data_dir,
                state,
                "superwing",
                decision,
                [market],
                apply=True,
                max_orders=2,
                max_notional_per_order=45.0,
                execution_context={"source": "test", "recorder": context, "recorder_public": recorder_replay.public_context(context), "bot_script_hash": "script-hash", "bot_script_status": "active"},
                now_fn=lambda: "2026-06-14T03:30:00+00:00",
            )
            ledger = [json.loads(line) for line in (data_dir / "risk_ledger.jsonl").read_text().splitlines()]

        self.assertEqual(result["fills"], [])
        self.assertIn("insufficient_recorded_ask_depth", result["rejections"][0]["reason"])
        self.assertEqual(ledger[-1]["event"], "order_rejected")
        self.assertEqual(ledger[-1]["rejection_reason"], "insufficient_recorded_ask_depth")

    def test_tail_replay_frame_lookup_does_not_materialize_large_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root)
            manifest_path = root / "raw" / "polymarket" / "2026-06-14" / "manifest.jsonl"
            rows = [json.loads(line) for line in manifest_path.read_text().splitlines()]
            book_row = next(row for row in rows if row["source"] == "clob_book")

            with mock.patch.object(market_recorder, "tail_text_lines", side_effect=AssertionError("tail materialized")):
                frame_ref = recorder_replay.frame_for_manifest_row(root, book_row, max_frame_lines=2000)

        self.assertIsNotNone(frame_ref)
        self.assertEqual(frame_ref["source"], "clob_book")

    def test_tail_replay_manifest_lookup_uses_bounded_iterator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root)
            with mock.patch.object(market_recorder, "iter_tail_text_lines", wraps=market_recorder.iter_tail_text_lines) as iter_tail:
                rows = recorder_replay.manifest_rows_for_ts(root, "2026-06-14T03:30:00+00:00", max_lines=50)

        self.assertTrue(iter_tail.called)
        self.assertGreaterEqual(len(rows), 4)
        self.assertEqual(rows, sorted(rows, key=lambda row: row["sequence"]))

    def test_tail_context_for_non_latest_timestamp_falls_back_to_full_manifest_lookup(self):
        first_ts = "2026-06-14T03:30:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root, ts=first_ts)
            for minute in range(31, 45):
                self.capture(root, ts=f"2026-06-14T03:{minute:02d}:00+00:00")

            context = recorder_replay.build_recorder_context(root, ts=first_ts, verify_scope="tail", max_rows=3)

        self.assertEqual(context["ts"], first_ts)
        self.assertEqual(context["source"], "polymarket_market_recorder_v0")
        self.assertIn("clob_book", context["source_refs"])

    def test_replay_is_deterministic_for_fixed_recorder_and_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recorder_dir = root / "recorder"
            paper_dir = root / "paper"
            self.capture(recorder_dir)
            bot_scripts.ensure_default_bot_scripts(paper_dir)

            first = recorder_replay.replay_session(
                recorder_data_dir=recorder_dir,
                paper_data_dir=paper_dir,
                output_dir=root / "replay1",
            )
            second = recorder_replay.replay_session(
                recorder_data_dir=recorder_dir,
                paper_data_dir=paper_dir,
                output_dir=root / "replay2",
            )

        self.assertEqual(first["account_state_hash"], second["account_state_hash"])
        self.assertEqual(first["risk_ledger_hash"], second["risk_ledger_hash"])
        self.assertGreater(first["risk_ledger_rows"], 0)

    def test_replay_detects_tampered_raw_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recorder_dir = root / "recorder"
            paper_dir = root / "paper"
            self.capture(recorder_dir)
            bot_scripts.ensure_default_bot_scripts(paper_dir)
            raw_path = recorder_dir / "raw" / "polymarket" / "2026-06-14" / "clob_books.jsonl"
            rows = [json.loads(line) for line in raw_path.read_text().splitlines()]
            rows[0]["payload"]["book"]["asks"][0][0] = "0.99"
            raw_path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                recorder_replay.replay_session(
                    recorder_data_dir=recorder_dir,
                    paper_data_dir=paper_dir,
                    output_dir=root / "replay",
                )

    def test_live_tick_records_recorder_context_and_risk_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root)
            bot_scripts.ensure_default_bot_scripts(root / "paper_duel")
            args = types.SimpleNamespace(data_dir=str(root / "paper_duel"), env_file="", mode="paper_apply", limit=1, min_volume=0, max_orders=2, mock_markets="", allow_proxy=False, dashboard_dir="")
            with mock.patch.dict(
                os.environ,
                {
                    "AURUM_RECORDER_MAX_STALE_SECONDS": "999999999",
                    "AURUM_DEEPSEEK_ALLOW_PAPER_APPLY": "true",
                    "AURUM_DEEPSEEK_OPERATOR_CONFIRM": "ALLOW_DEEPSEEK_PAPER_APPLY",
                },
                clear=False,
            ):
                tick = agent_bot_loop.run_mechanical_tick(args)
            ledger = [json.loads(line) for line in (root / "paper_duel" / "risk_ledger.jsonl").read_text().splitlines()]

        self.assertEqual(tick["market_source"]["recorder_context"]["source"], "polymarket_market_recorder_v0")
        self.assertTrue(tick["market_source"]["recorder_context"]["capture_id"])
        self.assertTrue(tick["applied"])
        self.assertEqual(tick["agents"]["superwing"]["result"]["fills"][0]["fill_source"], "recorded_orderbook_depth")
        self.assertEqual(ledger[0]["execution_context"]["recorder"]["capture_id"], tick["market_source"]["recorder_context"]["capture_id"])


if __name__ == "__main__":
    unittest.main()
