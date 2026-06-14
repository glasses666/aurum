import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import data_quality_gate


class DataQualityGateTests(unittest.TestCase):
    def write_artifacts(self, root: pathlib.Path, *, health: dict, markets: list, ts: str = "2026-06-14T03:30:00+00:00") -> None:
        (root / "reports").mkdir(parents=True, exist_ok=True)
        (root / "normalized" / "polymarket").mkdir(parents=True, exist_ok=True)
        payload = dict(health)
        payload.setdefault("ts", ts)
        (root / "reports" / "market_recorder_health.json").write_text(json.dumps(payload), encoding="utf-8")
        (root / "normalized" / "polymarket" / "latest_markets.json").write_text(
            json.dumps({"ts": ts, "source": "polymarket_market_recorder_v0", "markets": markets}),
            encoding="utf-8",
        )

    def healthy_health(self) -> dict:
        return {
            "ok": True,
            "sources": {
                "gamma_markets": {"ok_frames": 1},
                "clob_markets": {"ok_frames": 1},
                "data_trades": {"ok_frames": 1},
                "clob_book": {"ok_frames": 2, "requested_tokens": 2},
            },
            "book_coverage": {"requested_tokens": 2, "ok_tokens": 2, "orderable_tokens": 2},
            "manifest": {"ok": True, "frames": 4},
        }

    def test_trade_allowed_only_when_health_and_latest_markets_are_fresh_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                health=self.healthy_health(),
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "TRADE_ALLOWED")
        self.assertTrue(decision["trade_allowed"])
        self.assertEqual(decision["reason_codes"], [])
        self.assertEqual(decision["market_source"], "polymarket_market_recorder_v0")

    def test_incomplete_orderbook_source_is_hold_only_not_trade_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            health = self.healthy_health()
            health["sources"]["clob_book"] = {"ok_frames": 0, "requested_tokens": 2}
            self.write_artifacts(
                root,
                health=health,
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("source_not_ok:clob_book", decision["reason_codes"])

    def test_incomplete_book_coverage_is_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            health = self.healthy_health()
            health["sources"]["clob_book"] = {"ok_frames": 1, "requested_tokens": 2}
            health["book_coverage"] = {"requested_tokens": 2, "ok_tokens": 1, "orderable_tokens": 1}
            self.write_artifacts(
                root,
                health=health,
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertIn("book_coverage_incomplete", decision["reason_codes"])

    def test_manifest_verification_failure_is_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            health = self.healthy_health()
            health["manifest"] = {"ok": False, "errors": ["manifest_hash_error:1"]}
            self.write_artifacts(
                root,
                health=health,
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertIn("manifest_verification_failed", decision["reason_codes"])

    def test_stale_recorder_artifacts_are_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                health=self.healthy_health(),
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:40:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertIn("recorder_stale", decision["reason_codes"])

    def test_missing_required_artifacts_are_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("missing_health_report", decision["reason_codes"])
        self.assertIn("missing_latest_markets", decision["reason_codes"])

    def test_unreadable_core_artifact_stops_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "reports").mkdir(parents=True)
            (root / "normalized" / "polymarket").mkdir(parents=True)
            (root / "reports" / "market_recorder_health.json").write_text("{not-json", encoding="utf-8")
            (root / "normalized" / "polymarket" / "latest_markets.json").write_text(
                json.dumps({"ts": "2026-06-14T03:30:00+00:00", "source": "polymarket_market_recorder_v0", "markets": []}),
                encoding="utf-8",
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "STOP_SERVICE")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("health_report_parse:JSONDecodeError", decision["reason_codes"])

    def test_incomplete_latest_market_payload_is_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(root, health=self.healthy_health(), markets=[{}])

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("market_payload_incomplete", decision["reason_codes"])

    def test_malformed_source_frame_count_is_structured_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            health = self.healthy_health()
            health["sources"]["clob_book"] = {"ok_frames": "many"}
            self.write_artifacts(
                root,
                health=health,
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("source_invalid:clob_book", decision["reason_codes"])

    def test_boolean_source_frame_count_is_structured_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            health = self.healthy_health()
            health["sources"]["clob_book"] = {"ok_frames": True}
            self.write_artifacts(
                root,
                health=health,
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("source_invalid:clob_book", decision["reason_codes"])

    def test_fractional_source_frame_count_is_structured_hold_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            health = self.healthy_health()
            health["sources"]["clob_book"] = {"ok_frames": 1.5}
            self.write_artifacts(
                root,
                health=health,
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("source_invalid:clob_book", decision["reason_codes"])

    def test_malformed_health_ok_value_is_hold_only_not_truthy_trade_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            health = self.healthy_health()
            health["ok"] = "false"
            self.write_artifacts(
                root,
                health=health,
                markets=[{"market_id": "btc", "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("health_ok_invalid", decision["reason_codes"])

    def test_bad_market_volume_is_hold_only_before_hot_path_casts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                health=self.healthy_health(),
                markets=[{"market_id": "btc", "volume": "many", "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("market_payload_incomplete", decision["reason_codes"])

    def test_missing_market_volume_is_hold_only_before_hot_path_casts_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                health=self.healthy_health(),
                markets=[{"market_id": "btc", "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("market_payload_incomplete", decision["reason_codes"])

    def test_bad_extra_outcome_price_is_hold_only_before_hot_path_sorts_all_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                health=self.healthy_health(),
                markets=[
                    {
                        "market_id": "btc",
                        "volume": 5000,
                        "outcomes": [
                            {"name": "Yes", "price": 0.42},
                            {"name": "No", "price": 0.58},
                            {"name": "Maybe", "price": "bad"},
                        ],
                    }
                ],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("market_payload_incomplete", decision["reason_codes"])

    def test_non_string_market_id_is_hold_only_before_hot_path_hashes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                health=self.healthy_health(),
                markets=[{"market_id": [], "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("market_payload_incomplete", decision["reason_codes"])

    def test_non_string_outcome_name_is_hold_only_before_hot_path_lowercases_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                health=self.healthy_health(),
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": ["Yes"], "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("market_payload_incomplete", decision["reason_codes"])

    def test_future_recorder_artifacts_are_hold_only_clock_skew(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_artifacts(
                root,
                ts="2026-06-14T03:40:00+00:00",
                health=self.healthy_health(),
                markets=[{"market_id": "btc", "volume": 5000, "outcomes": [{"name": "Yes", "price": 0.42}, {"name": "No", "price": 0.58}]}],
            )

            decision = data_quality_gate.evaluate_data_quality_gate(
                root,
                now=lambda: "2026-06-14T03:31:00+00:00",
                max_stale_seconds=180,
            )

        self.assertEqual(decision["decision"], "HOLD_ONLY")
        self.assertFalse(decision["trade_allowed"])
        self.assertIn("recorder_ts_in_future", decision["reason_codes"])


if __name__ == "__main__":
    unittest.main()
