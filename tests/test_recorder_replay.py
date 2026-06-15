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
import data_quality_gate
import market_recorder
import recorder_replay


class ReplayFetcher:
    def __init__(
        self,
        *,
        ask_size: float = 100.0,
        market_id: str = "btc",
        question: str = "Will Bitcoin close above 100k?",
        fail_book_tokens: tuple[str, ...] = (),
    ):
        self.ask_size = ask_size
        self.market_id = market_id
        self.question = question
        self.fail_book_tokens = fail_book_tokens

    def __call__(self, url, timeout=12.0):
        if "gamma-api.polymarket.com" in url:
            return [
                {
                    "id": self.market_id,
                    "question": self.question,
                    "slug": self.market_id,
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
            if any(token in url for token in self.fail_book_tokens):
                raise RuntimeError("book unavailable")
            return {"bids": [["0.41", "100"]], "asks": [["0.43", str(self.ask_size)]]}
        raise AssertionError(url)


class RecorderReplayTests(unittest.TestCase):
    def capture(
        self,
        root: pathlib.Path,
        *,
        ask_size: float = 100.0,
        ts: str = "2026-06-14T03:30:00+00:00",
        market_id: str = "btc",
        question: str = "Will Bitcoin close above 100k?",
        fail_book_tokens: tuple[str, ...] = (),
    ) -> None:
        market_recorder.capture_once(
            root,
            fetcher=ReplayFetcher(ask_size=ask_size, market_id=market_id, question=question, fail_book_tokens=fail_book_tokens),
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

    def test_recorder_context_does_not_count_failed_book_frames_as_ok_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root, fail_book_tokens=("tok_no",))
            health = json.loads((root / "reports" / "market_recorder_health.json").read_text())
            context = recorder_replay.build_recorder_context(root, verify_scope="full")

        self.assertEqual(health["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(health["book_coverage"]["ok_tokens"], 1)
        self.assertEqual(context["book_coverage"]["requested_tokens"], 2)
        self.assertEqual(context["book_coverage"]["ok_tokens"], 1)
        self.assertIn("tok_yes", context["books_by_token"])
        self.assertNotIn("tok_no", context["books_by_token"])
        self.assertNotIn("tok_no", context["book_refs_by_token"])

    def test_no_order_decision_still_writes_risk_ledger_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = pathlib.Path(tmp)
            state = agent_duel.init_state(data_dir, reset=True)

            result = agent_duel.validate_and_apply(
                data_dir,
                state,
                "superwing",
                {"agent_id": "superwing", "orders": []},
                [],
                apply=True,
                max_orders=2,
                max_notional_per_order=45.0,
                execution_context={"source": "test", "recorder_public": {"capture_id": "cap-1"}, "bot_script_hash": "script-hash"},
                now_fn=lambda: "2026-06-14T03:30:00+00:00",
            )
            ledger = [json.loads(line) for line in (data_dir / "risk_ledger.jsonl").read_text().splitlines()]

        self.assertEqual(result["fills"], [])
        self.assertEqual(ledger[0]["event"], "decision_recorded")
        self.assertEqual(ledger[0]["order_count"], 0)
        self.assertEqual(ledger[0]["execution_context"]["recorder"]["capture_id"], "cap-1")

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

    def test_latest_tail_context_uses_published_manifest_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root)

            with mock.patch.object(market_recorder, "verify_manifest", side_effect=AssertionError("hot path full verifier")):
                context = recorder_replay.build_recorder_context(root, verify_scope="tail", max_rows=500)

        self.assertEqual(context["manifest"]["verification_scope"], "tail")
        self.assertEqual(context["manifest"]["verified_rows"], 5)
        self.assertIn("clob_book", context["source_refs"])

    def test_latest_tail_context_rejects_missing_published_terminal_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root)
            health_path = root / "reports" / "market_recorder_health.json"
            health = json.loads(health_path.read_text(encoding="utf-8"))
            health["manifest"].pop("last_manifest_sha256", None)
            health_path.write_text(json.dumps(health, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "last_manifest_sha256"):
                recorder_replay.build_recorder_context(root, verify_scope="tail", max_rows=500)

    def test_latest_tail_context_rejects_manifest_sequence_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root)
            manifest_path = root / "raw" / "polymarket" / "2026-06-14" / "manifest.jsonl"
            rows = [json.loads(line) for line in manifest_path.read_text().splitlines()]
            rows[-1]["sequence"] = 999
            rows[-1]["manifest_sha256"] = market_recorder.sha256_text(
                market_recorder.canonical_json({k: v for k, v in rows[-1].items() if k != "manifest_sha256"})
            )
            manifest_path.write_text("\n".join(market_recorder.canonical_json(row) for row in rows) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "manifest_sequence_error|manifest_latest_sequence_mismatch"):
                recorder_replay.build_recorder_context(root, verify_scope="tail", max_rows=500)

    def test_tail_context_for_non_latest_timestamp_requires_full_scope(self):
        first_ts = "2026-06-14T03:30:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root, ts=first_ts)
            for minute in range(31, 45):
                self.capture(root, ts=f"2026-06-14T03:{minute:02d}:00+00:00")

            with self.assertRaisesRegex(RuntimeError, "non_latest_tail_requires_full_scope"):
                recorder_replay.build_recorder_context(root, ts=first_ts, verify_scope="tail", max_rows=3)
            context = recorder_replay.build_recorder_context(root, ts=first_ts, verify_scope="full")

        self.assertEqual(context["ts"], first_ts)
        self.assertEqual(context["source"], "polymarket_market_recorder_v0")
        self.assertIn("clob_book", context["source_refs"])

    def test_tail_context_rejects_latest_and_health_rollback_to_older_capture(self):
        first_ts = "2026-06-14T03:30:00+00:00"
        second_ts = "2026-06-14T03:31:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root, ts=first_ts, market_id="btc-old", question="Will Bitcoin old capture close higher?")
            latest_path = root / "normalized" / "polymarket" / "latest_markets.json"
            health_path = root / "reports" / "market_recorder_health.json"
            old_latest = latest_path.read_text(encoding="utf-8")
            old_health = health_path.read_text(encoding="utf-8")
            self.capture(root, ts=second_ts, market_id="btc-new", question="Will Bitcoin new capture close higher?")
            latest_path.write_text(old_latest, encoding="utf-8")
            health_path.write_text(old_health, encoding="utf-8")

            gate = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:32:00+00:00",
                max_stale_seconds=999999999,
            )
            with self.assertRaisesRegex(RuntimeError, "manifest_tail_terminal_mismatch"):
                recorder_replay.build_recorder_context(root, verify_scope="tail", max_rows=500)

        self.assertEqual(gate["decision"], data_quality_gate.HOLD_ONLY)
        self.assertIn("manifest_tail_terminal_mismatch", gate["reason_codes"])

    def test_tail_context_verifies_every_manifest_row_it_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root, market_id="btc-raw", question="Will Bitcoin raw frame win?")
            day_dir = root / "raw" / "polymarket" / "2026-06-14"
            raw_path = day_dir / "gamma_markets.jsonl"
            frame = json.loads(raw_path.read_text(encoding="utf-8").splitlines()[0])
            frame["payload"][0]["id"] = "btc-tampered"
            frame["payload"][0]["question"] = "Tampered raw gamma frame outside tiny verified tail"
            frame["payload_sha256"] = market_recorder.sha256_text(market_recorder.canonical_json(frame["payload"]))
            raw_line = market_recorder.canonical_json(frame)
            raw_path.write_text(raw_line + "\n", encoding="utf-8")
            manifest_path = day_dir / "manifest.jsonl"
            rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["payload_sha256"] = frame["payload_sha256"]
            rows[0]["line_sha256"] = market_recorder.sha256_text(raw_line)
            rows[0]["manifest_sha256"] = market_recorder.sha256_text(
                market_recorder.canonical_json({k: v for k, v in rows[0].items() if k != "manifest_sha256"})
            )
            manifest_path.write_text("\n".join(market_recorder.canonical_json(row) for row in rows) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "manifest_prev_hash_error"):
                recorder_replay.build_recorder_context(root, verify_scope="tail", max_rows=3)

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

    def test_replay_uses_requested_historical_capture_markets_not_current_latest(self):
        first_ts = "2026-06-14T03:30:00+00:00"
        second_ts = "2026-06-14T03:31:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recorder_dir = root / "recorder"
            paper_dir = root / "paper"
            self.capture(recorder_dir, ts=first_ts, market_id="btc-old", question="Will Bitcoin old capture close higher?")
            self.capture(recorder_dir, ts=second_ts, market_id="btc-new", question="Will Bitcoin new capture close higher?")
            bot_scripts.ensure_default_bot_scripts(paper_dir)

            summary = recorder_replay.replay_session(
                recorder_data_dir=recorder_dir,
                paper_data_dir=paper_dir,
                output_dir=root / "replay-old",
                start_ts=first_ts,
            )

        self.assertEqual(summary["recorder_context"]["ts"], first_ts)
        self.assertEqual(summary["market_ids"], ["btc-old"])

    def test_replay_rejects_output_dir_that_would_delete_source_paper_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recorder_dir = root / "recorder"
            paper_dir = root / "paper_duel"
            self.capture(recorder_dir)
            bot_scripts.ensure_default_bot_scripts(paper_dir)

            with self.assertRaisesRegex(RuntimeError, "unsafe_replay_output_dir"):
                recorder_replay.replay_session(
                    recorder_data_dir=recorder_dir,
                    paper_data_dir=paper_dir,
                    output_dir=root,
                )

    def test_replay_rejects_output_dir_inside_recorder_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            recorder_dir = root / "recorder"
            paper_dir = root / "paper"
            self.capture(recorder_dir)
            bot_scripts.ensure_default_bot_scripts(paper_dir)

            with self.assertRaisesRegex(RuntimeError, "unsafe_replay_output_dir"):
                recorder_replay.replay_session(
                    recorder_data_dir=recorder_dir,
                    paper_data_dir=paper_dir,
                    output_dir=recorder_dir,
                )

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

    def test_live_tick_consumes_manifest_verified_raw_markets_not_mutable_latest_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.capture(root, market_id="btc-raw", question="Will Bitcoin raw frame win?")
            latest_path = root / "normalized" / "polymarket" / "latest_markets.json"
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest["markets"][0]["market_id"] = "btc-tampered"
            latest["markets"][0]["question"] = "Mutable latest json should not drive paper_apply"
            latest_path.write_text(json.dumps(latest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            health_path = root / "reports" / "market_recorder_health.json"
            health = json.loads(health_path.read_text(encoding="utf-8"))
            health["latest_markets_sha256"] = market_recorder.sha256_text(market_recorder.canonical_json(latest))
            health_path.write_text(json.dumps(health, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            args = types.SimpleNamespace(limit=1, min_volume=0, mock_markets="", allow_proxy=False)

            with mock.patch.dict(os.environ, {"AURUM_RECORDER_MAX_STALE_SECONDS": "999999999"}, clear=False):
                markets, source = agent_bot_loop.load_markets_for_tick(root / "paper_duel", args)

        self.assertEqual(source["source"], "polymarket_market_recorder_v0")
        self.assertEqual(source["recorder_context"]["source"], "polymarket_market_recorder_v0")
        self.assertEqual(markets[0]["market_id"], "btc-raw")
        self.assertNotEqual(markets[0]["market_id"], latest["markets"][0]["market_id"])


if __name__ == "__main__":
    unittest.main()
